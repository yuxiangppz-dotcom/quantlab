import copy
import json

import numpy as np
import pandas as pd
import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_annual_review import comparison_tables, read_verification
from quantlab.research.extended_completion_protocol import OUTPUT
from quantlab.research.extended_frequency_protocol import CONFIG


def fixture(root):
    config = json.loads((PROJECT_ROOT / CONFIG).read_text())
    out = root / OUTPUT
    out.mkdir(parents=True)
    atomic_seal(out / "plan.json", {"code_head": "source", "config": config})
    report = {
        "status": "complete",
        "fingerprint": "report",
        "references": {
            s: {"prediction_rows": config["model_specs"][s]["prediction_rows"]}
            for s in config["slot_order"]
        },
        "attempts": [
            {
                "slot": s,
                "summary": {
                    "prediction_rows": config["model_specs"][s]["prediction_rows"],
                    "reused_prediction_rows": 0,
                },
            }
            for s in config["slot_order"]
        ],
    }
    review = {
        "report_fingerprint": "report",
        "source_head": "source",
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
        "replicated_rows_by_slot": {
            s: config["model_specs"][s]["prediction_rows"] for s in config["slot_order"]
        },
        "reused_prediction_rows_by_slot": {
            slot: config["model_specs"][slot]["original_summary"]["prediction_rows"]
            if config["model_specs"][slot]["reuse_slot"] is not None
            else 0
            for slot in config["slot_order"]
        },
        "scaler_checks": {
            w["week_id"]: {
                "rows": w["train_rows"],
                "train_sha256": config["model_specs"][f"{w['week_id']}_ridge"]["train_sha256"],
                "max_mean_difference": 1e-15,
                "max_scale_difference": 1e-15,
            }
            for w in config["weeks"]
        },
        "complete_diagnostics": {
            "daily_rows": 968,
            "weekly_rows": 208,
            "monthly_rows": 48,
            "paired_rows": 80,
        },
    }
    return report, review, config


def test_missing_review_remains_unknown_then_complete_review_is_read(tmp_path):
    report, review, _ = fixture(tmp_path)
    assert read_verification(tmp_path, report) is None
    saved = atomic_seal(
        tmp_path / OUTPUT / "independent_annual" / f"{report['fingerprint']}.json", review
    )
    assert read_verification(tmp_path, report) == saved


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "report",
        "status",
        "partial",
        "models",
        "reuse",
        "fits",
        "bool_count",
        "authority",
        "rows",
        "replay",
        "scaler_coverage",
        "scaler_rows",
        "scaler_hash",
        "scaler_difference",
        "diagnostics",
    ],
)
def test_false_independent_coverage_and_authority_are_rejected(tmp_path, change):
    report, original, config = fixture(tmp_path)
    review = copy.deepcopy(original)
    first = config["slot_order"][0]
    week = config["weeks"][0]["week_id"]
    if change == "source":
        review["source_head"] = "other"
    elif change == "report":
        review["report_fingerprint"] = "other"
    elif change == "status":
        report["status"] = "checkpoint"
    elif change == "partial":
        review["coverage"] = "checkpoint"
    elif change == "models":
        review["verified_models"] -= 1
    elif change == "reuse":
        review["reused_prediction_rows_by_slot"][first] = 1
    elif change == "fits":
        review["additional_fit_attempts"] = 1
    elif change == "bool_count":
        review["additional_fit_attempts"] = False
    elif change == "authority":
        review["performance_evidence"] = True
    elif change == "rows":
        review["replicated_rows_by_slot"][first] -= 1
    elif change == "replay":
        review["max_prediction_difference"] = 1e-15
    elif change == "scaler_coverage":
        review["scaler_checks"].pop(week)
    elif change == "scaler_rows":
        review["scaler_checks"][week]["rows"] -= 1
    elif change == "scaler_hash":
        review["scaler_checks"][week]["train_sha256"] = "other"
    elif change == "scaler_difference":
        review["scaler_checks"][week]["max_scale_difference"] = 1.0
    else:
        review["complete_diagnostics"]["paired_rows"] = 79
    atomic_seal(tmp_path / OUTPUT / "independent_annual" / f"{report['fingerprint']}.json", review)
    with pytest.raises(DataValidationError, match="extended independent"):
        read_verification(tmp_path, report)


def test_complete_comparison_keeps_unknowns_and_separates_day_and_week_weights():
    dates = pd.bdate_range("2025-09-01", periods=242).strftime("%Y-%m-%d")
    records = [
        {
            "model": kind,
            "policy": policy,
            "trade_date": day,
            "shared_first_week": i < 54,
            "rank_ic": np.nan if i == 100 else (1.0 if policy == "weekly" else 0.0),
            "rank_stability": np.nan,
            "top20_membership_change": 0.1,
            "score_rows": 5000 if i < 241 else 10709,
            "evaluation_rows": 4995 if i < 241 else 10495,
        }
        for kind in ("ridge", "lightgbm")
        for policy in ("weekly", "monthly")
        for i, day in enumerate(dates)
    ]
    diagnostic = {
        "paired": [
            {"model": kind, "mean_weekly_minus_monthly_rank_ic": value}
            for kind in ("ridge", "lightgbm")
            for value in [0.1, -0.1, 0.0, np.nan] * 10
        ]
    }
    daily = pd.DataFrame(records)
    whole, paired = comparison_tables(daily, diagnostic)
    assert whole.rank_ic_days.eq(241).all() and whole.mean_rank_stability.isna().all()
    assert paired.paired_rank_ic_days.eq(187).all()
    assert paired.mean_daily_rank_ic_difference.eq(1.0).all()
    assert np.allclose(paired.mean_weekly_rank_ic_difference, 0)
    assert paired.positive_difference_weeks.eq(10).all()
    assert paired.negative_difference_weeks.eq(10).all()
    assert paired.tied_difference_weeks.eq(10).all()
    assert paired.undefined_difference_weeks.eq(10).all()
    daily["rank_ic"] = np.nan
    diagnostic["paired"] = [
        {"model": k, "mean_weekly_minus_monthly_rank_ic": np.nan}
        for k in ("ridge", "lightgbm")
        for _ in range(40)
    ]
    whole, paired = comparison_tables(daily, diagnostic)
    assert whole.mean_rank_ic.isna().all() and paired.mean_daily_rank_ic_difference.isna().all()
    assert paired.undefined_difference_weeks.eq(40).all() and paired.paired_rank_ic_days.eq(0).all()
    with pytest.raises(DataValidationError):
        comparison_tables(daily.iloc[1:], diagnostic)
    daily.loc[0, "score_rows"] -= 1
    with pytest.raises(DataValidationError, match="member/label"):
        comparison_tables(daily, diagnostic)
