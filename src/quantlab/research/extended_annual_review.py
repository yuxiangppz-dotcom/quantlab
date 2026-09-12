"""Read-only validation and descriptive presentation of the complete annual replay."""

import math

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.research.extended_completion_protocol import OUTPUT
from quantlab.research.round2_dataset import sealed_read


def read_verification(root, report):
    path = root / OUTPUT / "independent_annual" / f"{report['fingerprint']}.json"
    if not path.exists():
        return None
    review = sealed_read(path)
    plan = sealed_read(root / OUTPUT / "plan.json")
    config = plan["config"]
    expected = {
        "report_fingerprint": report["fingerprint"],
        "source_head": plan["code_head"],
        "coverage": "complete",
        "verified_models": 104,
        "verified_new_models": 98,
        "verified_reused_models": 6,
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "prediction_rows": 4320562,
        "max_prediction_difference": 0.0,
        "additional_fit_attempts": 0,
        "new_prediction_artifacts": 0,
        "provider_calls": 0,
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
        "complete_annual_comparison": True,
        "historical_market_coverage_complete": False,
        "original_parent_status": "failed",
        "global_actual_fit_invocations": 98,
        "all_original_bytes_unchanged": True,
    }
    if report["status"] != "complete" or any(
        type(review.get(key)) is not type(value) or review[key] != value
        for key, value in expected.items()
    ):
        raise DataValidationError("extended independent source/population/authority changed")
    rows = {slot: config["model_specs"][slot]["prediction_rows"] for slot in config["slot_order"]}
    summaries = report["references"]
    if len(rows) != 104 or set(summaries) != set(rows):
        raise DataValidationError("extended independent model inventory changed")
    for key, target in (
        ("replicated_rows_by_slot", rows),
        (
            "reused_prediction_rows_by_slot",
            {
                slot: config["model_specs"][slot]["original_summary"]["prediction_rows"]
                if config["model_specs"][slot]["reuse_slot"] is not None
                else 0
                for slot in rows
            },
        ),
    ):
        actual = review.get(key, {})
        if actual != target or any(type(value) is not int for value in actual.values()):
            raise DataValidationError("extended independent prediction coverage changed")
    scalers = review.get("scaler_checks", {})
    if len(config["weeks"]) != 52 or set(scalers) != {w["week_id"] for w in config["weeks"]}:
        raise DataValidationError("extended independent scaler coverage changed")
    for week in config["weeks"]:
        checked = scalers[week["week_id"]]
        if (
            type(checked.get("rows")) is not int
            or checked["rows"] != week["train_rows"]
            or checked.get("train_sha256")
            != config["model_specs"][f"{week['week_id']}_ridge"]["train_sha256"]
        ):
            raise DataValidationError("extended independent training population changed")
        for key in ("max_mean_difference", "max_scale_difference"):
            difference = checked.get(key)
            if (
                type(difference) is not float
                or not math.isfinite(difference)
                or not 0 <= difference <= 1e-9
            ):
                raise DataValidationError("extended independent scaler difference invalid")
    diagnostic_counts = {
        "daily_rows": 968,
        "weekly_rows": 208,
        "monthly_rows": 48,
        "paired_rows": 80,
    }
    if review.get("complete_diagnostics") != diagnostic_counts:
        raise DataValidationError("extended independent diagnostic coverage changed")
    return review


def comparison_tables(daily, diagnostics):
    """Use all declared outcomes, keeping shared days and undefined metrics explicit."""
    if len(daily) != 968 or daily.duplicated(["model", "policy", "trade_date"]).any():
        raise DataValidationError("extended presentation needs all policy days")
    whole, comparisons = [], []
    for kind in ("ridge", "lightgbm"):
        selected = daily.loc[daily.model.eq(kind)]
        for policy in ("weekly", "monthly"):
            frame = selected.loc[selected.policy.eq(policy)]
            if len(frame) != 242 or frame.shared_first_week.sum() != 54:
                raise DataValidationError("extended presentation policy/shared dates changed")
            if (
                int(frame.score_rows.sum()) != 1215709
                or int(frame.evaluation_rows.sum()) != 1214290
                or (frame.evaluation_rows > frame.score_rows).any()
                or (frame.evaluation_rows < 0).any()
            ):
                raise DataValidationError("extended presentation member/label coverage changed")
            whole.append(
                {
                    "model": kind,
                    "policy": policy,
                    "signal_days": len(frame),
                    "rank_ic_days": int(frame.rank_ic.notna().sum()),
                    "prediction_rows": int(frame.score_rows.sum()),
                    "evaluation_rows": int(frame.evaluation_rows.sum()),
                    "mean_rank_ic": known_mean(frame.rank_ic),
                    "mean_rank_stability": known_mean(frame.rank_stability),
                    "mean_top20_membership_change": known_mean(frame.top20_membership_change),
                }
            )
        a, b = [
            selected.loc[selected.policy.eq(policy) & ~selected.shared_first_week].set_index(
                "trade_date"
            )
            for policy in ("weekly", "monthly")
        ]
        if len(a) != 188 or not a.index.equals(b.index):
            raise DataValidationError("extended paired presentation dates differ")
        valid = a.rank_ic.notna() & b.rank_ic.notna()
        delta = a.rank_ic[valid] - b.rank_ic[valid]
        paired = pd.DataFrame(diagnostics["paired"])
        weekly_delta = paired.loc[paired.model.eq(kind), "mean_weekly_minus_monthly_rank_ic"]
        if len(weekly_delta) != 40:
            raise DataValidationError("extended paired presentation weekly coverage changed")
        comparisons.append(
            {
                "model": kind,
                "nonshared_signal_days": 188,
                "paired_rank_ic_days": int(valid.sum()),
                "weekly_mean_rank_ic_on_paired_days": known_mean(a.rank_ic[valid]),
                "monthly_mean_rank_ic_on_paired_days": known_mean(b.rank_ic[valid]),
                "mean_daily_rank_ic_difference": known_mean(delta),
                "mean_weekly_rank_ic_difference": known_mean(weekly_delta),
                "positive_difference_weeks": int(weekly_delta.gt(0).sum()),
                "negative_difference_weeks": int(weekly_delta.lt(0).sum()),
                "tied_difference_weeks": int(weekly_delta.eq(0).sum()),
                "undefined_difference_weeks": int(weekly_delta.isna().sum()),
            }
        )
    return pd.DataFrame(whole), pd.DataFrame(comparisons)


def known_mean(values):
    present = values.dropna()
    return None if present.empty else float(present.mean())
