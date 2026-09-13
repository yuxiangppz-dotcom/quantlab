"""Canonical-unit observations for a future replay, without tradability inference."""

from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, localcontext

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.quantity_kernel import ResearchSession
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG = "config/stock_replay_inputs_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/stock_replay_inputs"
KEYS = ["instrument_id", "trade_date"]


def exact_units(value, scale, *, positive=False):
    """Canonical values already use CNY/shares; no second provider-unit conversion."""
    if type(scale) is not int or scale not in (1, 100):
        raise DataValidationError("only canonical shares or CNY-to-fen scales supported")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            number = Decimal(str(value)) * scale
            if not number.is_finite() or number != number.to_integral_value():
                return None
            if not (0 < number <= 10**15 if positive else 0 <= number <= 10**15):
                return None
            return int(number)
    except InvalidOperation:
        return None


def canonical_bar(row):
    names = ("open", "low", "high", "close")
    prices = {k: exact_units(row.get(k), 100, positive=True) for k in names}
    valid = all(x is not None for x in prices.values())
    if valid:
        valid = (
            prices["low"] <= prices["open"] <= prices["high"]
            and prices["low"] <= prices["close"] <= prices["high"]
        )
    return {
        **{f"{k}_fen": v if valid else None for k, v in prices.items()},
        "amount_fen": exact_units(row.get("amount"), 100),
        "volume_shares": exact_units(row.get("volume"), 1),
        "ohlc_valid": valid,
    }


def prior20_amount(rows, dates, decision_date):
    if (
        type(dates) is not tuple
        or len(dates) != 20
        or any(type(d) is not date for d in dates)
        or tuple(sorted(set(dates))) != dates
        or dates[-1] != decision_date
    ):
        raise DataValidationError("ADV requires 20 explicit ordered sessions ending at decision")
    if set(rows) - set(dates):
        raise DataValidationError("ADV contains a date outside the prior decision window")
    values = {day: exact_units(rows.get(day), 100) for day in dates}
    missing = tuple(day for day, value in values.items() if value is None)
    mean = None if missing else sum(values.values()) // 20
    return mean, missing


def partition(path, day, *, exception_table=False, columns=None):
    frame = pd.read_parquet(path, columns=columns, use_threads=False)
    key = [*KEYS, "source_record_id"] if exception_table else KEYS
    if frame[key].isna().any().any() or frame.duplicated(key).any():
        raise DataValidationError("duplicate or missing source partition key")
    if not pd.to_datetime(frame.trade_date).eq(pd.Timestamp(day)).all():
        raise DataValidationError("source partition contains another date")
    return frame


