import copy
import json

import pytest

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_recovery_protocol import OUTPUT, PARENT_PROOF, PARENT_REPORT, SLOTS
from quantlab.research.extended_recovery_review import read_verification


@pytest.fixture
def evidence(tmp_path):
    config = json.loads((PROJECT_ROOT / "config/alpha158_extended_frequency_v1.json").read_text())
    out = tmp_path / OUTPUT
    atomic_seal(out / "plan.json", {"code_head": "synthetic-only", "config": config})
    report = {"status": "complete", "fingerprint": "synthetic-report"}
    values = {
        "source_head": "synthetic-only",
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
        "group_counts_by_slot": {},
        "scaler_checks": {},
    }
    for slot in SLOTS:
        s = config["model_specs"][slot]
        old = s["original_summary"]["prediction_rows"]
        values["group_counts_by_slot"][slot] = {"existing": old, "new": s["prediction_rows"] - old}
        values["scaler_checks"][s["week_id"]] = {
            "rows": s["train_rows"],
            "train_sha256": s["train_sha256"],
            "max_mean_difference": 0.0,
            "max_scale_difference": 0.0,
        }
    return tmp_path, out, report, values


def test_missing_independent_proof_remains_pending(evidence):
    root, _, report, _ = evidence
    assert read_verification(root, report) is None


def test_proof_preserves_failed_parent_and_incomplete_annual_scope(evidence):
    root, out, report, values = evidence
    proof = atomic_seal(out / "independent" / f"{report['fingerprint']}.json", values)
    assert read_verification(root, report) == proof
    assert proof["parent_failed_report"] == PARENT_REPORT
    assert (
        proof["combined_model_references"] == 100 and proof["complete_annual_comparison"] is False
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_head", "another-source"),
        ("report_fingerprint", "another-report"),
        ("verified_references", 5),
        ("additional_fit_attempts", False),
        ("prediction_rows", 314803),
        ("all_group_receipts_exact", False),
        ("max_prediction_difference", 1e-12),
        ("complete_annual_comparison", True),
        ("historical_market_coverage_complete", True),
        ("performance_evidence", True),
        ("execution_authority", True),
    ],
)
def test_invalid_proof_cannot_be_presented_as_success(evidence, key, value):
    root, out, report, values = evidence
    values[key] = value
    atomic_seal(out / "independent" / f"{report['fingerprint']}.json", values)
    with pytest.raises(DataValidationError):
        read_verification(root, report)


@pytest.mark.parametrize(
    "damage", ["count", "missing_slot", "train_hash", "train_rows", "scaler_nan"]
)
def test_proof_requires_every_group_and_mature_training_window(evidence, damage):
    root, out, report, values = evidence
    values = copy.deepcopy(values)
    if damage == "count":
        values["group_counts_by_slot"][SLOTS[0]]["new"] += 1
    elif damage == "missing_slot":
        values["group_counts_by_slot"].pop(SLOTS[0])
    elif damage == "train_hash":
        values["scaler_checks"]["2026w32"]["train_sha256"] = "wrong"
    elif damage == "train_rows":
        values["scaler_checks"]["2026w32"]["rows"] = 0
    else:
        values["scaler_checks"]["2026w32"]["max_mean_difference"] = None
    atomic_seal(out / "independent" / f"{report['fingerprint']}.json", values)
    with pytest.raises(DataValidationError):
        read_verification(root, report)
