"""Private immutable mobile views, separate from model and experiment archives."""

import hashlib
import re
from pathlib import Path
from uuid import uuid4

from quantlab.scout.concise_report import html, markdown
from quantlab.scout.daily_runtime import atomic, exclusive, read
from quantlab.scout.models import fingerprint

MARKER = "quantlab-scout-cloud-v1"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def initialize(root):
    root = Path(root)
    if root.is_symlink() or root.name != "scout":
        raise ValueError("Cloud volume must be a dedicated directory named scout")
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".scout-cloud"
    if marker.exists():
        if marker.read_text() != MARKER:
            raise ValueError("Cloud volume marker mismatch")
    else:
        if any(root.iterdir()):
            raise ValueError("Refuse to adopt a nonempty unmarked data directory")
        marker.write_text(MARKER)
    return root


def guarded(root):
    root = Path(root)
    if root.is_symlink() or (root / ".scout-cloud").read_text() != MARKER:
        raise ValueError("Not an isolated Scout cloud volume")
    for name in ("market", "runtime", "runs", "reports", "source_updates", "schedule", "outbox"):
        if not (root / name).resolve().is_relative_to(root.resolve()):
            raise ValueError("Cloud data subtree points outside its isolated volume")
    return root


def checked_report(run):
    report = read(Path(run) / "report.json")
    if fingerprint(report) != read(Path(run) / "manifest.json")["report_sha256"]:
        raise ValueError("Source report hash mismatch")
    if (
        report["status"] != "live_research_unvalidated"
        or report.get("opportunity", {}).get("validation", {}).get("status") != "complete"
        or not report.get("timing", {}).get("primary_eligible")
    ):
        raise ValueError("Only complete frozen prospective reports can be published")
    if not RUN_ID.fullmatch(report["run_id"]) or Path(run).name != report["run_id"]:
        raise ValueError("Invalid report identity")
    return report


def publish(root, run):
    root = guarded(root)
    report = checked_report(run)
    content = html(report).encode()
    metadata = {
        "run_id": report["run_id"],
        "generated_at": report["finished_at"],
        "target_session": report["timing"]["target_session"],
        "asof_session": report["market"]["session"],
        "source_report_sha256": fingerprint(report),
        "html_sha256": hashlib.sha256(content).hexdigest(),
    }
    with exclusive(root / "publish.lock"):
        folder = root / "reports" / report["run_id"]
        if folder.exists():
            if read(folder / "metadata.json") != metadata:
                raise ValueError("Published report is immutable")
            read_view(root, report["run_id"])
        else:
            pending = folder.with_name(".pending-" + uuid4().hex)
            pending.mkdir(parents=True)
            (pending / "report.html").write_bytes(content)
            (pending / "report.md").write_text(markdown(report), encoding="utf-8")
            atomic(pending / "metadata.json", metadata)
            pending.rename(folder)
        latest = root / "latest.json"
        if not latest.exists() or read(latest)["generated_at"] <= metadata["generated_at"]:
            atomic(latest, metadata)
    return metadata


def read_view(root, run_id):
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid report identity")
    folder = guarded(root) / "reports" / run_id
    content = (folder / "report.html").read_bytes()
    if hashlib.sha256(content).hexdigest() != read(folder / "metadata.json")["html_sha256"]:
        raise ValueError("Published report integrity failure")
    return content


def history(root):
    paths = (guarded(root) / "reports").glob("*/metadata.json")
    return sorted((read(p) for p in paths), key=lambda x: x["generated_at"], reverse=True)


def status(root, **values):
    atomic(guarded(root) / "cloud-status.json", values)
