"""Read-only completed scenario and independent-accounting evidence."""

import math

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.extended_economic_contract import CONTROL, OUT, POLICIES, scenarios
from quantlab.research.round2_dataset import sealed_read, verify_entries


def validate_review(report, plan, proof):
    if (
        report["fingerprint"] != "eb265b2145ca2a8ab8d5b1095eb57222d955dd8e3336348b263521f095de9feb"
        or plan["fingerprint"] != "1998aba5fc0d8022fe3c359b85c26e04e967f752827e0bcbc878e7cb75263590"
        or proof["fingerprint"]
        != "c970ab1981d907bb0d5ce649238f0d29bbb56df03bddd2c7f2c83320a2670ad9"
    ):
        raise DataValidationError(
            "economic delivery identity differs from independently closed run"
        )
    expected = {
        "report_fingerprint": report["fingerprint"],
        "plan_fingerprint": plan["fingerprint"],
        "source_head": plan["code_head"],
        "verified_paths": 60,
        "independent_target_rows": 444072,
        "independent_raw_marks": 1278503,
        "all_12_strategy_control_groups_symmetric": True,
        "capital_scale_invariance_checked": True,
        "all_original_inputs_unchanged": True,
        "extra_engine_runs": 0,
        "new_fits": 0,
        "provider_calls": 0,
        "execution_authority": False,
        "complete_user_fee_accounting": False,
        "historical_market_coverage_complete": False,
    }
    if any(type(proof.get(k)) is not type(v) or proof[k] != v for k, v in expected.items()):
        raise DataValidationError("economic independent proof scope or authority changed")
    ids = [s["id"] for s in scenarios()]
    if (
        report["status"] != "complete"
        or report["finished_paths"] != 60
        or report["consumed_paths"] != 60
        or report["plan"] != plan["fingerprint"]
        or report["source_head"] != plan["code_head"]
        or [r["scenario"] for r in report["scenarios"]] != scenarios()
        or [r["scenario"] for r in proof["statuses"]] != ids
        or set(proof["path_receipts"]) != set(ids)
    ):
        raise DataValidationError("economic independent scenario coverage changed")
    for r, p in zip(report["scenarios"], proof["statuses"], strict=True):
        s = r["summary"]
        if (
            r["status"] != "finished"
            or s["scenario"] != r["scenario"]
            or s["engine_status"] != p["status"]
            or s["valid_through"] != p["valid_through"]
            or s["valid_sessions"] != p["sessions"]
            or s["position_rows"] != p["position_rows"]
            or s["plan"] != plan["fingerprint"]
            or s["source_head"] != plan["code_head"]
        ):
            raise DataValidationError("economic independently verified result changed")
        for key in (
            "execution_authority",
            "complete_user_fee_accounting",
            "historical_market_coverage_complete",
        ):
            if report[key] is not False or s[key] is not False:
                raise DataValidationError("economic scenario financial authority changed")
    residual = proof["max_absolute_cash_position_residual"]
    if not isinstance(residual, float) or not math.isfinite(residual) or not 0 <= residual <= 1e-9:
        raise DataValidationError("economic independent residual is invalid")


def read_review(root):
    out = root / OUT
    if not (out / "report.json").exists():
        return None
    report = sealed_read(out / "report.json")
    proof_path = out / "independent" / f"{report['fingerprint']}.json"
    if not proof_path.exists():
        return None
    plan = sealed_read(out / "plan.json")
    proof = sealed_read(proof_path)
    validate_review(report, plan, proof)
    verify_entries(out, plan["artifacts"])
    for row in report["scenarios"]:
        name = row["scenario"]["id"]
        folder = out / "paths" / name
        receipt = sealed_read(folder / "receipt.json")
        if (
            receipt["fingerprint"] != proof["path_receipts"][name]
            or receipt["status"] != "finished"
            or receipt["identity"] != plan["fingerprint"]
        ):
            raise DataValidationError("economic path receipt differs from independent proof")
        verify_entries(folder, receipt["artifacts"])
        if sealed_read(folder / "summary.json") != row["summary"]:
            raise DataValidationError("economic path summary differs from frozen report")
    return report, plan, proof


def summary_table(report):
    rows = []
    for row in report["scenarios"]:
        s = row["summary"]
        e = s["first_blocking_event"] or {}
        rows.append(
            {
                **row["scenario"],
                "engine_status": s["engine_status"],
                "valid_through": s["valid_through"],
                "valid_sessions": s["valid_sessions"],
                "full_period_completed": s["full_period_completed"],
                "return_after_declared_friction_on_valid_prefix": s[
                    "return_after_declared_friction_on_valid_prefix"
                ],
                "drawdown_on_valid_prefix": s["drawdown_on_valid_prefix"],
                "declared_friction_charged": s["declared_friction_charged"],
                "final_cash": s["final_cash"],
                "terminal_open_positions": s["terminal_open_positions"],
                "blocking_instrument": e.get("instrument_id"),
                "blocking_session": e.get("blocking_session"),
                "last_observed_mark_date": e.get("last_mark_date"),
            }
        )
    return pd.DataFrame(rows)


def load_case(root, report, identity):
    rows = {r["scenario"]["id"]: r for r in report["scenarios"]}
    if identity not in rows:
        raise DataValidationError("unknown economic scenario")
    folder = root / OUT / "paths" / identity
    receipt = sealed_read(folder / "receipt.json")
    verify_entries(folder, receipt["artifacts"])
    return (
        rows[identity]["summary"],
        pd.read_parquet(folder / "daily.parquet", use_threads=False),
        pd.read_parquet(folder / "positions.parquet", use_threads=False),
        pd.read_parquet(folder / "value_transfers.parquet", use_threads=False),
        sealed_read(folder / "accounting.json"),
    )


def comparable_prefix(frames):
    """Mechanical intersection of all five valid paths; never an optimized date window."""
    if set(frames) != set(POLICIES) or any(f.empty for f in frames.values()):
        raise DataValidationError("all five nonempty paths are required for comparison")
    common_end = min(max(f.trade_date) for f in frames.values())
    indices = None
    curves = {}
    rows = []
    for policy in POLICIES:
        f = frames[policy].loc[frames[policy].trade_date.le(common_end)].copy()
        f = f.sort_values("trade_date")
        dates = list(f.trade_date)
        if indices is None:
            indices = dates
        elif indices != dates:
            raise DataValidationError("comparison dates do not match exactly")
        if len(dates) != len(set(dates)):
            raise DataValidationError("comparison has duplicated dates")
        values = f.value_after_declared_friction
        if values.iloc[0] != 1 or not values.map(math.isfinite).all() or (values <= 0).any():
            raise DataValidationError("comparison value basis is invalid")
        curves[policy] = values.to_numpy()
        rows.append(
            {
                "policy": policy,
                "common_end": str(common_end),
                "sessions": len(f),
                "return_after_declared_friction": float(values.iloc[-1] - 1),
                "drawdown": float((values / values.cummax() - 1).min()),
                "declared_friction_charged": float(f.declared_friction_charge.sum()),
            }
        )
    table = pd.DataFrame(rows)
    control = table.loc[table.policy.eq(CONTROL), "return_after_declared_friction"].iloc[0]
    table["difference_from_cohort_control"] = table.return_after_declared_friction - control
    return table, pd.DataFrame(curves, index=pd.Index(indices, name="trade_date"))
