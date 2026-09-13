"""One bounded financing-balance intake; no signal, label, or eligibility inference."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from datetime import datetime

from quantlab.data.dividend_raw import WireClient, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire, now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/f7_margin_intake_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/f7_margin_intake"
FIELDS = ("ts_code", "trade_date", "rzye")
CONFIG_FINGERPRINT = "95355eeee7b2f1fded92b74aa6f5681d3ffa40fe480a7fbc1a908fb91d42a765"


def requests_for(codes):
    if (
        not codes
        or codes != sorted(set(codes))
        or any(not isinstance(c, str) or re.fullmatch(r"\d{6}\.(SH|SZ)", c) is None for c in codes)
    ):
        raise DataValidationError("margin cohort must be unique ordered SH/SZ identifiers")
    return [
        {
            "id": "margin_" + code,
            "row_cap": 6000,
            "parameters": {
                "api_name": "margin_detail",
                "params": {"ts_code": code, "start_date": "20191224", "end_date": "20241230"},
                "fields": ",".join(FIELDS),
            },
        }
        for code in codes
    ]


def profile(raw, request, *, allowed_dates=None):
    try:
        obj = strict_json(raw)
        if not isinstance(obj, dict) or type(obj.get("code")) is not int:
            raise ValueError
        if obj["code"] != 0:
            message = str(obj.get("msg") or "").lower()
            permission = obj["code"] == 2002 or any(
                k in message for k in ("权限", "积分", "token", "permission", "授权")
            )
            return {
                "status": "permission_error" if permission else "provider_error",
                "server_code": obj["code"],
                "rows": None,
            }
        data = obj["data"]
        fields, items = data["fields"], data["items"]
        if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
            raise ValueError
        if (
            len(set(fields)) != len(fields)
            or set(fields) != set(FIELDS)
            or not isinstance(items, list)
        ):
            raise ValueError
        keys, nulls, negatives = [], 0, 0
        params = request["parameters"]["params"]
        for values in items:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError
            row = dict(zip(fields, values, strict=True))
            day = row["trade_date"]
            if (
                row["ts_code"] != params["ts_code"]
                or not isinstance(day, str)
                or re.fullmatch(r"\d{8}", day) is None
                or not params["start_date"] <= day <= params["end_date"]
                or (allowed_dates is not None and day not in allowed_dates)
            ):
                raise ValueError
            datetime.strptime(day, "%Y%m%d")
            keys.append((row["ts_code"], day))
            value = row["rzye"]
            if value is None:
                nulls += 1
            elif type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError
            else:
                negatives += value < 0
        if len(keys) != len(set(keys)):
            return {
                "status": "duplicate_keys",
                "server_code": 0,
                "rows": len(items),
                "duplicate_key_rows": len(keys) - len(set(keys)),
            }
        saturated = len(items) >= request["row_cap"]
        for container in (obj, data):
            total = container.get("total")
            if total is not None and (type(total) is not int or total < len(items)):
                raise ValueError
            saturated |= bool(container.get("has_more") or container.get("truncated"))
            saturated |= total is not None and total > len(items)
        return {
            "status": "saturated" if saturated else "nonempty" if items else "empty",
            "server_code": 0,
            "rows": len(items),
            "returned_fields": fields,
            "null_rzye_rows": nulls,
            "negative_rzye_rows": negatives,
            "duplicate_key_rows": 0,
            "raw_unit": "CNY",
            "date_range": [min(d for _, d in keys), max(d for _, d in keys)] if keys else None,
            "historical_pit_certified": False,
            "margin_target_eligibility_certified": False,
        }
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        return {"status": "schema_error", "rows": None}


def load_contract(root):
    config = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    if canonical_payload_fingerprint(config) != CONFIG_FINGERPRINT:
        raise DataValidationError("fixed margin source contract changed")
    verify_entries(root, config["inputs"])
    for path, entry in config["inputs"].items():
        if (root / path).stat().st_size != entry["bytes"]:
            raise DataValidationError("margin source byte length changed")
    selection = sealed_read(root / config["selection_path"])
    calendar = sealed_read(root / config["calendar_path"])
    document = sealed_read(root / config["documentation_path"])
    if any(
        x["fingerprint"] != config[key + "_fingerprint"]
        for key, x in (
            ("selection", selection),
            ("calendar", calendar),
            ("documentation", document),
        )
    ):
        raise DataValidationError("margin prerequisite proof changed")
    days = calendar["sessions"]
    source_days = days[days.index("2019-12-24") : days.index("2024-12-30") + 1]
    if (
        selection["instrument_ids"] != config["codes"]
        or len(config["codes"]) != 256
        or (config["source_dates"] != source_days or len(source_days) != 1217)
    ):
        raise DataValidationError("margin original cohort/calendar changed")
    return config


def run(root, *, client_factory=WireClient):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("margin source must be pushed")
    config = load_contract(root)
    identity = canonical_payload_fingerprint(
        {"source_head": head, "config_sha256": _sha(root / CONFIG)}
    )
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("margin intake segment already consumed")
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        budget = Budget(out, config["resources"])
        budget.check()
        dates = frozenset(d.replace("-", "") for d in config["source_dates"])
        journal = Journal(
            out,
            config,
            requests_for(config["codes"]),
            identity,
            inspector=lambda raw, request: profile(raw, request, allowed_dates=dates),
        )
        atomic_seal(
            out / "started.json",
            {
                "at": now(),
                "identity": identity,
                "source_head": head,
                "code_inputs": binding.entries,
                "actual_segment_attempt": 1,
            },
        )
        client = client_factory(token, config["endpoint"])
        try:
            with budget.watchdog():
                stop = acquire(
                    journal, budget, client, gap_seconds=60.1 / config["requests_per_minute"]
                )
        except Exception as exc:
            stop = "runtime_or_budget_error"
            atomic_seal(out / "failed.json", {"at": now(), "error_type": type(exc).__name__})
        finally:
            client.close()
        binding.check()
        load_contract(root)
        return atomic_seal(
            out / "report.json",
            {
                "at": now(),
                "source_head": head,
                "identity": identity,
                "config_sha256": _sha(root / CONFIG),
                "stop_reason": stop,
                **journal.summary(),
                "actual_segment_attempts": 1,
                "complete_history": False,
                "strategy_input_eligible": False,
                "performance_evidence": False,
                "canonical_writes": False,
                "funding_identities_used": 0,
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )
