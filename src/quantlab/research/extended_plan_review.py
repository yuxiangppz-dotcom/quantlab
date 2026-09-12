"""Bind independent planning evidence to the unchanged zero-action schedule."""

from quantlab.data.models import DataValidationError
from quantlab.research.extended_plan_run import OUTPUT
from quantlab.research.round2_dataset import sealed_read


def read_verification(root, report, weekly):
    path = root / OUTPUT / "independent_verification.json"
    if not path.exists():
        return None
    review = sealed_read(path)
    plan = sealed_read(root / OUTPUT / "plan.json")
    expected = {
        key: report[key]
        for key in (
            "metadata_rows",
            "metadata_codes",
            "weekly_rows",
            "monthly_anchors",
            "future_max_new_fit_attempts",
        )
    }
    expected.update(
        report_fingerprint=report["fingerprint"],
        schedule_fingerprint=weekly["fingerprint"],
        source_head=plan["code_head"],
        signal_market_days=weekly["signal_market_days"],
        reused_models=6,
        new_fit_attempts=0,
        new_prediction_rows=0,
        provider_calls=0,
        performance_evidence=False,
        execution_authority=False,
    )
    if any(
        type(review.get(key)) is not type(value) or review[key] != value
        for key, value in expected.items()
    ):
        raise DataValidationError("extended independent source/population/action mismatch")
    fields = (
        "train_feature_rows",
        "train_rows",
        "train_unmatured_or_unknown_end",
        "train_missing_label_rows",
        "prediction_rows",
        "evaluation_rows",
        "prediction_missing_label_rows",
    )
    expected_rows = [{key: row[key] for key in fields} for row in weekly["weeks"]]
    if review.get("weekly_populations") != expected_rows or any(
        type(value) is not int or value < 0
        for row in review["weekly_populations"]
        for value in row.values()
    ):
        raise DataValidationError("extended independent weekly coverage changed")
    return review
