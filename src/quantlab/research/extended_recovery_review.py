"""Validate a separate zero-fit recovery proof before presenting its bounded result."""

import math

from quantlab.data.models import DataValidationError
from quantlab.research.extended_recovery_protocol import OUTPUT, PARENT_PROOF, PARENT_REPORT, SLOTS
from quantlab.research.round2_dataset import sealed_read


def read_verification(root, report):
    path = root / OUTPUT / "independent" / f"{report['fingerprint']}.json"
    if not path.exists():
        return None
    proof = sealed_read(path)
    plan = sealed_read(root / OUTPUT / "plan.json")
    expected = {
        "source_head": plan["code_head"],
        "report_fingerprint": report["fingerprint"],
        "parent_failed_report": PARENT_REPORT,
        "inherited_prefix_proof": PARENT_PROOF,
        "verified_references": 6,
        "prediction_rows": 314804,
        "preserved_old_rows": 253806,
        "newly_scored_rows": 60998,
        "metadata_rows": 9487149,
        "metadata_codes": 5443,
        "verified_metadata_partitions": 171,
        "max_prediction_difference": 0.0,
        "all_original_bytes_unchanged": True,
        "all_group_receipts_exact": True,
        "combined_model_references": 100,
        "required_model_references": 104,
        "combined_prediction_rows": 4259564,
        "unconsumed_new_fit_slots": 4,
        "additional_fit_attempts": 0,
        "new_prediction_artifacts": 0,
        "provider_calls": 0,
        "complete_annual_comparison": False,
        "performance_evidence": False,
        "execution_authority": False,
        "history_already_observed": True,
        "historical_market_coverage_complete": False,
    }
    if report["status"] != "complete" or any(
        type(proof.get(key)) is not type(value) or proof[key] != value
        for key, value in expected.items()
    ):
        raise DataValidationError("recovery independent source/population/authority changed")
    counts = proof.get("group_counts_by_slot", {})
    if set(counts) != set(SLOTS):
        raise DataValidationError("recovery independent group inventory changed")
    for slot in SLOTS:
        spec = plan["config"]["model_specs"][slot]
        existing = spec["original_summary"]["prediction_rows"]
        target = {"existing": existing, "new": spec["prediction_rows"] - existing}
        if counts[slot] != target or any(type(v) is not int for v in counts[slot].values()):
            raise DataValidationError("recovery independent group population changed")
    scalers = proof.get("scaler_checks", {})
    weeks = {plan["config"]["model_specs"][s]["week_id"] for s in SLOTS}
    if set(scalers) != weeks:
        raise DataValidationError("recovery independent training windows changed")
    for week in weeks:
        spec = plan["config"]["model_specs"][f"{week}_ridge"]
        checked = scalers[week]
        if (
            type(checked.get("rows")) is not int
            or checked["rows"] != spec["train_rows"]
            or checked.get("train_sha256") != spec["train_sha256"]
        ):
            raise DataValidationError("recovery independent mature training membership changed")
        for key in ("max_mean_difference", "max_scale_difference"):
            difference = checked.get(key)
            if (
                type(difference) is not float
                or not math.isfinite(difference)
                or not 0 <= difference < 1e-9
            ):
                raise DataValidationError("recovery independent training scaler changed")
    return proof
