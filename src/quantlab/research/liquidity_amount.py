"""Conservative amount capacity bounds; never prices, account cash or repaired source data."""

import json
import subprocess
import time
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal, DecimalException

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.stock_replay_inputs import partition

CONFIG = "config/liquidity_amount_lower_bound_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/liquidity_amount_lower_bound"
OLD_OUTPUT = "data/products/research_program/launch_20260912/stock_replay_inputs/"
POLICY = "daily_canonical_CNY_decimal_floor_fen_then_prior20_floor_mean_v1"


def amount_floor(value):
    """Return (fen lower bound, was floored), relative only to the saved CNY observation."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None, False
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 10**13:
            return None, False
        parts = number.as_tuple()
        # Exact exponent shift avoids context rounding before the downward bound.
        scaled = Decimal((parts.sign, parts.digits, parts.exponent + 2))
        result = int(scaled.to_integral_value(rounding=ROUND_FLOOR))
        return result, scaled != result
    except (DecimalException, ValueError):
        return None, False


def adv_floor(rows, dates, decision):
    if (
        type(dates) is not tuple
        or len(dates) != 20
        or any(type(d) is not date for d in dates)
        or tuple(sorted(set(dates))) != dates
        or dates[-1] != decision
        or set(rows) - set(dates)
    ):
        raise DataValidationError("liquidity bounds require the complete fixed prior20 calendar")
    values = {d: amount_floor(rows.get(d)) for d in dates}
    unknown = [d.isoformat() for d, (n, _) in values.items() if n is None]
    floored = [d.isoformat() for d, (_, changed) in values.items() if changed]
    mean = None if unknown else sum(n for n, _ in values.values()) // 20
    return {
        "daily_floor_fen": {d.isoformat(): n for d, (n, _) in values.items()},
        "unknown_dates": unknown,
        "floored_dates": floored,
        "adv20_floor_fen": mean,
    }


def load_contract(root):
    c = json.loads((root / CONFIG).read_text())
    fixed = {
        "schema": "liquidity_amount_lower_bound_v1",
        "output": OUTPUT,
        "old_output": OLD_OUTPUT,
        "old_config": "config/stock_replay_inputs_v1.json",
        "decision_date": "2021-12-31",
        "provider_calls": 0,
        "economic_paths": 0,
        "model_fits": 0,
        "max_actual_attempts": 1,
    }
    if any(c.get(k) != v or type(c.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed liquidity projection contract")
    old = json.loads((root / c["old_config"]).read_text())
    wanted = set(
        c["paths"]
        + [c["old_config"]]
        + [
            OLD_OUTPUT + name
            for name in (
                "observations.json",
                "report.json",
                "independent_proof.json",
                "amount_precision_note.json",
            )
        ]
    )
    if (
        c["dates"] != old["prior20_dates"]
        or c["paths"] != old["prior20_paths"]
        or c["dates"][0] != "2021-12-06"
        or c["dates"][-1] != "2021-12-31"
        or len(c["dates"]) != 20
        or len(c["paths"]) != 20
        or set(c["inputs"]) != wanted
    ):
        raise DataValidationError("liquidity projection changed its bound original window")
    verify_entries(root, c["inputs"])
    if any((root / p).stat().st_size != e["bytes"] for p, e in c["inputs"].items()):
        raise DataValidationError("liquidity bound source bytes changed")
    saved = {name: sealed_read(root / OLD_OUTPUT / name) for name in c["fingerprints"]}
    if any(saved[n]["fingerprint"] != fp for n, fp in c["fingerprints"].items()):
        raise DataValidationError("old exact evidence changed")
    report, proof = saved["report.json"], saved["independent_proof.json"]
    if (
        report["fingerprint"] != "e8ea83c4503107b6a14de3e6b7ad30d7ad80fc4578c541d7bdfdf794b8df176d"
        or proof["report_fingerprint"] != report["fingerprint"]
        or report["observations_fingerprint"] != saved["observations.json"]["fingerprint"]
    ):
        raise DataValidationError("projection does not refer to the frozen exact input bridge")
    rows = saved["observations.json"]["rows"]
    codes = [r["instrument_id"] for r in rows]
    if len(codes) != 256 or codes != sorted(set(codes)):
        raise DataValidationError("fixed liquidity cohort changed")
    if c["resources"] != {
        "max_generated_bytes": 8 * 1024**2,
        "max_rss_bytes": 2 * 1024**3,
        "max_wakeup_seconds": 300,
        "next_partition_time_reserve_seconds": 30,
        "reserve_host_D_bytes": 8 * 1024**3,
    }:
        raise DataValidationError("liquidity resource limits changed")
    return c, rows


def run(root):
    c, old_rows = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("liquidity projection requires pushed code")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        if any(p.name != "worker.lock" for p in out.iterdir()):
            raise DataValidationError("liquidity projection attempt already consumed")
        budget = Budget(out, c["resources"])
        budget.check(projected_bytes=1024**2, projected_memory=128 * 1024**2)
        atomic_seal(
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
                amounts = {r["instrument_id"]: {} for r in old_rows}
                dates = tuple(date.fromisoformat(d) for d in c["dates"])
                for path, day in zip(c["paths"], dates, strict=True):
                    f = partition(
                        root / path, day, columns=["instrument_id", "trade_date", "amount"]
                    )
                    for row in f[f.instrument_id.isin(amounts)].to_dict("records"):
                        amounts[row["instrument_id"]][day] = row["amount"]
                rows = []
                for old in old_rows:
                    code = old["instrument_id"]
                    lower = adv_floor(amounts[code], dates, date(2021, 12, 31))
                    exact = old["session"]["prior20_amount_fen"]
                    if exact is not None and exact != lower["adv20_floor_fen"]:
                        raise DataValidationError(
                            "exact original ADV changed under lower-bound policy"
                        )
                    if set(old["prior20_unknown_dates"]) != set(
                        lower["unknown_dates"] + lower["floored_dates"]
                    ):
                        raise DataValidationError(
                            "projection differs for a reason other than downward amount flooring"
                        )
                    rows.append({"instrument_id": code, "copied_exact_adv20_fen": exact, **lower})
                table = atomic_seal(out / "projection.json", {"rows": rows})
                binding.check()
                verify_entries(root, c["inputs"])
                return atomic_seal(
                    out / "report.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "source_head": head,
                        "policy": POLICY,
                        "relative_to_saved_numeric_observations_only": True,
                        "projection_fingerprint": table["fingerprint"],
                        "old_report_fingerprint": c["fingerprints"]["report.json"],
                        "counts": {
                            "cohort": len(rows),
                            "exact_adv20_known": sum(
                                r["copied_exact_adv20_fen"] is not None for r in rows
                            ),
                            "lower_bound_adv20_known": sum(
                                r["adv20_floor_fen"] is not None for r in rows
                            ),
                            "fractional_fen_rows_floored": sum(
                                len(r["floored_dates"]) for r in rows
                            ),
                            "unknown_code_dates_retained": sum(
                                len(r["unknown_dates"]) for r in rows
                            ),
                        },
                        "provider_calls": 0,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "original_sources_repaired": False,
                        "controller_installed": False,
                        "cashflow_eligible": False,
                        "performance_evidence": False,
                        "execution_authority": False,
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
                {"at": datetime.now(UTC).isoformat(), "error_type": type(exc).__name__},
            )
            raise
