import json

import pytest

from quantlab.data.dividend_acquisition import CONFIG, OUTPUT
from quantlab.data.dividend_review import read_acquisition
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_write


@pytest.fixture
def fixture(tmp_path):
    request = sealed_write(tmp_path / "request.json", {"instrument_ids": ["000001.SZ"]})
    path = tmp_path / CONFIG
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "inputs": {},
                "request_path": "request.json",
                "request_fingerprint": request["fingerprint"],
            }
        )
    )
    out = tmp_path / OUTPUT
    out.mkdir(parents=True)
    report = {
        "identity": canonical_payload_fingerprint({"contract_sha256": _sha(path)}),
        "instrument_count": 1,
        "attempts": 1,
        "returned_rows": 0,
        "status_counts": {"empty": 1},
        "per_code": [{"instrument_id": "000001.SZ", "status": "empty", "attempts": 1, "rows": 0}],
        "complete_event_history_certified": False,
        "historical_pit_certified": False,
        "cashflow_eligible": False,
        "performance_evidence": False,
        "execution_authority": False,
    }
    return tmp_path, out, report


def test_absent_and_stage_progress_are_explicit(tmp_path, fixture):
    assert read_acquisition(tmp_path / "absent") is None
    root, out, report = fixture
    sealed_write(out / "progress.json", report)
    kind, result = read_acquisition(root)
    assert kind == "progress" and result["returned_rows"] == 0
    assert result["complete_event_history_certified"] is False


@pytest.mark.parametrize(
    "flag",
    [
        "complete_event_history_certified",
        "historical_pit_certified",
        "cashflow_eligible",
        "performance_evidence",
        "execution_authority",
    ],
)
def test_raw_readiness_never_gains_financial_authority(fixture, flag):
    root, out, report = fixture
    report[flag] = True
    sealed_write(out / "progress.json", report)
    with pytest.raises(DataValidationError, match="financial authority"):
        read_acquisition(root)


@pytest.mark.parametrize("change", ["population", "count", "path"])
def test_population_aggregation_and_path_escape_fail_closed(fixture, change):
    root, out, report = fixture
    if change == "population":
        report["per_code"][0]["instrument_id"] = "600000.SH"
    elif change == "count":
        report["attempts"] = 2
    else:
        sealed_write(out / "latest.json", {"report_path": "../../request.json"})
    sealed_write(out / "progress.json", report)
    with pytest.raises(DataValidationError):
        read_acquisition(root)
