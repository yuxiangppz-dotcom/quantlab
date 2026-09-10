from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from quantlab.research.forward_shadow_analytics import (
    paired_shadow_diagnostics,
    summarize_forward_shadow,
)
from quantlab.research.shadow_timing import PREDICTION_V2, temporal_admission


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_prediction(
    root: Path,
    *,
    model_id: str,
    version: str,
    signal_date: str,
) -> str:
    scores = b"instrument_id,alpha_score\n000001.SZ,1.0\n"
    target = b"instrument_id,target_weight\n000001.SZ,1.0\n"
    files_sha = {
        "scores.csv": hashlib.sha256(scores).hexdigest(),
        "target_portfolio.csv": hashlib.sha256(target).hexdigest(),
    }
    core = {
        "schema": PREDICTION_V2,
        "trade_date": signal_date,
        "created_at": f"{signal_date}T18:00:00+08:00",
        "source_daily_generated_at": f"{signal_date}T17:00:00+08:00",
        "daily_report_sha256": "a" * 64,
        "temporal_admission": temporal_admission(
            signal_date, f"{signal_date}T18:00:00+08:00", f"{signal_date}T17:00:00+08:00",
        ),
        "model": {"model_id": model_id, "version": version, "status": "candidate"},
        "config_id": "test",
        "label": {"name": "diagnostic_future_return_20d", "horizon_sessions": 20},
    }
    fingerprint = _hash({"core": core, "files_sha256": files_sha})
    out = root / model_id / version / signal_date / fingerprint
    out.mkdir(parents=True)
    (out / "scores.csv").write_bytes(scores)
    (out / "target_portfolio.csv").write_bytes(target)
    manifest = {
        **core,
        "prediction_fingerprint": fingerprint,
        "created_at": f"{signal_date}T18:00:00+08:00",
        "files_sha256": files_sha,
        "claims": {
            "broker_order": False,
            "fill": False,
            "performance": False,
            "forward_evidence_matured": False,
        },
    }
    (out / "prediction.json").write_text(json.dumps(manifest), encoding="utf-8")
    return fingerprint


def _write_evaluation(
    root: Path,
    *,
    model_id: str,
    version: str,
    signal_date: str,
    prediction_fingerprint: str,
    status: str,
    value: float | None,
) -> Path:
    core = {
        "schema": "quantlab_forward_shadow_evaluation_v1",
        "prediction_fingerprint": prediction_fingerprint,
        "model_id": model_id,
        "model_version": version,
        "signal_date": signal_date,
        "label_date": "2026-10-10",
        "label": "diagnostic_future_return_20d",
        "status": status,
        "target_count": 1,
        "missing_target_count": 0 if status == "complete" else 1,
        "missing_target_sample": [] if status == "complete" else ["000001.SZ"],
        "weighted_target_return": value,
        "label_input_sha256": {},
        "not_a_backtest_or_execution_claim": True,
    }
    payload = {**core, "evaluation_fingerprint": _hash(core)}
    out = root / model_id / version / signal_date / prediction_fingerprint / "evaluation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    return out


def test_summary_separates_complete_incomplete_and_pending_predictions(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    evaluations = tmp_path / "evaluations"
    first = _write_prediction(shadow, model_id="candidate", version="v1", signal_date="2026-09-01")
    second = _write_prediction(shadow, model_id="candidate", version="v1", signal_date="2026-09-02")
    _write_prediction(shadow, model_id="candidate", version="v1", signal_date="2026-09-03")
    _write_evaluation(
        evaluations,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-01",
        prediction_fingerprint=first,
        status="complete",
        value=0.12,
    )
    _write_evaluation(
        evaluations,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-02",
        prediction_fingerprint=second,
        status="incomplete_missing_target_label",
        value=None,
    )

    result = summarize_forward_shadow(shadow, evaluation_root=evaluations)[0]

    assert result.prediction_count == 3
    assert result.complete_evaluation_count == 1
    assert result.incomplete_evaluation_count == 1
    assert result.pending_prediction_count == 1
    assert result.first_signal_date == "2026-09-01"
    assert result.last_signal_date == "2026-09-03"
    assert result.mean_weighted_target_return == pytest.approx(0.12)
    assert result.median_weighted_target_return == pytest.approx(0.12)
    assert result.positive_rate == pytest.approx(1.0)


def test_paired_diagnostic_matches_only_same_date_complete_labels(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    evaluations = tmp_path / "evaluations"
    values = {
        ("candidate", "2026-09-01"): 0.10,
        ("baseline", "2026-09-01"): 0.04,
        ("candidate", "2026-09-02"): -0.02,
        ("baseline", "2026-09-02"): 0.01,
    }
    for (model_id, signal_date), value in values.items():
        fingerprint = _write_prediction(
            shadow,
            model_id=model_id,
            version="v1",
            signal_date=signal_date,
        )
        _write_evaluation(
            evaluations,
            model_id=model_id,
            version="v1",
            signal_date=signal_date,
            prediction_fingerprint=fingerprint,
            status="complete",
            value=value,
        )

    result = paired_shadow_diagnostics(
        shadow,
        ("candidate", "v1"),
        ("baseline", "v1"),
        evaluation_root=evaluations,
    )

    assert result["paired_complete_count"] == 2
    assert result["mean_paired_difference"] == pytest.approx(0.015)
    assert result["left_win_rate"] == pytest.approx(0.5)
    assert result["claim"] == "overlapping_label_diagnostic_not_portfolio_active_return"


def test_tampered_evaluation_fails_closed(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    evaluations = tmp_path / "evaluations"
    fingerprint = _write_prediction(
        shadow,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-01",
    )
    path = _write_evaluation(
        evaluations,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-01",
        prediction_fingerprint=fingerprint,
        status="complete",
        value=0.1,
    )
    payload = json.loads(path.read_text())
    payload["weighted_target_return"] = 9.9
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="evaluation fingerprint mismatch"):
        summarize_forward_shadow(shadow, evaluation_root=evaluations)


def test_orphan_evaluation_fails_closed(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    evaluations = tmp_path / "evaluations"
    _write_prediction(shadow, model_id="candidate", version="v1", signal_date="2026-09-01")
    _write_evaluation(
        evaluations,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-02",
        prediction_fingerprint="f" * 64,
        status="complete",
        value=0.1,
    )

    with pytest.raises(ValueError, match="unknown prediction"):
        summarize_forward_shadow(shadow, evaluation_root=evaluations)


def test_unknown_evaluation_status_fails_closed(tmp_path: Path) -> None:
    shadow = tmp_path / "shadow"
    evaluations = tmp_path / "evaluations"
    fingerprint = _write_prediction(
        shadow,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-01",
    )
    _write_evaluation(
        evaluations,
        model_id="candidate",
        version="v1",
        signal_date="2026-09-01",
        prediction_fingerprint=fingerprint,
        status="mystery",
        value=None,
    )

    with pytest.raises(ValueError, match="unsupported forward-shadow evaluation status"):
        summarize_forward_shadow(shadow, evaluation_root=evaluations)
