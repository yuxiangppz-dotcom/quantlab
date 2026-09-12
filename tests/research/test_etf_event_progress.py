"""ETF accounting evidence cannot silently become admission or execution authority."""

import hashlib
import json

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.etf_event_progress import (
    AUTHORITY_FLAGS,
    CONFIG,
    PROOF_CHECKS,
    read_etf_progress,
)


def binding(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def replace_seal(path, changes):
    data = json.loads(path.read_text())
    data.pop("fingerprint")
    data.update(changes)
    path.unlink()
    return atomic_seal(path, data)


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path
    out = root / "data/audit"
    out.mkdir(parents=True)
    source = root / "data/source.body"
    source.write_bytes(b"fixed raw cash distribution")
    parent = atomic_seal(root / "data/parent.json", {"kind": "intake"})
    atomic_seal(root / "data/parent_proof.json", {"report_fingerprint": parent["fingerprint"]})
    intent = atomic_seal(out / "started.json", {"inputs": {"data/source.body": binding(source)}})
    artifact = out / "matrix.csv"
    artifact.write_bytes(b"code,readiness\nfixture,unknown\n")
    report = atomic_seal(
        out / "report.json",
        {
            "schema": "etf_event_audit_v1",
            "intent_fingerprint": intent["fingerprint"],
            "parent_etf_report_fingerprint": parent["fingerprint"],
            **dict.fromkeys(AUTHORITY_FLAGS, False),
            "provider_calls_used": 0,
            "economic_paths_used": 0,
            "model_fits_used": 0,
            "new_funding_feature_ids": [],
            "instruments": [
                {"s1_ab_eligible": False, "f8_eligible": False, "f8_is_required_for_s1_ab": False}
            ],
            "artifacts": {"matrix.csv": binding(artifact)},
        },
    )
    proof = atomic_seal(
        out / "independent_proof.json",
        {
            "report_fingerprint": report["fingerprint"],
            "intent_fingerprint": intent["fingerprint"],
            **dict.fromkeys(AUTHORITY_FLAGS, False),
            **dict.fromkeys(PROOF_CHECKS, True),
            "artifacts": {},
        },
    )
    config = {
        "schema": "etf_event_progress_v1",
        "output": "data/audit",
        "inputs": {},
        "report_fingerprint": report["fingerprint"],
        "proof_fingerprint": proof["fingerprint"],
        "parent_report_path": "data/parent.json",
        "parent_proof_path": "data/parent_proof.json",
        "provider_calls_authorized": 0,
        "economic_paths_authorized": 0,
        "model_fits_authorized": 0,
    }
    (root / CONFIG).parent.mkdir()
    (root / CONFIG).write_text(json.dumps(config))
    return root, out


def bind_changed_report(root, out, changes):
    report = replace_seal(out / "report.json", changes)
    proof = replace_seal(
        out / "independent_proof.json", {"report_fingerprint": report["fingerprint"]}
    )
    config = json.loads((root / CONFIG).read_text())
    config.update(report_fingerprint=report["fingerprint"], proof_fingerprint=proof["fingerprint"])
    (root / CONFIG).write_text(json.dumps(config))


def test_read_only_evidence_does_not_start_any_work(evidence):
    root, _ = evidence
    before = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    report = read_etf_progress(root)
    assert report["execution_authority"] is False
    assert before == sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def test_missing_review_stays_hidden(evidence):
    root, out = evidence
    (out / "independent_proof.json").unlink()
    assert read_etf_progress(root) is None


@pytest.mark.parametrize("name", ["data/source.body", "data/audit/matrix.csv"])
def test_changed_raw_source_or_matrix_is_not_displayed(evidence, name):
    root, _ = evidence
    (root / name).write_bytes(b"changed")
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


@pytest.mark.parametrize("flag", AUTHORITY_FLAGS)
@pytest.mark.parametrize("value", [True, None, 0])
def test_review_cannot_grant_financial_authority(evidence, flag, value):
    root, out = evidence
    bind_changed_report(root, out, {flag: value})
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


def test_event_review_does_not_imply_candidate_admission(evidence):
    root, out = evidence
    bind_changed_report(
        root, out, {"instruments": [{"s1_ab_eligible": True, "f8_eligible": False}]}
    )
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


@pytest.mark.parametrize("flag", PROOF_CHECKS)
def test_missing_arithmetic_review_does_not_display_pass(evidence, flag):
    root, out = evidence
    proof = replace_seal(out / "independent_proof.json", {flag: False})
    config = json.loads((root / CONFIG).read_text())
    config["proof_fingerprint"] = proof["fingerprint"]
    (root / CONFIG).write_text(json.dumps(config))
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


def test_other_intake_proof_cannot_be_substituted(evidence):
    root, _ = evidence
    replace_seal(root / "data/parent_proof.json", {"report_fingerprint": "other"})
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


def test_bound_byte_count_is_checked(evidence):
    root, out = evidence
    entry = binding(out / "matrix.csv")
    entry["bytes"] += 1
    bind_changed_report(root, out, {"artifacts": {"matrix.csv": entry}})
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


def test_artifact_path_must_remain_in_audit_folder(evidence):
    root, out = evidence
    bind_changed_report(
        root, out, {"artifacts": {"../source.body": binding(root / "data/source.body")}}
    )
    with pytest.raises(DataValidationError):
        read_etf_progress(root)


def test_etf_missing_evidence_page_is_read_only(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from quantlab.ui import research_program

    monkeypatch.setattr(research_program, "PROJECT_ROOT", tmp_path)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_etf_progress\nrender_etf_progress()"
    ).run()
    assert not app.exception
    assert "尚未完成复核" in app.info[0].value
    assert not app.button
