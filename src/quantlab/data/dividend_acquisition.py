"""Durable bounded dividend acquisition; no canonical or financial-state writes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from datetime import UTC, datetime

from quantlab.data.dividend_raw import FATAL, FIELDS, TRANSIENT, WireClient, inspect_response
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/dividend_acquisition_v1.json"
OUTPUT = "data/products/corporate_action_staging/dividend_targets_20260911"


def now():
    return datetime.now(UTC).isoformat()


def replace_status(path, payload):
    """Only the derived progress pointer is replaceable; facts stay exclusive."""
    pending = path.with_name(f"{path.name}.pending-{uuid.uuid4().hex}")
    atomic_seal(pending, payload)
    os.replace(pending, path)


def load_contract(root):
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    verify_entries(root, config["inputs"])
    request = sealed_read(root / config["request_path"])
    if request["fingerprint"] != config["request_fingerprint"]:
        raise DataValidationError("dividend request identity changed")
    codes = request["instrument_ids"]
    if (
        codes != sorted(set(codes))
        or len(codes) != config["instrument_count"]
        or any(re.fullmatch(r"\d{6}\.(SH|SZ)", code) is None for code in codes)
    ):
        raise DataValidationError("invalid fixed dividend request set")
    if config["fields"] != list(FIELDS) or config["endpoint"] != "https://api.tushare.pro":
        raise DataValidationError("unreviewed dividend protocol")
    return config, codes


def parameters(code, config):
    return {
        "api_name": "dividend",
        "params": {"ts_code": code},
        "fields": ",".join(config["fields"]),
    }


class Journal:
    def __init__(self, out, config, codes, identity):
        self.out, self.config, self.codes, self.identity = out, config, codes, identity
        self.records = {code: [] for code in codes}
        self.used_bytes = 0
        self.attempts = 0
        self.retries = 0
        self.latest_time = None
        attempts = out / "attempts"
        if attempts.exists():
            for folder in sorted(attempts.iterdir()):
                if not folder.is_dir() or folder.name not in self.records:
                    raise DataValidationError("unknown dividend attempt directory")
                for number, entry in enumerate(sorted(folder.iterdir()), 1):
                    if not entry.is_dir() or entry.name != f"{number:02d}" or number > 2:
                        raise DataValidationError("invalid dividend attempt sequence")
                    intent = sealed_read(entry / "intent.json")
                    if (
                        intent["identity"] != identity
                        or intent["instrument_id"] != folder.name
                        or intent["attempt"] != number
                        or intent["parameters"] != parameters(folder.name, config)
                        or intent["reserved_body_bytes"] != config["per_response_bytes"]
                    ):
                        raise DataValidationError("dividend intent changed")
                    at = datetime.fromisoformat(intent["at"])
                    if at.utcoffset() is None:
                        raise DataValidationError("unknown request time")
                    self.latest_time = max(self.latest_time or at, at)
                    result = None
                    if (entry / "result.json").exists():
                        result = sealed_read(entry / "result.json")
                        if (
                            result["intent_fingerprint"] != intent["fingerprint"]
                            or result["identity"] != identity
                            or not 0 <= result["received_bytes"] <= config["per_response_bytes"]
                        ):
                            raise DataValidationError("dividend result changed")
                        verify_entries(entry, result["artifacts"])
                        if any(
                            result.get(flag) is not False
                            for flag in (
                                "complete_event_history_certified",
                                "execution_authority",
                                "historical_pit_certified",
                            )
                        ):
                            raise DataValidationError("raw receipt gained financial authority")
                        if not result["raw_redacted"] and (
                            (entry / "response.body").stat().st_size != result["received_bytes"]
                            or _sha(entry / "response.body") != result["wire_sha256"]
                        ):
                            raise DataValidationError("raw wire receipt no longer matches body")
                    if number > 1:
                        previous = self.records[folder.name][-1][1]
                        if previous is None or previous["status"] not in TRANSIENT:
                            raise DataValidationError("persisted retry violates retry policy")
                    self._add(folder.name, intent, result)
        if (
            self.attempts > len(codes) + config["max_retries"]
            or self.retries > config["max_retries"]
        ):
            raise DataValidationError("cumulative dividend call budget exceeded")

    def _add(self, code, intent, result):
        self.records[code].append((intent, result))
        self.attempts += 1
        self.retries += intent["attempt"] > 1
        self.used_bytes += (
            result["received_bytes"] if result is not None else intent["reserved_body_bytes"]
        )

    def next_code(self):
        for code, attempts in self.records.items():
            if not attempts:
                return code
        if self.retries < self.config["max_retries"]:
            for code, attempts in self.records.items():
                if len(attempts) == 1 and attempts[0][1] is not None:
                    if attempts[0][1]["status"] in TRANSIENT:
                        return code
        return None

    def begin(self, code):
        number = len(self.records[code]) + 1
        if code != self.next_code():
            raise DataValidationError("request order or retry policy changed")
        if self.used_bytes + self.config["per_response_bytes"] > self.config["max_body_bytes"]:
            raise DataValidationError("cumulative dividend body budget exhausted")
        path = self.out / "attempts" / code / f"{number:02d}"
        intent = atomic_seal(
            path / "intent.json",
            {
                "identity": self.identity,
                "instrument_id": code,
                "attempt": number,
                "at": now(),
                "parameters": parameters(code, self.config),
                "reserved_body_bytes": self.config["per_response_bytes"],
            },
        )
        self._add(code, intent, None)
        return path, intent

    def finish(self, path, intent, raw, transport):
        if len(raw) > self.config["per_response_bytes"]:
            raise DataValidationError("raw response exceeds reserved budget")
        with (path / "response.body").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if transport["transport_status"] == "received":
            result = inspect_response(
                raw, intent["instrument_id"], self.config["fields"], self.config["row_cap"]
            )
        else:
            result = {"status": transport["transport_status"], "rows": None}
        result = atomic_seal(
            path / "result.json",
            {
                **transport,
                **result,
                "identity": self.identity,
                "observed_at": now(),
                "intent_fingerprint": intent["fingerprint"],
                "artifacts": {
                    "response.body": {"sha256": _sha(path / "response.body"), "bytes": len(raw)}
                },
                "complete_event_history_certified": False,
                "execution_authority": False,
                "historical_pit_certified": False,
            },
        )
        code = intent["instrument_id"]
        self.records[code][-1] = (intent, result)
        self.used_bytes += result["received_bytes"] - intent["reserved_body_bytes"]
        return result

    def summary(self):
        states, total_rows, per_code = Counter(), 0, []
        profile_counts = {
            key: Counter() for key in ("null_counts", "malformed_counts", "status_counts")
        }
        observation_totals = Counter()
        for code, attempts in self.records.items():
            latest = attempts[-1][1] if attempts else None
            status = (
                latest["status"] if latest else "interrupted_unknown" if attempts else "pending"
            )
            states[status] += 1
            rows = latest.get("rows") if latest else None
            total_rows += rows or 0
            if latest and "profile" in latest:
                profile = latest["profile"]
                for key, counts in profile_counts.items():
                    counts.update(profile[key])
                for key in (
                    "exact_duplicate_rows",
                    "candidate_identity_conflicts",
                    "rows_with_date_in_required_window",
                    "rows_without_valid_event_date",
                    "implemented_positive_cash_without_valid_pay_date",
                ):
                    observation_totals[key] += profile[key]
            per_code.append(
                {"instrument_id": code, "status": status, "attempts": len(attempts), "rows": rows}
            )
        return {
            "instrument_count": len(self.codes),
            "status_counts": dict(states),
            "attempts": self.attempts,
            "retries": self.retries,
            "charged_body_bytes": self.used_bytes,
            "returned_rows": total_rows,
            "per_code": per_code,
            "observation_profile": {
                **{key: dict(value) for key, value in profile_counts.items()},
                **dict(observation_totals),
            },
            "complete_event_history_certified": False,
            "historical_pit_certified": False,
            "cashflow_eligible": False,
            "performance_evidence": False,
            "execution_authority": False,
        }


def run(root, *, client_factory=WireClient):
    config, codes = load_contract(root)
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if status.strip() or head != upstream:
        raise DataValidationError("dividend acquisition must start from clean pushed code")
    binding = InputBinding(root)
    if code_binding(root, binding) != head:
        raise DataValidationError("dividend code changed during preflight")
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise DataValidationError("Tushare credential is unavailable")
    out = root / OUTPUT
    identity = canonical_payload_fingerprint({"contract_sha256": _sha(root / CONFIG)})
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        budget = Budget(out, config["resources"])
        journal = Journal(out, config, codes, identity)
        # Prior fatal outcomes remain a blocker; a wakeup cannot bypass them.
        fatal = [
            r["status"]
            for rows in journal.records.values()
            for _, r in rows
            if r and r["status"] in FATAL
        ]
        if fatal:
            raise DataValidationError(f"prior acquisition blocker: {fatal[0]}")
        run_path = out / "runs" / uuid.uuid4().hex
        atomic_seal(
            run_path / "started.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "attempts_before": journal.attempts,
            },
        )
        client = client_factory(token, config["endpoint"])
        stop_reason = "request_scope_exhausted"
        transient_streak = 0
        minimum_gap = 60.1 / config["requests_per_minute"]
        next_start = time.monotonic()
        if journal.latest_time:
            # Full quiet minute handles restarts and uncertain wall-clock drift safely.
            next_start += 60.1
        try:
            with budget.watchdog():
                while (code := journal.next_code()) is not None:
                    if not budget.can_start():
                        stop_reason = "wakeup_checkpoint"
                        break
                    if journal.attempts % 100 == 0:
                        budget.check(
                            projected_bytes=100 * (config["per_response_bytes"] + 16384)
                            + 16 * 1024**2,
                            projected_memory=64 * 1024**2,
                        )
                    if shutil.disk_usage(budget.disk_root).free < (
                        config["resources"]["reserve_host_D_bytes"]
                        + config["per_response_bytes"]
                        + 16 * 1024**2
                    ):
                        stop_reason = "host_disk_reserve"
                        break
                    delay = max(0, next_start - time.monotonic())
                    if delay:
                        time.sleep(delay)
                    if not budget.can_start():
                        stop_reason = "wakeup_checkpoint"
                        break
                    path, intent = journal.begin(code)
                    next_start = time.monotonic() + minimum_gap
                    raw, transport = client.fetch(
                        intent["parameters"], config["per_response_bytes"]
                    )
                    result = journal.finish(path, intent, raw, transport)
                    transient_streak = transient_streak + 1 if result["status"] in TRANSIENT else 0
                    if result["status"] == "rate_limited":
                        next_start = time.monotonic() + 60.1
                    if journal.attempts % 100 == 0 or result["status"] not in ("nonempty", "empty"):
                        progress = {
                            "at": now(),
                            "identity": identity,
                            "running": True,
                            **journal.summary(),
                        }
                        replace_status(out / "progress.json", progress)
                        print(
                            json.dumps(
                                {
                                    k: progress[k]
                                    for k in ("attempts", "status_counts", "returned_rows")
                                }
                            ),
                            flush=True,
                        )
                    if result["status"] in FATAL or transient_streak >= 3:
                        stop_reason = result["status"]
                        break
        except Exception as exc:
            stop_reason = "runtime_or_budget_error"
            atomic_seal(
                run_path / "failed.json",
                {
                    "at": now(),
                    "identity": identity,
                    "error_type": type(exc).__name__,
                    "attempts": journal.attempts,
                },
            )
        finally:
            client.close()
        binding.check()
        load_contract(root)
        artifacts = {
            p.relative_to(out).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size}
            for p in sorted((out / "attempts").rglob("*"))
            if p.is_file()
        }
        report = atomic_seal(
            run_path / "report.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "contract_sha256": _sha(root / CONFIG),
                "artifacts": artifacts,
                "request_fingerprint": config["request_fingerprint"],
                "stop_reason": stop_reason,
                "initial_scope_attempted": all(journal.records.values()),
                **journal.summary(),
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "bytes": budget.check(),
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
        replace_status(
            out / "progress.json",
            {"at": now(), "identity": identity, "running": False, **journal.summary()},
        )
        return report
