from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research import status_service


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "schema": "quantlab_strategy_registry_v1",
                "strategies": [
                    {
                        "strategy_id": "candidate",
                        "version": "v1",
                        "role": "candidate",
                        "score_source": "score higher_is_better",
                        "status": "FORWARD_EVIDENCE_ACCUMULATING",
                        "evidence_refs": ["experiment/run-1/summary.json"],
                        "user_approved": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _shadow() -> ShadowDiagnosticSummary:
    return ShadowDiagnosticSummary(
        model_id="candidate",
        model_version="v1",
        prediction_count=3,
        complete_evaluation_count=1,
        incomplete_evaluation_count=0,
        pending_prediction_count=2,
        first_signal_date="2026-09-09",
        last_signal_date="2026-09-11",
        mean_weighted_target_return=0.01,
        median_weighted_target_return=0.01,
        positive_rate=1.0,
    )


def test_missing_local_evidence_roots_are_explicit_not_fabricated(tmp_path: Path) -> None:
    payload = status_service.build_research_status(
        registry_path=_registry(tmp_path),
        experiment_root=tmp_path / "missing-experiments",
        shadow_root=tmp_path / "missing-shadow",
    )

    assert payload["sources"]["experiment_root_present"] is False
    assert payload["sources"]["forward_shadow_root_present"] is False
    assert payload["evidence_catalog"]["entry_count"] == 0
    assert payload["forward_shadow"] == []
    assert payload["strategies"][0]["forward_prediction_count"] == 0
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" in payload["strategies"][0]["blockers"]
    assert payload["claims"]["performance_claim"] is False
    assert payload["claims"]["broker_order_authority"] is False
    assert len(payload["status_fingerprint"]) == 64


def test_status_composes_catalog_and_forward_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment_root = tmp_path / "experiments"
    summary = experiment_root / "experiment" / "run-1" / "summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(
        json.dumps(
            {
                "schema": "test_research_evidence_v1",
                "run_id": "run-1",
                "code_head": "a" * 40,
                "history_status": "retrospective_not_fresh_oos",
                "performance_claim": False,
                "strategy_promoted": False,
            }
        ),
        encoding="utf-8",
    )
    shadow_root = tmp_path / "shadow"
    shadow_root.mkdir()
    monkeypatch.setattr(status_service, "summarize_forward_shadow", lambda root: [_shadow()])

    payload = status_service.build_research_status(
        registry_path=_registry(tmp_path),
        experiment_root=experiment_root,
        shadow_root=shadow_root,
    )

    assert payload["evidence_catalog"]["entry_count"] == 1
    assert payload["forward_shadow"][0]["complete_evaluation_count"] == 1
    strategy = payload["strategies"][0]
    assert strategy["forward_prediction_count"] == 3
    assert strategy["forward_complete_evaluation_count"] == 1
    assert strategy["broker_order_authority"] is False
    assert payload["readiness"]["broker_order_authority_count"] == 0


def test_existing_malformed_evidence_fails_closed(tmp_path: Path) -> None:
    experiment_root = tmp_path / "experiments"
    bad = experiment_root / "bad" / "summary.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{}", encoding="utf-8")

    with pytest.raises(DataValidationError, match="evidence schema"):
        status_service.build_research_status(
            registry_path=_registry(tmp_path),
            experiment_root=experiment_root,
            shadow_root=tmp_path / "missing-shadow",
        )


def test_human_format_never_labels_broker_authority_true(tmp_path: Path) -> None:
    payload = status_service.build_research_status(
        registry_path=_registry(tmp_path),
        experiment_root=tmp_path / "missing-experiments",
        shadow_root=tmp_path / "missing-shadow",
    )
    text = status_service.format_research_status(payload)

    assert "QuantLab research status" in text
    assert "broker order authority: false" in text
    assert "candidate@v1: FORWARD_EVIDENCE_ACCUMULATING" in text
    assert payload["status_fingerprint"] in text
