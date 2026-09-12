"""Bounded ETF input profiling, with no implicit historical portfolio eligibility."""

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

CONFIG = "config/etf_intake_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/etf_intake"
CODES = ("159915.SZ", "510300.SH", "510500.SH", "511010.SH", "518880.SH")
FIELDS = {
    "fund_daily": "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
    "fund_adj": "ts_code,trade_date,adj_factor",
    "fund_div": (
        "ts_code,ann_date,imp_anndate,base_date,div_proc,record_date,ex_date,pay_date,"
        "earpay_date,net_ex_date,div_cash,base_unit,ear_distr,ear_amount,account_date,base_year"
    ),
    "fund_share": "ts_code,trade_date,fd_share",
}
CAPS = {"fund_daily": 5000, "fund_adj": 2000, "fund_div": 1000, "fund_share": 2000}
UNITS = {
    "fund_daily": {"vol": "hands", "amount": "thousand_CNY", "OHLC": "CNY_per_share"},
    "fund_adj": {"adj_factor": "dimensionless"},
    "fund_div": {"div_cash": "CNY_per_share", "base_unit": "ten_thousand_shares"},
    "fund_share": {"fd_share": "ten_thousand_shares"},
}
NUMERIC = {
    "fund_daily": ("open", "high", "low", "close", "pre_close", "vol", "amount"),
    "fund_adj": ("adj_factor",),
    "fund_div": ("div_cash", "base_unit", "ear_distr", "ear_amount"),
    "fund_share": ("fd_share",),
}


def requests_for():
    requests = []
    for api, fields in FIELDS.items():
        for code in CODES:
            params = {"ts_code": code}
            if api != "fund_div":
                params.update(start_date="20191001", end_date="20241231")
            requests.append(
                {
                    "id": f"{api}_{code}",
                    "row_cap": CAPS[api],
                    "parameters": {"api_name": api, "params": params, "fields": fields},
                }
            )
    return requests


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def profile(raw, request):
    """Keep missing events/units/versions unknown; raw invalid values are retained."""
    try:
        obj = strict_json(raw)
        if not isinstance(obj, dict) or type(obj.get("code")) is not int:
            raise ValueError
        if obj["code"] != 0:
            message = str(obj.get("msg") or "").lower()
            permission = obj["code"] == 2002 or any(
                word in message for word in ("权限", "积分", "token", "permission", "授权")
            )
            return {
                "status": "permission_error" if permission else "provider_error",
                "server_code": obj["code"],
                "rows": None,
            }
        api = request["parameters"]["api_name"]
        params = request["parameters"]["params"]
        data = obj["data"]
        names, items = data["fields"], data["items"]
        required = FIELDS[api].split(",")
        if (
            not isinstance(names, list)
            or not all(isinstance(n, str) for n in names)
            or len(names) != len(set(names))
            or not set(required).issubset(names)
            or not isinstance(items, list)
        ):
            raise ValueError
        rows, keys, days = [], [], []
        for item in items:
            if not isinstance(item, list) or len(item) != len(names):
                raise ValueError
            row = dict(zip(names, item, strict=True))
            if row["ts_code"] != params["ts_code"]:
                raise ValueError
            if api == "fund_div":
                for name in (
                    "ann_date",
                    "imp_anndate",
                    "base_date",
                    "record_date",
                    "ex_date",
                    "pay_date",
                    "earpay_date",
                    "net_ex_date",
                    "account_date",
                ):
                    value = row[name]
                    if value is not None and value != "":
                        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
                            raise ValueError
                        datetime.strptime(value, "%Y%m%d")
                keys.append(
                    tuple(
                        row[n] for n in ("ts_code", "ann_date", "ex_date", "pay_date", "div_proc")
                    )
                )
                if row["ann_date"]:
                    days.append(row["ann_date"])
            else:
                day = row["trade_date"]
                if (
                    not isinstance(day, str)
                    or len(day) != 8
                    or not day.isdigit()
                    or not params["start_date"] <= day <= params["end_date"]
                ):
                    raise ValueError
                datetime.strptime(day, "%Y%m%d")
                keys.append((row["ts_code"], day))
                days.append(day)
            rows.append(row)
        saturated = len(rows) >= request["row_cap"]
        for part in (obj, data):
            saturated |= bool(part.get("has_more") or part.get("truncated"))
            total = part.get("total")
            saturated |= numeric(total) and total > len(rows)
        return {
            "status": "saturated" if saturated else ("nonempty" if rows else "empty"),
            "server_code": 0,
            "rows": len(rows),
            "returned_fields": names,
            "duplicate_key_rows": len(keys) - len(set(keys)),
            "exact_duplicate_rows": len(rows)
            - len({json.dumps(r, sort_keys=True, allow_nan=False) for r in rows}),
            "null_counts": {n: sum(r[n] is None or r[n] == "" for r in rows) for n in required},
            "invalid_numeric_counts": {
                n: sum(not numeric(r[n]) for r in rows) for n in NUMERIC[api]
            },
            "nonpositive_numeric_counts": {
                n: sum(numeric(r[n]) and r[n] <= 0 for r in rows) for n in NUMERIC[api]
            },
            "date_range": [min(days), max(days)] if days else None,
            "date_range_field": "ann_date" if api == "fund_div" else "trade_date",
            "raw_units": UNITS[api],
            "complete_history": False,
        }
    except (ValueError, TypeError, KeyError, UnicodeError):
        return {"status": "schema_error", "rows": None}


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "quantlab_etf_intake_v1",
        "codes": list(CODES),
        "output": OUTPUT,
        "endpoint": "https://api.tushare.pro",
        "start_date": "20191001",
        "end_date": "20241231",
        "max_requests": 20,
        "max_retries": 0,
        "requests_per_minute": 30,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
        "funding_features_authorized": [],
        "canonical_writes_authorized": False,
        "execution_authority": False,
        "historical_pit_certified": False,
    }
    if any(config.get(k) != v for k, v in fixed.items()):
        raise DataValidationError("unreviewed ETF intake contract")
    verify_entries(root, config["inputs"])
    proof = sealed_read(
        root / "data/products/research_program/launch_20260912/intake_independent_proof.json"
    )
    if proof.get("historical_pit_certified") is not False:
        raise DataValidationError("ETF metadata proof authority changed")
    return config


def run(root, *, client_factory=WireClient):
    config = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("ETF intake requires clean pushed source")
    identity = canonical_payload_fingerprint(
        {"config_sha256": _sha(root / CONFIG), "source_head": head}
    )
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("ETF attempt consumed or output unexplained; no replay")
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        journal = Journal(out, config, requests_for(), identity, inspector=profile)
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=16 * 1024**2, projected_memory=128 * 1024**2)
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
                "identity": identity,
                "source_head": head,
                "config_sha256": _sha(root / CONFIG),
                "stop_reason": stop,
                **journal.summary(),
                "actual_segment_attempts": 1,
                "complete_history": False,
                "strategy_universe_eligible": False,
                "performance_evidence": False,
                "funding_features_used": [],
                "resources": {
                    "seconds": time.monotonic() - budget.started,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "generated_bytes": budget.check(),
                },
            },
        )
