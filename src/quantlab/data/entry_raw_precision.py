"""Bounded raw daily alternatives; no canonical repair or automatic evidence admission."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from decimal import Decimal
from fractions import Fraction

import pandas as pd

from quantlab.data.dividend_raw import WireClient, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire, now
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/s4_entry_raw_precision_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/s4_entry_raw_precision"
CONFIG_FINGERPRINT = "26beac86b0a29a352f13ad6aa87340ff6f98b106be0f7b41cf429191fc4c91f2"
FIELDS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")
SCALES = {"open": 100, "high": 100, "low": 100, "close": 100, "vol": 100, "amount": 100000}


def raw_units(value, scale, *, positive=False):
    if type(scale) is not int or scale not in (100, 100000):
        raise DataValidationError("raw daily scale must be explicit")
    if value is None:
        return None, "missing"
    if type(value) not in (int, Decimal):
        return None, "not_exact_raw_number"
    number = Decimal(value)
    if not number.is_finite():
        return None, "nonfinite"
    if abs(number.as_tuple().exponent) > 100 or number > Decimal(10**15) / scale:
        return None, "out_of_range"
    scaled = Fraction(number) * scale
    invalid_sign = scaled <= 0 if positive else scaled < 0
    if invalid_sign:
        return None, "invalid_sign"
    if scaled > 10**15:
        return None, "out_of_range"
    if scaled.denominator != 1:
        return None, "off_integer_grid"
    return scaled.numerator, None


def requests_for(keys):
    seen, result = set(), []
    for row in keys:
        code, day = row["instrument_id"], row["trade_date"]
        key = (code, day)
        if key in seen:
            raise DataValidationError("duplicate raw daily request")
        seen.add(key)
        compact = day.replace("-", "")
        result.append(
            {
                "id": f"daily_{code}_{compact}",
                "row_cap": 6000,
                "parameters": {
                    "api_name": "daily",
                    "params": {"ts_code": code, "start_date": compact, "end_date": compact},
                    "fields": ",".join(FIELDS),
                },
            }
        )
    return result


def decoded_rows(raw):
    strict_json(raw)  # Reject duplicate object keys and nonstandard JSON constants.
    obj = json.loads(raw, parse_float=Decimal)
    data = obj["data"]
    return [dict(zip(data["fields"], row, strict=True)) for row in data["items"]]


def profile(raw, request):
    try:
        strict_json(raw)
        obj = json.loads(raw, parse_float=Decimal)
        if not isinstance(obj, dict) or type(obj.get("code")) is not int:
            raise ValueError
        if obj["code"] != 0:
            msg = str(obj.get("msg") or "").lower()
            permission = obj["code"] == 2002 or any(
                w in msg for w in ("token", "权限", "积分", "permission")
            )
            return {
                "status": "permission_error" if permission else "provider_error",
                "server_code": obj["code"],
                "rows": None,
            }
        data = obj["data"]
        names, items = data["fields"], data["items"]
        if (
            not isinstance(names, list)
            or any(type(x) is not str for x in names)
            or len(names) != len(FIELDS)
            or set(names) != set(FIELDS)
            or not isinstance(items, list)
        ):
            raise ValueError
        rows = []
        params = request["parameters"]["params"]
        for values in items:
            if not isinstance(values, list) or len(values) != len(FIELDS):
                raise ValueError
            r = dict(zip(names, values, strict=True))
            if r["ts_code"] != params["ts_code"] or r["trade_date"] != params["start_date"]:
                raise ValueError
            if any(
                v is not None and (type(v) not in (int, Decimal) or not Decimal(v).is_finite())
                for v in (r[k] for k in SCALES)
            ):
                raise ValueError
            rows.append(r)
        for container in (obj, data):
            total = container.get("total")
            if total is not None and (type(total) is not int or total < len(rows)):
                raise ValueError
            if (
                container.get("has_more")
                or container.get("truncated")
                or (total is not None and total > len(rows))
            ):
                return {"status": "saturated", "rows": len(rows)}
        if len(rows) > 1:
            return {"status": "duplicate_keys", "rows": len(rows)}
        unknowns = (
            {}
            if not rows
            else {
                k: reason
                for k, scale in SCALES.items()
                if (reason := raw_units(rows[0][k], scale, positive=k not in ("vol", "amount"))[1])
                is not None
            }
        )
        return {
            "status": "nonempty" if rows else "empty",
            "rows": len(rows),
            "server_code": 0,
            "unknown_units": unknowns,
            "historical_pit_certified": False,
            "execution_authority": False,
        }
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
        return {"status": "schema_error", "rows": None}


def reconcile_row(key, raw_row, canonical_row):
    result = {
        **key,
        "source_status": "nonempty" if raw_row is not None else "empty",
        "canonical_row_present": canonical_row is not None,
        "raw_normalized": {},
        "raw_number_text": {},
        "unknown_units": {},
        "comparisons": {},
        "automatically_admitted": False,
        "historical_pit_certified": False,
    }
    for field, scale in SCALES.items():
        value = None if raw_row is None else raw_row[field]
        parsed, reason = raw_units(value, scale, positive=field not in ("vol", "amount"))
        result["raw_normalized"][field] = parsed
        result["raw_number_text"][field] = None if value is None else str(value)
        result["unknown_units"][field] = reason
        canonical_field = "volume" if field == "vol" else field
        old = None if canonical_row is None else canonical_row.get(canonical_field)
        genuine = isinstance(old, (int, float)) and not isinstance(old, bool) and math.isfinite(old)
        canonical_scale = 1 if field == "vol" else 100
        exact = (
            None
            if not genuine or parsed is None
            else Fraction(str(old)) * canonical_scale == parsed
        )
        provider_multiplier = 100 if field == "vol" else 1000 if field == "amount" else 1
        float_agrees = (
            None if not genuine or value is None else float(value) * provider_multiplier == old
        )
        result["comparisons"][field] = {
            "saved_canonical_text": str(old) if genuine else None,
            "exact_value_equal": exact,
            "float_normalization_reproduces_saved": float_agrees,
        }
    prices = [result["raw_normalized"][k] for k in ("open", "high", "low", "close")]
    result["raw_ohlc_consistent"] = (
        None
        if any(p is None for p in prices)
        else (prices[2] <= prices[0] <= prices[1] and prices[2] <= prices[3] <= prices[1])
    )
    return result


def load_contract(root):
    c = json.loads((root / CONFIG).read_text(encoding="utf-8"))
    if canonical_payload_fingerprint(c) != CONFIG_FINGERPRINT:
        raise DataValidationError("fixed raw precision contract changed")
    verify_entries(root, c["inputs"])
    for path, entry in c["inputs"].items():
        if (root / path).stat().st_size != entry["bytes"]:
            raise DataValidationError("raw precision input byte length changed")
    saved = {k: sealed_read(root / path) for k, path in c["saved"].items()}
    if any(saved[k]["fingerprint"] != fp for k, fp in c["pins"].items()):
        raise DataValidationError("raw precision prerequisite changed")
    for label, name in (
        ("plan", "proof"),
        ("observations", "independent_proof"),
        ("liquidity", "independent_proof"),
    ):
        if (
            saved[label + "_" + name]["report_fingerprint"]
            != saved[label + "_report"]["fingerprint"]
        ):
            raise DataValidationError("raw precision parent proof mismatch")
    return c, saved


def run(root, *, client_factory=WireClient):
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("raw precision requires pushed code")
    c, saved = load_contract(root)
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("raw precision actual attempt consumed")
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            raise DataValidationError("Tushare credential unavailable")
        budget = Budget(out, c["resources"])
        budget.check()
        identity = canonical_payload_fingerprint(
            {"head": head, "config_sha256": _sha(root / CONFIG)}
        )
        journal = Journal(out, c, requests_for(c["keys"]), identity, inspector=profile)
        atomic_seal(
            out / "started.json",
            {
                "at": now(),
                "source_head": head,
                "identity": identity,
                "code_inputs": binding.entries,
                "attempt": 1,
            },
        )
        client = client_factory(token, c["endpoint"])
        try:
            with budget.watchdog():
                stop = acquire(journal, budget, client, gap_seconds=60.1 / c["requests_per_minute"])
                if stop != "request_scope_exhausted":
                    raise DataValidationError("raw precision acquisition did not complete")
                canonical = {}
                for day, path in c["canonical_paths"].items():
                    frame = pd.read_parquet(
                        root / path,
                        columns=[
                            "instrument_id",
                            "trade_date",
                            "open",
                            "high",
                            "low",
                            "close",
                            "volume",
                            "amount",
                        ],
                        use_threads=False,
                    )
                    if (
                        frame.instrument_id.isna().any()
                        or frame.instrument_id.duplicated().any()
                        or not pd.to_datetime(frame.trade_date).eq(pd.Timestamp(day)).all()
                    ):
                        raise DataValidationError("canonical source keys changed")
                    canonical[day] = {r["instrument_id"]: r for r in frame.to_dict("records")}
                rows = []
                for key, (request, _, _response) in zip(c["keys"], journal.records, strict=True):
                    raw = decoded_rows(
                        (out / "attempts" / request["id"] / "response.body").read_bytes()
                    )
                    rows.append(
                        reconcile_row(
                            key,
                            raw[0] if raw else None,
                            canonical[key["trade_date"]].get(key["instrument_id"]),
                        )
                    )
                projected = atomic_seal(
                    out / "reconciliation.json",
                    {"rows": rows, "source_head": head, "identity": identity},
                )
                proposals = [
                    {
                        "instrument_id": r["instrument_id"],
                        "proposal_rank": r["proposal_rank"],
                        "nominal_quantity": r["nominal_quantity"],
                        "original_required_unknowns": r["required_unknowns"],
                        "new_source_keys": [
                            k for k in c["keys"] if k["instrument_id"] == r["instrument_id"]
                        ],
                        "automatically_admitted": False,
                    }
                    for r in saved["plan_plan"]["rows"]
                    if r["selected_raw_proposal"]
                ]
                summary = atomic_seal(
                    out / "proposals.json",
                    {
                        "rows": proposals,
                        "parent_plan_fingerprint": saved["plan_plan"]["fingerprint"],
                        "economic_paths_started": 0,
                    },
                )
                binding.check()
                load_contract(root)
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": now(),
                        "source_head": head,
                        "identity": identity,
                        "stop_reason": stop,
                        **journal.summary(),
                        "reconciliation_fingerprint": projected["fingerprint"],
                        "proposals_fingerprint": summary["fingerprint"],
                        "canonical_writes": False,
                        "performance_evidence": False,
                        "candidate_promotion_eligible": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "generated_bytes": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {
                    "at": now(),
                    "error_type": type(exc).__name__,
                    "identity": identity,
                    **journal.summary(),
                },
            )
            raise
        finally:
            client.close()