def observation(code, execution, following, decision, prior_dates, amounts, bar, limits, score):
    if not decision < execution < following:
        raise DataValidationError("decision/execution/following chronology invalid")
    if any(row.get("instrument_id", code) != code for row in (bar, limits)):
        raise DataValidationError("source row belongs to another instrument")
    raw = canonical_bar(bar)
    adv, missing = prior20_amount(amounts, prior_dates, decision)
    up = exact_units(limits.get("up_limit"), 100, positive=True)
    down = exact_units(limits.get("down_limit"), 100, positive=True)
    limits_consistent = (
        raw["ohlc_valid"]
        and up is not None
        and down is not None
        and down <= raw["low_fen"] <= raw["high_fen"] <= up
    )
    if type(score) not in (int, float) or not math.isfinite(score):
        score = None
    context = ResearchSession(
        instrument_id=code,
        execution_date=execution,
        next_session=following,
        evidence_date=execution,
        calendar_verified=True,
        market_open=None,
        corporate_actions_processed=None,
        raw_close_fen=raw["close_fen"],
        low_fen=raw["low_fen"],
        high_fen=raw["high_fen"],
        down_limit_fen=down,
        up_limit_fen=up,
        prior20_amount_fen=adv,
        prior20_asof=decision,
        prior20_sessions=20 - len(missing),
        session_amount_fen=raw["amount_fen"],
        session_volume_shares=raw["volume_shares"],
        participation=None,
        rules=None,
        fees=None,
    )
    return {
        "instrument_id": code,
        "copied_S4_A": score,
        "signal_date": decision.isoformat(),
        "session": asdict(context),
        "raw_bar": raw,
        "bar_limit_relation_valid": bool(limits_consistent),
        "prior20_unknown_dates": [x.isoformat() for x in missing],
        "adv_rounding": "floor exact mean to integer fen",
        "execution_authority": False,
        "historical_performance_eligible": False,
    }


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "stock_replay_inputs_v1",
        "output": OUTPUT,
        "decision_date": "2021-12-31",
        "execution_date": "2022-01-04",
        "next_session": "2022-01-05",
        "expected_cohort": 256,
        "max_actual_attempts": 1,
        "provider_calls": 0,
        "economic_paths": 0,
        "selection_fingerprint": "04a36754511be4136685a3ce378d60aa8c746ca218da7fa66d62df23b2e5840e",
        "parent_report_fingerprint": (
            "a8b37fff1b8068bf4a01af44ee0fb4e83cf2f988d938f589ea92e588c86e0b03"
        ),
    }
    if any(config.get(k) != v or type(config.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("fixed S4 input bridge scope changed")
    verify_entries(root, config["inputs"])
    for name, entry in config["inputs"].items():
        if (root / name).stat().st_size != entry["bytes"]:
            raise DataValidationError("input byte length changed")
    calendar = sealed_read(root / config["calendar"])["sessions"]
    i = calendar.index(config["decision_date"])
    dates = calendar[i - 19 : i + 1]
    expected_paths = [
        f"data/canonical/daily/year={d[:4]}/month={d[5:7]}/{d}.parquet" for d in dates
    ]
    expected_context = {
        name: f"data/canonical/{kind}/year=2022/month=01/2022-01-04.parquet"
        for name, kind in (
            ("daily", "daily"),
            ("limits", "daily_price_limit"),
            ("st", "lifecycle_context_v1/stock_st"),
            ("suspensions", "lifecycle_context_v1/suspensions"),
        )
    }
    base = "data/products/research_program/launch_20260912/"
    expected_saved = {
        "selection": base + "selection.json",
        "calendar": base + "funding_inputs.json",
        "signals": base + "s4_conditional_reversal/signals.parquet",
        "parent_report": base + "s4_conditional_reversal/report.json",
        "parent_proof": base + "s4_conditional_reversal/independent_proof.json",
    }
    if (
        dates != config["prior20_dates"]
        or expected_paths != config["prior20_paths"]
        or calendar[i + 1 : i + 3] != [config["execution_date"], config["next_session"]]
        or config["context_paths"] != expected_context
        or any(config[k] != v for k, v in expected_saved.items())
        or set(config["inputs"])
        != set(expected_paths + list(expected_context.values()) + list(expected_saved.values()))
    ):
        raise DataValidationError("fixed decision/ADV/execution calendar changed")
    selection = sealed_read(root / config["selection"])
    parent = sealed_read(root / config["parent_report"])
    proof = sealed_read(root / config["parent_proof"])
    if (
        selection["fingerprint"] != config["selection_fingerprint"]
        or parent["fingerprint"] != config["parent_report_fingerprint"]
        or proof["report_fingerprint"] != parent["fingerprint"]
        or config["inputs"][config["signals"]] != parent["artifacts"]["signals.parquet"]
        or any(parent[k] is not False for k in ("performance_evidence", "execution_authority"))
    ):
        raise DataValidationError("saved signal/selection evidence mismatch")
    codes = selection["instrument_ids"]
    if len(codes) != 256 or sorted(set(codes)) != codes:
        raise DataValidationError("fixed 256 code grid changed")
    return config, codes


def run(root):
    config, codes = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("input bridge requires pushed source")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("input bridge actual attempt already consumed")
        budget = Budget(out, config["resources"])
        budget.check(projected_bytes=4 * 1024**2, projected_memory=128 * 1024**2)
        intent = atomic_seal(
            out / "started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "config_sha256": _sha(root / CONFIG),
                "code_inputs": binding.entries,
            },
        )
        try:
            with budget.watchdog():
                decision, execution, following = (
                    date.fromisoformat(config[k])
                    for k in ("decision_date", "execution_date", "next_session")
                )
                dates = tuple(map(date.fromisoformat, config["prior20_dates"]))
                amounts = {code: {} for code in codes}
                for day, path in zip(dates, config["prior20_paths"], strict=True):
                    frame = partition(root / path, day, columns=[*KEYS, "amount"])
                    for row in frame[frame.instrument_id.isin(codes)].to_dict("records"):
                        amounts[row["instrument_id"]][day] = row["amount"]
                context = {
                    name: partition(
                        root / path, execution, exception_table=name in {"st", "suspensions"}
                    )
                    for name, path in config["context_paths"].items()
                }
                signal = pd.read_parquet(
                    root / config["signals"],
                    columns=[*KEYS, "S4-A"],
                    filters=[("trade_date", "=", pd.Timestamp(decision))],
                    use_threads=False,
                )
                if (
                    len(signal) != 256
                    or signal[KEYS].isna().any().any()
                    or signal.duplicated(KEYS).any()
                    or sorted(signal.instrument_id) != codes
                    or not pd.to_datetime(signal.trade_date).eq(pd.Timestamp(decision)).all()
                ):
                    raise DataValidationError("saved signal date/grid differs from contract")
                scores = signal.set_index("instrument_id")["S4-A"].to_dict()
                keyed = {
                    k: v.set_index("instrument_id").to_dict("index")
                    for k, v in context.items()
                    if k in {"daily", "limits"}
                }
                rows = []
                for code in codes:
                    row = observation(
                        code,
                        execution,
                        following,
                        decision,
                        dates,
                        amounts[code],
                        keyed["daily"].get(code, {}),
                        keyed["limits"].get(code, {}),
                        scores[code],
                    )
                    for name in ("st", "suspensions"):
                        selected = context[name][context[name].instrument_id.eq(code)].astype(
                            object
                        )
                        row[name + "_source_records"] = selected.where(
                            pd.notna(selected), None
                        ).to_dict("records")
                    rows.append(row)
                table = atomic_seal(out / "observations.json", {"rows": rows})
                binding.check()
                verify_entries(root, config["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "observations_fingerprint": table["fingerprint"],
                        "counts": {
                            "grid": len(rows),
                            "copied_signal_known": sum(r["copied_S4_A"] is not None for r in rows),
                            "valid_raw_ohlc": sum(r["raw_bar"]["ohlc_valid"] for r in rows),
                            "complete_adv20": sum(
                                r["session"]["prior20_amount_fen"] is not None for r in rows
                            ),
                            "consistent_bar_limits": sum(
                                r["bar_limit_relation_valid"] for r in rows
                            ),
                            "st_codes": sum(bool(r["st_source_records"]) for r in rows),
                            "suspension_codes": sum(
                                bool(r["suspensions_source_records"]) for r in rows
                            ),
                            "tradability_certified": 0,
                            "corporate_processing_certified": 0,
                        },
                        "provider_calls": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "execution_authority": False,
                        "performance_evidence": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes": budget.check(),
                        },
                    },
                )
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {"error_type": type(exc).__name__, "intent_fingerprint": intent["fingerprint"]},
            )
            raise
