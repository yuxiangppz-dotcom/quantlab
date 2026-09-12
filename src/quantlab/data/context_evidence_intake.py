"""Finite raw valuation/member/disclosure sample, separate from canonical data."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from datetime import datetime

from quantlab.data.dividend_raw import WireClient, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire, now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/context_evidence_intake_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/context_evidence_intake"
CODES = (
    "000004.SZ",
    "002024.SZ",
    "002517.SZ",
    "300022.SZ",
    "300558.SZ",
    "600303.SH",
    "600845.SH",
    "603127.SH",
)
DAYS = (
    "20191231",
    "20200102",
    "20201231",
    "20211231",
    "20220104",
    "20221230",
    "20230103",
    "20231229",
    "20240102",
    "20241231",
    "20260911",
)
FIELDS = {
    "daily_basic": (
        "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,pe,"
        "pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,free_share,"
        "total_mv,circ_mv"
    ),
    "index_member_all": (
        "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new"
    ),
    "forecast": (
        "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,"
        "net_profit_max,last_parent_net,first_ann_date,summary,change_reason"
    ),
    "express": (
        "ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,"
        "total_assets,total_hldr_eqy_exc_min_int,diluted_eps,diluted_roe,"
        "yoy_net_profit,bps,perf_summary,is_audit,remark"
    ),
}
CAPS = {"daily_basic": 6000, "index_member_all": 2000, "forecast": 3500, "express": 1000}
TEXT = {
    "ts_code",
    "trade_date",
    "ann_date",
    "end_date",
    "first_ann_date",
    "in_date",
    "out_date",
    "type",
    "summary",
    "change_reason",
    "perf_summary",
    "remark",
    "l1_code",
    "l1_name",
    "l2_code",
    "l2_name",
    "l3_code",
    "l3_name",
    "name",
    "is_new",
}
UNITS = {
    "daily_basic": (
        "raw:price CNY;turnover/dividend yield percent;shares10000shares;"
        "market value10000CNY;valuation multiples dimensionless"
    ),
    "index_member_all": (
        "classification identifiers and declared inclusion/exclusion dates;"
        " publication/vintage unknown"
    ),
    "forecast": (
        "profit min/max10000CNY;change limits percent;"
        "last_parent_net unit needs separate confirmation"
    ),
    "express": (
        "reported financial totals CNY;per-share values CNY;diluted_roe percent;"
        "no cashflow-quality inference"
    ),
}


def requests_for():
    requests = []
    for day in DAYS:
        requests.append(("daily_basic", f"basic_{day}", {"trade_date": day}))
    for code in CODES:
        for flag in ("Y", "N"):
            requests.append(
                ("index_member_all", f"member_{code}_{flag}", {"ts_code": code, "is_new": flag})
            )
    for api in ("forecast", "express"):
        for code in CODES:
            requests.append(
                (
                    api,
                    f"{api}_{code}",
                    {"ts_code": code, "start_date": "20200101", "end_date": "20241231"},
                )
            )
    return [
        {
            "id": name,
            "row_cap": CAPS[api],
            "parameters": {"api_name": api, "params": params, "fields": FIELDS[api]},
        }
        for api, name, params in requests
    ]


def _date(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError
    datetime.strptime(value, "%Y%m%d")
    return value


def profile(raw, request):
    try:
        obj = strict_json(raw)
        if not isinstance(obj, dict) or type(obj.get("code")) is not int:
            raise ValueError
        if obj["code"] != 0:
            message = str(obj.get("msg") or "").lower()
            permission = obj["code"] == 2002 or any(
                s in message for s in ("权限", "积分", "token", "permission", "授权")
            )
            return {
                "status": "permission_error" if permission else "provider_error",
                "server_code": obj["code"],
                "rows": None,
            }
        data = obj["data"]
        fields, items = data["fields"], data["items"]
        api = request["parameters"]["api_name"]
        params = request["parameters"]["params"]
        required = FIELDS[api].split(",")
        if (
            not isinstance(fields, list)
            or not all(isinstance(k, str) for k in fields)
            or len(set(fields)) != len(fields)
            or not set(required).issubset(fields)
            or not isinstance(items, list)
        ):
            raise ValueError
        rows, keys, dates = [], [], []
        for values in items:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError
            row = dict(zip(fields, values, strict=True))
            if not isinstance(row["ts_code"], str) or not row["ts_code"]:
                raise ValueError
            if api == "daily_basic":
                day = _date(row["trade_date"])
                if day != params["trade_date"]:
                    raise ValueError
                keys.append((row["ts_code"], day))
                dates.append(day)
            else:
                if row["ts_code"] != params["ts_code"]:
                    raise ValueError
                if api == "index_member_all":
                    if row["is_new"] != params["is_new"]:
                        raise ValueError
                    entry, exit_day = (
                        _date(row["in_date"], optional=True),
                        _date(row["out_date"], optional=True),
                    )
                    if entry and exit_day and entry > exit_day:
                        raise ValueError
                    dates.extend(d for d in (entry, exit_day) if d)
                    keys.append((row["ts_code"], row["l3_code"], entry, exit_day, row["is_new"]))
                else:
                    day = _date(row["ann_date"])
                    _date(row["end_date"], optional=True)
                    if not params["start_date"] <= day <= params["end_date"]:
                        raise ValueError
                    if api == "forecast":
                        _date(row["first_ann_date"], optional=True)
                    dates.append(day)
                    keys.append((row["ts_code"], day, row["end_date"]))
            for key in set(required) & TEXT:
                if row[key] is not None and not isinstance(row[key], str):
                    raise ValueError
            for key in set(required) - TEXT:
                value = row[key]
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                ):
                    raise ValueError
            rows.append(row)
        saturated = len(rows) >= request["row_cap"]
        for container in (obj, data):
            total = container.get("total")
            saturated |= bool(container.get("has_more") or container.get("truncated"))
            saturated |= isinstance(total, (int, float)) and total > len(rows)
        return {
            "status": "saturated" if saturated else "nonempty" if rows else "empty",
            "rows": len(rows),
            "server_code": 0,
            "returned_fields": fields,
            "duplicate_key_rows": len(keys) - len(set(keys)),
            "exact_duplicate_rows": len(rows)
            - len({canonical_payload_fingerprint(r) for r in rows}),
            "null_counts": {k: sum(row[k] in (None, "") for row in rows) for k in required},
            "date_range": [min(dates), max(dates)] if dates else None,
            "raw_units": UNITS[api],
            "complete_history": False,
            "historical_pit_certified": False,
            "strategy_input_eligible": False,
        }
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        return {"status": "schema_error", "rows": None}


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "context_evidence_intake_v1",
        "output": OUTPUT,
        "endpoint": "https://api.tushare.pro",
        "codes": list(CODES),
        "days": list(DAYS),
        "max_requests": 43,
        "max_retries": 0,
        "requests_per_minute": 100,
        "per_response_bytes": 2 * 1024**2,
        "max_body_bytes": 64 * 1024**2,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "canonical_writes_authorized": False,
        "execution_authority": False,
        "historical_pit_certified": False,
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed context-evidence contract")
    verify_entries(root, config["inputs"])
    parent = sealed_read(root / config["feasibility_report_path"])
    proof = sealed_read(root / config["feasibility_proof_path"])
    selection = sealed_read(root / config["selection_path"])
    if (
        proof["report_fingerprint"] != parent["fingerprint"]
        or tuple(selection["instrument_ids"][::32]) != CODES
    ):
        raise DataValidationError("context-evidence population or prerequisite changed")
    return config


def run(root, *, client_factory=WireClient):
    config = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("context intake requires clean pushed source")
    identity = canonical_payload_fingerprint(
        {"source_head": head, "config_sha256": _sha(root / CONFIG)}
    )
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("context intake segment already consumed; no restart")
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=16 * 1024**2, projected_memory=128 * 1024**2)
        journal = Journal(out, config, requests_for(), identity, inspector=profile)
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
                "new_formula_identities_used": 0,
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )
