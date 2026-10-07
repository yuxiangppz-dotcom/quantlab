"""Ephemeral fixed runner backed by a private, durable Cloudflare archive/claim."""

import argparse
import gzip
import hashlib
import io
import json
import os
import threading
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.request import Request, build_opener
from uuid import uuid4

from quantlab.scout.cloud_artifacts import guarded, initialize
from quantlab.scout.cloud_data import (
    Fetcher,
    prepare_history,
    refresh_calendar,
    refresh_market,
    refresh_observation_limits,
)
from quantlab.scout.cloud_push import NoRedirect, public_origin
from quantlab.scout.cloud_runner import tick
from quantlab.scout.daily_runtime import atomic, read
from quantlab.scout.models import SHANGHAI, fingerprint
from quantlab.scout.nextday_contract import VERSION as NEXT_VERSION
from quantlab.scout.observation_runtime import scan_pending

CHUNK = 131072
MAX_FILE = 32 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
PREFIXES = {
    "market",
    "runtime",
    "runs",
    "reports",
    "source_updates",
    "schedule",
    "outbox",
    "source_cache",
    "observations",
    "data_preparation",
    "event_index",
    "hot_snapshots",
    "tushare_snapshots",
}
ROOT_FILES = {".scout-cloud", "latest.json", "cloud-status.json"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def safe_path(value):
    if not isinstance(value, str) or "\\" in value or len(value) > 500:
        raise ValueError("archive_path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or (value not in ROOT_FILES and path.parts[0] not in PREFIXES)
        or path.suffix in {".lock", ".tmp", ".log"}
    ):
        raise ValueError("archive_path")
    return path


class Client:
    def __init__(self, origin, token, *, opener=None):
        self.origin = public_origin(origin)
        if len(token) < 32:
            raise ValueError("archive_credentials_missing")
        self.token = token
        self.opener = opener or build_opener(NoRedirect())
        self.calls = 0
        self.lock = threading.Lock()

    def request(self, path, *, method="GET", value=None, binary=None, job=None):
        if not path.startswith("/api/") or "?" in path:
            raise ValueError("archive_endpoint")
        with self.lock:
            if self.calls >= 8000:
                raise ValueError("archive_request_budget")
            self.calls += 1
        headers = {
            "Authorization": "Bearer " + self.token,
            "User-Agent": "QuantLab-Scout-Cloud/1.0",
        }
        if job:
            headers.update({"X-Scout-Day": job["day"], "X-Scout-Owner": job["owner"]})
        data = binary if binary is not None else encoded(value) if value is not None else None
        headers["Content-Type"] = (
            "application/octet-stream" if binary is not None else "application/json"
        )
        request = Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                content = response.read(1_200_001)
                if len(content) > 1_200_000:
                    raise ValueError("archive_response_bound")
                return (
                    content
                    if path.startswith("/api/blobs/") and method == "GET"
                    else json.loads(content)
                )
        except Exception:
            # urllib HTTP errors can contain credential-bearing URLs/headers. Never log them.
            raise ValueError("archive_delivery_unknown_no_retry") from None


def restore(root, snapshot, client):
    root = guarded(root)
    if not snapshot:
        return
    files = snapshot["files"]
    if len(files) > 5000 or sha(encoded(files)) != snapshot["sha256"]:
        raise ValueError("archive_manifest_integrity")
    seen = set()
    total = 0
    if sum(r["size"] for r in files) > 512 * 1024 * 1024:
        raise ValueError("archive_expanded_bound")
    for record in files:
        relative = safe_path(record["path"])
        if relative in seen or not 0 <= record["size"] <= MAX_FILE:
            raise ValueError("archive_file_bound")
        seen.add(relative)
        compressed = bytearray()
        for digest in record["chunks"]:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("archive_chunk_identity")
            data = client.request("/api/blobs/" + digest)
            total += len(data)
            if len(data) > CHUNK or total > MAX_ARCHIVE or sha(data) != digest:
                raise ValueError("archive_chunk_integrity")
            compressed.extend(data)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            data = stream.read(MAX_FILE + 1)
        if len(data) != record["size"] or sha(data) != record["sha256"]:
            raise ValueError("archive_file_integrity")
        destination = root / str(relative)
        if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
            raise ValueError("archive_symlink")
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError("archive_local_conflict")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    guarded(root)


class Archive:
    def __init__(self, root, client, job):
        self.root, self.client, self.job = guarded(root), client, job
        self.uploaded = set()
        self.previous = None
        self.stop = threading.Event()
        self.error = None
        self.lock = threading.Lock()

    def checkpoint(self):
        with self.lock:
            files, total = [], 0
            credentials = [
                value.encode()
                for name in ("TUSHARE_TOKEN", "DEEPSEEK_API_KEY", "SCOUT_API_TOKEN")
                if len(value := os.environ.get(name, "")) >= 12
            ]
            for path in sorted(self.root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("archive_symlink")
                if not path.is_file() or path.suffix in {".lock", ".tmp", ".log"}:
                    continue
                relative = path.relative_to(self.root).as_posix()
                safe_path(relative)
                if path.stat().st_size > MAX_FILE:
                    raise ValueError("archive_file_bound")
                data = path.read_bytes()
                if any(secret in data for secret in credentials):
                    raise ValueError("secret_in_archive")
                packed = gzip.compress(data, mtime=0)
                total += len(packed)
                if total > MAX_ARCHIVE or len(files) >= 5000:
                    raise ValueError("archive_budget")
                chunks = []
                for start in range(0, len(packed), CHUNK):
                    chunk = packed[start : start + CHUNK]
                    digest = sha(chunk)
                    chunks.append(digest)
                    if digest not in self.uploaded:
                        self.client.request(
                            "/api/blobs/" + digest, method="PUT", binary=chunk, job=self.job
                        )
                        self.uploaded.add(digest)
                files.append(
                    {"path": relative, "sha256": sha(data), "size": len(data), "chunks": chunks}
                )
            snapshot = {"sha256": sha(encoded(files)), "files": files}
            if snapshot["sha256"] != self.previous:
                if len(encoded(snapshot)) > 1_050_000:
                    raise ValueError("archive_manifest_bound")
                self.client.request(
                    "/api/checkpoint", method="POST", value=self.job | {"snapshot": snapshot}
                )
                self.previous = snapshot["sha256"]

    def background(self):
        while not self.stop.wait(30):
            try:
                self.checkpoint()
            except Exception:
                self.error = "archive_checkpoint_failed"
                return


def observation_catchup(root, now, *, fetcher_factory=Fetcher, scan=scan_pending):
    """Model-free bounded catch-up; preserve failures without blocking prediction."""
    receipt = {"status": "running", "model_calls": 0, "started_at": now.isoformat()}
    fetcher = None
    storage = None
    try:
        fetcher = fetcher_factory(root, now)
        storage = refresh_calendar(root, fetcher, now)
        receipt["data"] = refresh_market(root, fetcher, now, history_sessions=120)
    except Exception as exc:
        receipt.update(status="data_unknown", error_type=type(exc).__name__)
    if fetcher is not None:
        try:
            receipt["target_limits"] = refresh_observation_limits(root, fetcher, now)
            if receipt["target_limits"]["status"] != "ready":
                receipt["status"] = "data_unknown"
        except Exception as exc:
            receipt.update(status="data_unknown", limit_error_type=type(exc).__name__)
    try:
        activation = root / "observations" / "activation.json"
        if not activation.exists():
            atomic(activation, {"started_at": now.isoformat(), "start_date": now.date().isoformat(),
                                "version": NEXT_VERSION})
        start = read(activation)["start_date"]
        eligible = None
        if storage is not None:
            eligible = sorted({r.trade_date.isoformat() for r in storage.load_trading_calendar()
                               if r.exchange == "SSE" and r.is_open
                               and start <= r.trade_date.isoformat()
                               and (r.trade_date < now.date()
                                    or r.trade_date == now.date() and now.hour >= 18)})
        receipt["observation"] = scan(root / "runs", root / "market", root / "observations",
                                      eligible_target_sessions=eligible, max_reports=200)
        if receipt["status"] == "running":
            receipt["status"] = "completed"
    except Exception as exc:
        receipt.update(status="observation_failed_preserved", error_type=type(exc).__name__)
    receipt["provider_calls"] = getattr(fetcher, "calls", 0)
    atomic(root / "observations" / "jobs" / (uuid4().hex + ".json"), receipt)
    return receipt


def maintenance(settings, client, *, app_commit, kind, batch=0, now=None,
                fetcher_factory=Fetcher, scan=scan_pending):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if kind not in {"observation", "prepare"} or not 0 <= batch <= 7:
        raise ValueError("maintenance_kind_or_batch")
    if kind == "observation" and not (now.hour == 8 or now.hour >= 18):
        return {"status": "outside_observation_window", "model_calls": 0}
    job = client.request("/api/maintenance/claim", method="POST", value={
        "day": now.date().isoformat(), "app_commit": app_commit, "kind": kind, "batch": batch})
    if job["status"] != "claimed":
        return job
    root = initialize(Path(settings["canonical_dir"]).parent)
    archive = Archive(root, client, job)
    restore(root, client.request("/api/snapshot")["snapshot"], client)
    archive.checkpoint()  # Durable owner/workspace before any provider work.
    thread = threading.Thread(target=archive.background, daemon=True)
    thread.start()
    result = {"status": "failed", "model_calls": 0}
    try:
        if kind == "prepare":
            fetcher = fetcher_factory(root, now)
            result = prepare_history(root, fetcher, now, partition_limit=40)
            result["model_calls"] = 0
        else:
            result = observation_catchup(root, now, fetcher_factory=fetcher_factory, scan=scan)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__, "model_calls": 0}
    finally:
        archive.stop.set()
        thread.join(timeout=60)
        if thread.is_alive() or archive.error:
            raise ValueError("archive_unresolved_maintenance_claim_retained")
        atomic(root / "observations" / "maintenance_jobs" / (uuid4().hex + ".json"),
               result | {"kind": kind, "batch": batch, "started_at": now.isoformat()})
        archive.checkpoint()
    state = ("completed" if result["status"] in {"completed", "ready"}
             else "pending" if result["status"] == "pending" else "failed")
    client.request("/api/maintenance/finish", method="POST", value=job | {"status": state})
    return result | {"maintenance_status": state}


def execute(settings, client, *, app_commit, now=None, run=tick, catchup=observation_catchup):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if now.hour != 8:
        return {"status": "outside_morning_window"}
    job = client.request(
        "/api/claim", method="POST", value={"day": now.date().isoformat(), "app_commit": app_commit}
    )
    if job["status"] != "claimed":
        return job
    root = initialize(Path(settings["canonical_dir"]).parent)
    archive = Archive(root, client, job)
    snapshot = client.request("/api/snapshot")["snapshot"]
    restore(root, snapshot, client)
    archive.checkpoint()  # Initial durable workspace before any provider/model request.
    thread = threading.Thread(target=archive.background, daemon=True)
    thread.start()
    result = None
    try:
        # Push key lives only in Cloudflare. Local tick therefore never sends notifications.
        if os.environ.get("SERVERCHAN_SENDKEY"):
            raise ValueError("push_secret_must_not_be_on_runner")
        catchup(root, now)  # Independent observation failures are archived, never paid retries.
        archive.checkpoint()
        result = run(settings, now, enabled=True, cancelled=lambda: bool(archive.error))
    finally:
        archive.stop.set()
        thread.join(timeout=60)
        if thread.is_alive() or archive.error:
            raise ValueError("archive_unresolved_claim_retained")
        archive.checkpoint()  # Failure here retains remote claim and blocks automatic retry.
    if result["status"] == "published":
        metadata = result["report"]
        folder = root / "reports" / metadata["run_id"]
        markdown = (folder / "report.md").read_text(encoding="utf-8")
        published = client.request(
            "/api/publish",
            method="POST",
            value=job
            | {
                "metadata": metadata,
                "html": (folder / "report.html").read_text(encoding="utf-8"),
                "markdown": markdown,
                "markdown_sha256": sha(markdown.encode()),
            },
        )
        publication = published.get("publication")
        if publication:
            if (publication.get("run_id") != metadata["run_id"]
                    or publication.get("source_report_sha256") != metadata["source_report_sha256"]):
                raise ValueError("cloud_publication_identity")
            matches = list((root / "runs").rglob(metadata["run_id"] + "/report.json"))
            if len(matches) != 1:
                raise ValueError("cloud_publication_original_run_unknown")
            if fingerprint(read(matches[0])) != metadata["source_report_sha256"]:
                raise ValueError("cloud_publication_source_hash")
            receipt_path = matches[0].parent / "cloud-publication.json"
            if receipt_path.exists() and read(receipt_path) != publication:
                raise ValueError("cloud_publication_immutable_conflict")
            atomic(receipt_path, publication)
            archive.checkpoint()
    state = (
        result["status"]
        if result["status"] in {"published", "non_trading_day", "outside_morning_window"}
        else "failed"
    )
    client.request(
        "/api/finish",
        method="POST",
        value=job | {"status": state, "run_id": result.get("report", {}).get("run_id")},
    )
    if state in {"published", "failed"}:
        client.request("/api/notify", method="POST", value=job)
    return {"status": state, "target_session": job["day"]}


def sealed_identity(settings):
    """Read installed engine identity in its own interpreter, with zero data calls."""
    import subprocess

    from quantlab.scout.daily_runtime import prediction_settings, verify_release

    engine = prediction_settings(settings)
    root = Path(engine["release_root"])
    code = (
        "import json,hashlib; "
        "from quantlab.scout.daily_contract import VERSION,selection_instruction,selection_schema; "
        "from quantlab.scout.models import fingerprint; "
        "from quantlab.scout.selection_judgments import INSTRUCTION as J_INSTRUCTION,"
        "schema as j_schema; "
        "from pathlib import Path; "
        "cfg=json.loads(Path('config/scout_daily.fixed.json').read_text()); "
        "pkt=dict(prediction_objective='next_session') "
        "if cfg.get('next_session_selection') else {}; "
        "from quantlab.scout.nextday_contract import VERSION as NEXT_VERSION; "
        "ver=NEXT_VERSION if pkt else VERSION; "
        "instruction=J_INSTRUCTION if cfg.get('program_assembled_selection') "
        "else selection_instruction(pkt); "
        "schema=j_schema(dict(candidates=[])) if cfg.get('program_assembled_selection') "
        "else selection_schema([],pkt); "
        "print(json.dumps(dict(prompt_version=ver,"
        "decision_version=ver,config_sha256=fingerprint(cfg),"
        "prompt_text_sha256=hashlib.sha256(instruction.encode()).hexdigest(),"
        "schema_id=schema.get('$id'),schema_sha256=fingerprint(schema))))"
    )
    result = subprocess.run(
        [str(root / ".venv/bin/python"), "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    identity = json.loads(result.stdout)
    return {
        "application_commit": verify_release(settings)["commit"],
        "engine_commit": verify_release(engine)["commit"],
        **identity,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("/app/daily-settings.json"))
    parser.add_argument(
        "--check", action="store_true", help="Archive connectivity only; zero provider/model calls"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--observe", action="store_true")
    modes.add_argument("--prepare-data", action="store_true")
    parser.add_argument("--prepare-batch", type=int, choices=range(8), default=0)
    args = parser.parse_args()
    if args.check and (args.observe or args.prepare_data):
        parser.error("check cannot be combined with maintenance")
    try:
        client = Client(os.environ["SCOUT_PUBLIC_URL"], os.environ["SCOUT_API_TOKEN"])
        if args.check:
            client.request("/api/snapshot")
            result = {
                "status": "archive_connected",
                "provider_model_calls": 0,
                "installed_release": sealed_identity(read(args.settings)),
            }
        else:
            app = read(Path("/bundle.json"))["app_commit"]
            if args.observe or args.prepare_data:
                result = maintenance(read(args.settings), client, app_commit=app,
                                     kind="prepare" if args.prepare_data else "observation",
                                     batch=args.prepare_batch)
            else:
                result = execute(read(args.settings), client, app_commit=app)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "failed_unknown_no_retry", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
