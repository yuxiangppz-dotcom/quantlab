import hashlib
import json

import pytest

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.cost_input_review import read_report
from quantlab.research.cost_input_sources import CONFIG, MANIFEST, OUTPUT
from quantlab.research.round2_dataset import sealed_write


def mutate_fixture(path, payload):
    """Simulate altered input bytes in pytest's temporary directory only."""
    path.write_text(json.dumps({**payload, "fingerprint": canonical_payload_fingerprint(payload)}))


@pytest.fixture
def saved(tmp_path):
    for name in (CONFIG, MANIFEST):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    digest = hashlib.sha256(b"{}").hexdigest()
    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    (out / "evidence.txt").write_text("retained")
    report = {
        "contract_sha256": digest,
        "source_manifest_sha256": digest,
        "new_fit_attempts": 0,
        "complete_cost": None,
        "net_cost_ready": False,
        "performance_evidence": False,
        "execution_authority": False,
        "fresh_forward_evidence": False,
        "acquisition_request": {"instrument_ids": ["000001.SZ"], "execution_authority": False},
        "artifacts": {"evidence.txt": {"sha256": hashlib.sha256(b"retained").hexdigest()}},
    }
    report = sealed_write(out / "report.json", report)
    sealed_write(
        out / "acquisition_request.json",
        {
            "report_fingerprint": report["fingerprint"],
            **report["acquisition_request"],
        },
    )
    return tmp_path, report


def test_valid_read_preserves_unknown_cost_and_absent_report(tmp_path, saved):
    root, expected = saved
    assert read_report(root) == expected
    assert read_report(tmp_path / "empty") is None


@pytest.mark.parametrize(
    "flag",
    ["net_cost_ready", "performance_evidence", "execution_authority", "fresh_forward_evidence"],
)
def test_presentation_cannot_upgrade_audit_authority(saved, flag):
    root, report = saved
    report[flag] = True
    report.pop("fingerprint")
    mutate_fixture(root / OUTPUT / "report.json", report)
    with pytest.raises(DataValidationError, match="authority"):
        read_report(root)


@pytest.mark.parametrize("field,value", [("new_fit_attempts", 1), ("complete_cost", 0)])
def test_no_fit_or_zero_full_cost_substitution(saved, field, value):
    root, report = saved
    report[field] = value
    report.pop("fingerprint")
    mutate_fixture(root / OUTPUT / "report.json", report)
    with pytest.raises(DataValidationError, match="fit models or certify"):
        read_report(root)


@pytest.mark.parametrize("target", ["source", "artifact", "request"])
def test_download_identity_changes_are_detected(saved, target):
    root, _ = saved
    if target == "source":
        (root / MANIFEST).write_text('{"changed":true}')
    elif target == "artifact":
        (root / OUTPUT / "evidence.txt").write_text("changed")
    else:
        path = root / OUTPUT / "acquisition_request.json"
        payload = json.loads(path.read_text())
        payload.pop("fingerprint")
        payload["instrument_ids"] = ["600000.SH"]
        mutate_fixture(path, payload)
    with pytest.raises(DataValidationError):
        read_report(root)
