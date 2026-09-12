"""Same-cohort attribution and proof-gated display remain separate from promotion."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.funding_attribution import conditional_ic, decompose, read_progress


def rows(n=200):
    return pd.DataFrame(
        {
            "instrument_id": [str(i) for i in range(n)],
            "trade_date": pd.Timestamp("2020-01-02"),
            "momentum20": np.arange(n, dtype=float),
            "F2": np.tile(np.arange(40, dtype=float), 5)[:n],
            "F4": -np.arange(n, dtype=float),
            "label_5": -np.arange(n, dtype=float),
        }
    )


def test_component_comparison_uses_identical_finite_population():
    frame = rows()
    frame.loc[0, "F4"] = np.nan
    frame.loc[1, "F2"] = np.inf
    result = decompose(frame).iloc[0]
    assert result.pairs == 198
    assert result.F4 == pytest.approx(1)
    assert result.reversal_component == pytest.approx(1)
    assert result.F4_minus_reversal == pytest.approx(0)


def test_fixed_momentum_buckets_recover_within_group_direction():
    frame = rows()
    frame.label_5 = frame.F2
    assert conditional_ic(frame) == pytest.approx(1)
    frame.label_5 = -frame.F2
    assert conditional_ic(frame) == pytest.approx(-1)


def test_ties_and_small_bucket_do_not_create_favorable_conditioning():
    frame = rows()
    frame.momentum20 = 0
    assert conditional_ic(frame) is None
    assert conditional_ic(rows(149)) is None


def test_row_order_does_not_change_conditional_association():
    frame = rows()
    first = conditional_ic(frame)
    shuffled = frame.sample(frac=1, random_state=1)
    assert conditional_ic(shuffled) == pytest.approx(first)


def test_duplicate_identity_is_rejected():
    frame = rows()
    with pytest.raises(DataValidationError):
        decompose(pd.concat([frame, frame.iloc[:1]]))


def test_no_valid_label_stays_unknown():
    frame = rows()
    frame.label_5 = np.nan
    result = decompose(frame).iloc[0]
    assert result.pairs == 0 and pd.isna(result.F4_minus_reversal)
    assert pd.isna(result.F2_within_momentum)


@pytest.fixture
def evidence(tmp_path):
    base = tmp_path / "data/pilot"
    base.mkdir(parents=True)
    (base / "features.parquet").write_bytes(b"fixture artifact")
    report = atomic_seal(
        base / "report.json",
        {
            "artifacts": {
                "features.parquet": {"sha256": hashlib.sha256(b"fixture artifact").hexdigest()}
            },
            "historical_pit_certified": False,
            "historical_candidate_selection_eligible": False,
            "execution_authority": False,
            "performance_evidence": False,
        },
    )
    atomic_seal(base / "proof.json", {"report_fingerprint": report["fingerprint"]})
    config = {
        "inputs": {
            p.relative_to(tmp_path).as_posix(): {
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()
            }
            for p in [base / "report.json", base / "proof.json", base / "features.parquet"]
        },
        "pilot_report_path": "data/pilot/report.json",
        "pilot_proof_path": "data/pilot/proof.json",
        "pilot_report_fingerprint": report["fingerprint"],
        "output": "data/attribution",
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config/funding_attribution_v1.json").write_text(json.dumps(config))
    return tmp_path, base, report


def test_missing_proof_hides_result_and_does_not_compute(evidence):
    root, base, _ = evidence
    (base / "proof.json").unlink()
    assert read_progress(root) is None
    assert not (root / "data/attribution").exists()


def test_source_tamper_does_not_display_success(evidence):
    root, base, _ = evidence
    (base / "features.parquet").write_bytes(b"tampered")
    with pytest.raises(DataValidationError):
        read_progress(root)


def test_unverified_attribution_is_hidden_until_matching_proof(evidence):
    root, _, parent = evidence
    out = root / "data/attribution"
    report = atomic_seal(
        out / "report.json",
        {
            "parent_report_fingerprint": parent["fingerprint"],
            "artifacts": {},
            "historical_pit_certified": False,
            "performance_evidence": False,
            "execution_authority": False,
            "candidate_promotion_eligible": False,
        },
    )
    assert read_progress(root)["attribution"] is None
    atomic_seal(out / "independent_proof.json", {"report_fingerprint": report["fingerprint"]})
    assert read_progress(root)["attribution"]["fingerprint"] == report["fingerprint"]


def test_attribution_cannot_grant_execution_authority(evidence):
    root, _, parent = evidence
    out = root / "data/attribution"
    report = atomic_seal(
        out / "report.json",
        {
            "parent_report_fingerprint": parent["fingerprint"],
            "artifacts": {},
            "historical_pit_certified": False,
            "performance_evidence": False,
            "execution_authority": True,
            "candidate_promotion_eligible": False,
        },
    )
    atomic_seal(out / "independent_proof.json", {"report_fingerprint": report["fingerprint"]})
    with pytest.raises(DataValidationError):
        read_progress(root)


def test_app_page_without_artifacts_is_read_only(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from quantlab.ui import research_program

    monkeypatch.setattr(research_program, "PROJECT_ROOT", tmp_path)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_program\nrender_program()"
    ).run()
    assert not app.exception
    assert "尚未完成独立复核" in app.info[0].value
    assert not app.button
