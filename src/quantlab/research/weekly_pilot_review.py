"""Read-only validation of the independent six-fit pilot reconstruction."""

import math

from quantlab.data.models import DataValidationError
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import OUTPUT


def read_verification(root, report):
    path = root / OUTPUT / "independent_verification.json"
    if not path.exists():
        return None
    review = sealed_read(path)
    plan = sealed_read(root / OUTPUT / "plan.json")
    expected = {
        "report_fingerprint": report["fingerprint"],
        "source_head": plan["code_head"],
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "prediction_rows": 253806,
        "daily_rows": 60,
        "summary_rows": 12,
        "additional_fit_attempts": 0,
        "provider_calls": 0,
        "performance_evidence": False,
        "execution_authority": False,
    }
    if report["status"] != "complete" or any(
        type(review.get(key)) is not type(value) or review[key] != value
        for key, value in expected.items()
    ):
        raise DataValidationError("weekly pilot independent source/population/authority changed")
    rows = {item["slot"]: item["summary"]["prediction_rows"] for item in report["attempts"]}
    actual = review.get("replicated_rows_by_slot", {})
    if actual != rows or any(type(value) is not int for value in actual.values()):
        raise DataValidationError("weekly pilot independent prediction coverage changed")
    differences = review.get("max_prediction_difference_by_slot", {})
    if set(differences) != set(rows) or any(
        type(value) is not float or value != 0.0 for value in differences.values()
    ):
        raise DataValidationError("weekly pilot saved predictions failed independent replay")
    scalers = review.get("scaler_checks", {})
    if set(scalers) != {f"week{i}" for i in (1, 2, 3)}:
        raise DataValidationError("weekly pilot scaler coverage changed")
    for week in plan["config"]["weeks"]:
        key = f"week{week['week']}"
        checked = scalers[key]
        if (
            type(checked.get("rows")) is not int
            or checked["rows"] != week["train_rows"]
            or checked.get("membership_sha256") != plan["config"]["membership_sha256"][key]["train"]
        ):
            raise DataValidationError("weekly pilot independent training population changed")
        for field in ("max_mean_difference", "max_scale_difference"):
            value = checked.get(field)
            if type(value) is not float or not math.isfinite(value) or not 0 <= value <= 1e-9:
                raise DataValidationError("weekly pilot independent scaler difference invalid")
    paired = review.get("paired_checks", [])
    if len(paired) != len(report["paired"]) or len(paired) != 4:
        raise DataValidationError("weekly pilot independent paired coverage changed")
    for checked, row in zip(paired, report["paired"], strict=True):
        if (
            checked.get("model") != row["model"]
            or type(checked.get("week")) is not int
            or checked["week"] != row["week"]
            or type(checked.get("days")) is not int
            or checked["days"] != row["paired_days"]
            or type(checked.get("delta")) is not float
            or not math.isclose(
                checked["delta"], row["mean_weekly_minus_anchor_rank_ic"], abs_tol=1e-12, rel_tol=0
            )
        ):
            raise DataValidationError("weekly pilot independent paired result changed")
    return review
