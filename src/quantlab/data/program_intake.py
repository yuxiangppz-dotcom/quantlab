"""One fixed raw-input pilot for the user-approved v3 research program."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

from quantlab.data.dividend_acquisition import body_accounting, replace_status
from quantlab.data.dividend_raw import WireClient, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/research_program_intake_v1.json"
FUND_FIELDS = (
    "ts_code",
    "name",
    "management",
    "fund_type",
    "found_date",
    "due_date",
    "list_date",
    "issue_date",
    "delist_date",
    "m_fee",
    "c_fee",
    "benchmark",
    "status",
    "invest_type",
    "type",
    "market",
)
FLOW_FIELDS = (
    "ts_code",
    "trade_date",
    "buy_sm_vol",
    "buy_sm_amount",
    "sell_sm_vol",
    "sell_sm_amount",
    "buy_md_vol",
    "buy_md_amount",
    "sell_md_vol",
    "sell_md_amount",
    "buy_lg_vol",
    "buy_lg_amount",
    "sell_lg_vol",
    "sell_lg_amount",
    "buy_elg_vol",
    "buy_elg_amount",
    "sell_elg_vol",
    "sell_elg_amount",
    "net_mf_vol",
    "net_mf_amount",
)
OUTPUT = "data/products/research_program/launch_20260912/intake"
GOOD = {"nonempty", "empty"}


def now():
    return datetime.now(UTC).isoformat()


def requests_for(codes, start="20191001", end="20241231"):
    if (
        codes != sorted(set(codes))
        or not codes
        or any(re.fullmatch(r"\d{6}\.(SH|SZ)", c) is None for c in codes)
        or (start, end) != ("20191001", "20241231")
    ):
        raise DataValidationError("invalid fixed intake population or dates")
    requests = [
        {
            "id": f"fund_{status}",
            "row_cap": 15000,
            "parameters": {
                "api_name": "fund_basic",
                "params": {"market": "E", "status": status},
                "fields": ",".join(FUND_FIELDS),
            },
        }
        for status in ("D", "I", "L")
    ]
    requests.extend(
        {
            "id": f"flow_{code}",
            "row_cap": 6000,
            "parameters": {
                "api_name": "moneyflow",
                "params": {"ts_code": code, "start_date": start, "end_date": end},
                "fields": ",".join(FLOW_FIELDS),
            },
        }
        for code in codes
    )
    return requests


def inspect(raw, request):
    """Profile lossless rows; neither duplicates nor empty responses prove completeness."""
    try:
        payload = strict_json(raw)
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ValueError
        if payload["code"] != 0:
            # Preserve full provider bytes separately, never log message/exception text.
            message = str(payload.get("msg") or "").lower()
            permission = payload["code"] == 2002 or any(
                word in message for word in ("权限", "积分", "token", "permission", "授权")
            )
            return {
                "status": "permission_error" if permission else "provider_error",
                "server_code": payload["code"],
                "rows": None,
            }
        data = payload["data"]
        names, items = data["fields"], data["items"]
        required = request["parameters"]["fields"].split(",")
        if (
            not isinstance(names, list)
            or not all(isinstance(n, str) for n in names)
            or len(names) != len(set(names))
            or not set(required).issubset(names)
            or not isinstance(items, list)
        ):
            raise ValueError
        rows, keys, dates = [], [], []
        params = request["parameters"]["params"]
        is_flow = request["parameters"]["api_name"] == "moneyflow"
        for values in items:
            if not isinstance(values, list) or len(values) != len(names):
                raise ValueError
            row = dict(zip(names, values, strict=True))
            if not isinstance(row["ts_code"], str):
                raise ValueError
            if is_flow:
                day = row["trade_date"]
                if (
                    row["ts_code"] != params["ts_code"]
                    or not isinstance(day, str)
                    or len(day) != 8
                    or not day.isdigit()
                    or not params["start_date"] <= day <= params["end_date"]
                ):
                    raise ValueError
                datetime.strptime(day, "%Y%m%d")
                dates.append(day)
                keys.append((row["ts_code"], day))
            else:
                if row["market"] != "E" or row["status"] != params["status"]:
                    raise ValueError
                keys.append((row["ts_code"],))
            rows.append(row)
        saturated = len(rows) >= request["row_cap"]
        for obj in (payload, data):
            total = obj.get("total")
            saturated |= bool(obj.get("has_more") or obj.get("truncated"))
            saturated |= isinstance(total, (int, float)) and total > len(rows)
        return {
            "status": "saturated" if saturated else ("nonempty" if rows else "empty"),
            "server_code": 0,
            "rows": len(rows),
            "returned_fields": names,
            "duplicate_key_rows": len(keys) - len(set(keys)),
            "exact_duplicate_rows": len(rows)
            - len(
                {json.dumps(r, sort_keys=True, ensure_ascii=True, allow_nan=False) for r in rows}
            ),
            "null_counts": {n: sum(r[n] is None or r[n] == "" for r in rows) for n in required},
            "date_range": [min(dates), max(dates)] if dates else None,
        }
    except (ValueError, TypeError, KeyError, UnicodeError):
        return {"status": "schema_error", "rows": None}


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    if (
        config["schema"] != "quantlab_program_intake_v1"
        or config["endpoint"] != "https://api.tushare.pro"
        or config["output"] != OUTPUT
        or config["max_requests"] != 259
        or config["selection_count"] != 256
        or config["max_retries"] != 0
        or config["economic_paths_authorized"] != 0
        or config["model_fits_authorized"] != 0
    ):
        raise DataValidationError("unreviewed intake contract")
    verify_entries(root, config["inputs"])
    selection = sealed_read(root / config["selection_path"])
    if (
        selection["fingerprint"] != config["selection_fingerprint"]
        or len(selection["instrument_ids"]) != config["selection_count"]
    ):
        raise DataValidationError("intake selection changed")
    requests = requests_for(selection["instrument_ids"], config["start_date"], config["end_date"])
    identity = canonical_payload_fingerprint({"config_sha256": _sha(root / CONFIG)})
    return config, requests, identity


class Journal:
    """One durable intent per predeclared request, no automatic replay or budget reset."""

    def __init__(self, out, config, requests, identity, *, inspector=inspect):
        self.out, self.config, self.requests, self.identity = out, config, requests, identity
        self.inspector = inspector
        self.records = []
        wanted = {r["id"]: r for r in requests}
        folder = out / "attempts"
        if folder.exists() and any(
            p.name not in wanted or not p.is_dir() for p in folder.iterdir()
        ):
            raise DataValidationError("unregistered intake request directory")
        gap = False
        for request in requests:
            path = folder / request["id"]
            if not path.exists():
                gap = True
                continue
            if gap:
                raise DataValidationError("intake request order changed")
            intent = sealed_read(path / "intent.json")
            if (
                intent["identity"] != identity
                or intent["request"] != request
                or intent["reserved_bytes"] != config["per_response_bytes"]
            ):
                raise DataValidationError("intake intent changed")
            if datetime.fromisoformat(intent["at"]).utcoffset() is None:
                raise DataValidationError("unknown intake observation time")
            result = None
            if (path / "result.json").exists():
                result = sealed_read(path / "result.json")
                if (
                    result["intent_fingerprint"] != intent["fingerprint"]
                    or result["identity"] != identity
                    or any(
                        result.get(k) is not False
                        for k in (
                            "historical_pit_certified",
                            "execution_authority",
                            "complete_history",
                        )
                    )
                ):
                    raise DataValidationError("intake result identity or authority changed")
                verify_entries(path, result["artifacts"])
                body_accounting(result, config["per_response_bytes"])
                body = path / "response.body"
                if not result["raw_redacted"] and (
                    body.stat().st_size != result["received_bytes"]
                    or _sha(body) != result["wire_sha256"]
                ):
                    raise DataValidationError("intake raw response changed")
            self.records.append((request, intent, result))
        self.check_budget()

    @property
    def used_bytes(self):
        return sum(
            body_accounting(result, self.config["per_response_bytes"])["budget_body_bytes"]
            if result is not None
            else intent["reserved_bytes"]
            for _, intent, result in self.records
        )

    def check_budget(self):
        if (
            len(self.records) > self.config["max_requests"]
            or self.used_bytes > self.config["max_body_bytes"]
        ):
            raise DataValidationError("cumulative intake budget exceeded")

    def next_request(self):
        if any(result is None or result["status"] not in GOOD for _, _, result in self.records):
            raise DataValidationError("prior incomplete or blocked intake must not be retried")
        return self.requests[len(self.records)] if len(self.records) < len(self.requests) else None

    def begin(self, request):
        if request != self.next_request():
            raise DataValidationError("wrong next intake request")
        if self.used_bytes + self.config["per_response_bytes"] > self.config["max_body_bytes"]:
            raise DataValidationError("insufficient intake body reservation")
        path = self.out / "attempts" / request["id"]
        intent = atomic_seal(
            path / "intent.json",
            {
                "at": now(),
                "identity": self.identity,
                "request": request,
                "reserved_bytes": self.config["per_response_bytes"],
            },
        )
        self.records.append((request, intent, None))
        return path, intent

    def finish(self, path, intent, raw, transport):
        if self.records[-1][1] != intent or self.records[-1][2] is not None:
            raise DataValidationError("no matching unfinished request")
        with (path / "response.body").open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        profile = (
            self.inspector(raw, intent["request"])
            if transport["transport_status"] == "received"
            else {"status": transport["transport_status"], "rows": None}
        )
        result = atomic_seal(
            path / "result.json",
            {
                **transport,
                **profile,
                **body_accounting(transport, self.config["per_response_bytes"]),
                "at": now(),
                "identity": self.identity,
                "intent_fingerprint": intent["fingerprint"],
                "artifacts": {
                    "response.body": {"sha256": _sha(path / "response.body"), "bytes": len(raw)}
                },
                "historical_pit_certified": False,
                "execution_authority": False,
                "complete_history": False,
            },
        )
        self.records[-1] = (self.records[-1][0], intent, result)
        self.check_budget()
        return result

    def summary(self):
        counts = Counter(r["status"] if r else "interrupted" for _, _, r in self.records)
        return {
            "requests_attempted": len(self.records),
            "requests_limit": len(self.requests),
            "status_counts": dict(counts),
            "charged_body_bytes": self.used_bytes,
            "returned_rows": sum(r.get("rows") or 0 for _, _, r in self.records if r),
            "scope_attempted": len(self.records) == len(self.requests),
            "historical_pit_certified": False,
            "execution_authority": False,
            "economic_paths_used": 0,
            "model_fits_used": 0,
        }


def acquire(journal, budget, client, *, gap_seconds):
    next_start = time.monotonic()
    stop = "request_scope_exhausted"
    while (request := journal.next_request()) is not None:
        if not budget.can_start():
            return "wakeup_checkpoint"
        budget.check(
            projected_bytes=journal.config["per_response_bytes"] + 65536,
            projected_memory=64 * 1024**2,
        )
        time.sleep(max(0, next_start - time.monotonic()))
        if not budget.can_start():
            return "wakeup_checkpoint"
        path, intent = journal.begin(request)
        next_start = time.monotonic() + gap_seconds
        raw, transport = client.fetch(request["parameters"], journal.config["per_response_bytes"])
        result = journal.finish(path, intent, raw, transport)
        if len(journal.records) % 25 == 0 or result["status"] not in GOOD:
            print(json.dumps(journal.summary()), flush=True)
        if result["status"] not in GOOD:
            return result["status"]
    return stop


def run(root, *, client_factory=WireClient):
    config, requests, identity = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise DataValidationError("intake requires clean pushed code")
    identity = canonical_payload_fingerprint({"contract": identity, "source_head": head})
    out = root / config["output"]
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        journal = Journal(out, config, requests, identity)
        if journal.next_request() is None:
            latest = sealed_read(out / "latest.json")
            report = sealed_read(out / latest["report_path"])
            if report["fingerprint"] != latest["report_fingerprint"]:
                raise DataValidationError("intake report pointer changed")
            return report
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        budget = Budget(out, config["resources"])
        run_path = out / "runs" / uuid.uuid4().hex
        atomic_seal(
            run_path / "started.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "requests_before": len(journal.records),
                "code_inputs": binding.entries,
            },
        )
        client = client_factory(token, config["endpoint"])
        try:
            # A resume observes a full quiet minute; no uncertain call is replayed.
            if journal.records:
                time.sleep(60.1)
            with budget.watchdog():
                stop = acquire(
                    journal, budget, client, gap_seconds=60.1 / config["requests_per_minute"]
                )
        except Exception as exc:
            stop = "runtime_or_budget_error"
            atomic_seal(run_path / "failed.json", {"at": now(), "error_type": type(exc).__name__})
        finally:
            client.close()
        binding.check()
        load_contract(root)
        report = atomic_seal(
            run_path / "report.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "contract_sha256": _sha(root / CONFIG),
                "stop_reason": stop,
                **journal.summary(),
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )
        replace_status(
            out / "latest.json",
            {
                "report_path": (run_path / "report.json").relative_to(out).as_posix(),
                "report_fingerprint": report["fingerprint"],
                "identity": identity,
            },
        )
        return report
