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


@pytest.fixture
def historical_sources(tmp_path):
    import hashlib
    import subprocess

    from quantlab.data.dividend_review import PINNED_CODE

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "--quiet")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    inputs = {}
    for name in [*sorted(PINNED_CODE), "data/source.json"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = b"original source"
        path.write_bytes(raw)
        inputs[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    git("add", ".")
    git("commit", "--quiet", "-m", "sealed synthetic acquisition source")
    return tmp_path, inputs, git("rev-parse", "HEAD")


def test_snapshot_uses_bound_historical_code_but_current_data(historical_sources):
    from quantlab.data.dividend_review import PINNED_CODE, verify_snapshot_inputs

    root, inputs, head = historical_sources
    for name in PINNED_CODE:
        (root / name).write_text("later implementation")
    verify_snapshot_inputs(root, inputs, head)
    # Live progress cannot use historical source to authorize a different running version.
    with pytest.raises(DataValidationError):
        verify_snapshot_inputs(root, inputs)
    (root / "data/source.json").write_text("changed data")
    with pytest.raises(DataValidationError):
        verify_snapshot_inputs(root, inputs, head)


@pytest.mark.parametrize("head", ["0" * 40, "HEAD", "--help", "../HEAD", 123])
def test_unavailable_or_unpinned_history_is_rejected(historical_sources, head):
    from quantlab.data.dividend_review import verify_snapshot_inputs

    root, inputs, _ = historical_sources
    with pytest.raises(DataValidationError):
        verify_snapshot_inputs(root, inputs, head)


def test_historical_source_hash_and_size_must_both_match(historical_sources):
    from quantlab.data.dividend_review import PINNED_CODE, verify_snapshot_inputs

    root, inputs, head = historical_sources
    name = sorted(PINNED_CODE)[0]
    inputs[name]["bytes"] += 1
    with pytest.raises(DataValidationError):
        verify_snapshot_inputs(root, inputs, head)
    inputs[name]["bytes"] -= 1
    inputs[name]["sha256"] = "0" * 64
    with pytest.raises(DataValidationError):
        verify_snapshot_inputs(root, inputs, head)


@pytest.mark.parametrize("change", [None, "wrong_report", "undercount", "correction", "authority"])
def test_budget_correction_stays_bound_and_conservative(fixture, change):
    from quantlab.data.dividend_review import read_verification

    root, out, report = fixture
    report.update(fingerprint="fixed-report", retries=0, charged_body_bytes=5)
    config = json.loads((root / CONFIG).read_text())
    config["max_body_bytes"] = 100
    (root / CONFIG).write_text(json.dumps(config))
    verified = {
        "report_fingerprint": report["fingerprint"],
        "exact_codes": 1,
        "attempts": 1,
        "retries": 0,
        "returned_rows": 0,
        "status_counts": {"empty": 1},
        "charged_body_bytes_as_recorded": 5,
        "conservative_body_budget_bytes": 20,
        "failure_budget_correction_bytes": 15,
        "performance_evidence": False,
        "execution_authority": False,
    }
    if change == "wrong_report":
        verified["report_fingerprint"] = "another"
    elif change == "undercount":
        verified["conservative_body_budget_bytes"] = 4
    elif change == "correction":
        verified["failure_budget_correction_bytes"] = 0
    elif change == "authority":
        verified["execution_authority"] = 0
    sealed_write(out / "independent_verification.json", verified)
    if change is None:
        assert read_verification(root, report)["conservative_body_budget_bytes"] == 20
    else:
        with pytest.raises(DataValidationError):
            read_verification(root, report)
