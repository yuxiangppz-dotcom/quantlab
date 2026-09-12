import copy

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.weekly_pilot_protocol import OUTPUT
from quantlab.research.weekly_pilot_review import read_verification


def fixture(root):
    out = root / OUTPUT
    out.mkdir(parents=True)
    slots = [f"week{i}_{kind}" for i in (1, 2, 3) for kind in ("ridge", "lightgbm")]
    counts = [3600958, 3601723, 3602634]
    rows = {slot: [76118, 25375, 25410][int(slot[4]) - 1] for slot in slots}
    report = {
        "status": "complete",
        "fingerprint": "report",
        "attempts": [{"slot": slot, "summary": {"prediction_rows": n}} for slot, n in rows.items()],
        "paired": [
            {"model": kind, "week": i, "paired_days": 5, "mean_weekly_minus_anchor_rank_ic": 0.1}
            for kind in ("ridge", "lightgbm")
            for i in (2, 3)
        ],
    }
    atomic_seal(
        out / "plan.json",
        {
            "code_head": "source",
            "config": {
                "weeks": [{"week": i, "train_rows": counts[i - 1]} for i in (1, 2, 3)],
                "membership_sha256": {f"week{i}": {"train": str(i)} for i in (1, 2, 3)},
            },
        },
    )
    review = {
        "report_fingerprint": "report",
        "source_head": "source",
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "prediction_rows": 253806,
        "daily_rows": 60,
        "summary_rows": 12,
        "additional_fit_attempts": 0,
        "provider_calls": 0,
        "performance_evidence": False,
        "execution_authority": False,
        "replicated_rows_by_slot": rows,
        "max_prediction_difference_by_slot": dict.fromkeys(slots, 0.0),
        "scaler_checks": {
            f"week{i}": {
                "rows": counts[i - 1],
                "membership_sha256": str(i),
                "max_mean_difference": 1e-15,
                "max_scale_difference": 1e-15,
            }
            for i in (1, 2, 3)
        },
        "paired_checks": [
            {"model": row["model"], "week": row["week"], "days": 5, "delta": 0.1}
            for row in report["paired"]
        ],
    }
    return report, review


def test_missing_independent_review_is_unknown_then_verified(tmp_path):
    report, review = fixture(tmp_path)
    assert read_verification(tmp_path, report) is None
    saved = atomic_seal(tmp_path / OUTPUT / "independent_verification.json", review)
    assert read_verification(tmp_path, report) == saved


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "report",
        "fit",
        "bool_count",
        "authority",
        "prediction_rows",
        "replay",
        "scaler",
        "scaler_hash",
        "paired",
        "paired_coverage",
        "training_rows",
        "status",
    ],
)
def test_resealed_unrelated_or_incomplete_review_rejected(tmp_path, change):
    report, original = fixture(tmp_path)
    review = copy.deepcopy(original)
    if change == "source":
        review["source_head"] = "other"
    elif change == "report":
        review["report_fingerprint"] = "other"
    elif change == "fit":
        review["additional_fit_attempts"] = 1
    elif change == "bool_count":
        review["additional_fit_attempts"] = False
    elif change == "authority":
        review["performance_evidence"] = True
    elif change == "prediction_rows":
        review["replicated_rows_by_slot"]["week1_ridge"] -= 1
    elif change == "replay":
        review["max_prediction_difference_by_slot"]["week1_ridge"] = 1e-15
    elif change == "scaler":
        review["scaler_checks"]["week1"]["max_scale_difference"] = 1.0
    elif change == "scaler_hash":
        review["scaler_checks"]["week1"]["membership_sha256"] = "other"
    elif change == "training_rows":
        review["scaler_checks"]["week1"]["rows"] -= 1
    elif change == "paired":
        review["paired_checks"][0]["delta"] = 0.2
    elif change == "paired_coverage":
        review["paired_checks"].pop()
    else:
        report["status"] = "failed"
    atomic_seal(tmp_path / OUTPUT / "independent_verification.json", review)
    with pytest.raises(DataValidationError, match="weekly pilot"):
        read_verification(tmp_path, report)
