from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_forward_binding import validate_strategy_forward_binding
from quantlab.research.strategy_readiness import build_registered_strategy_readiness


def _forward_model(
    *,
    model_id: str = "shadow_model",
    version: str = "v1",
    source_column: str = "score",
    direction: str = "higher_is_better",
) -> dict:
    return {
        "model_id": model_id,
        "version": version,
        "source_column": source_column,
        "direction": direction,
        "status": "RESEARCH_CANDIDATE_NOT_PROMOTED",
    }


def _strategy(
    *,
    strategy_id: str = "strategy",
    version: str = "v1",
    forward_model_id: str | None = "shadow_model",
    score_source: str = "score higher_is_better",
) -> dict:
    payload = {
        "strategy_id": strategy_id,
        "version": version,
        "role": "candidate",
        "score_source": score_source,
        "status": "FORWARD_EVIDENCE_ACCUMULATING",
        "evidence_refs": ["config/forward.json"],
        "user_approved": False,
    }
    if forward_model_id is not None:
        payload["forward_model_id"] = forward_model_id
    return payload


def _write_pair(
    tmp_path: Path,
    *,
    strategies: list[dict] | None = None,
    models: list[dict] | None = None,
    forward_ref: str = "config/forward.json",
) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    registry_path = config_dir / "registry.json"
    forward_path = config_dir / "forward.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema": "quantlab_strategy_registry_v1",
                "forward_config_ref": forward_ref,
                "strategies": strategies or [_strategy()],
            }
        ),
        encoding="utf-8",
    )
    forward_path.write_text(
        json.dumps(
            {
                "schema": "quantlab_forward_shadow_config_v1",
                "config_id": "forward_test",
                "models": models or [_forward_model()],
            }
        ),
        encoding="utf-8",
    )
    return registry_path, forward_path


def _shadow(
    model_id: str,
    version: str,
    *,
    predictions: int = 1,
    complete: int = 0,
) -> ShadowDiagnosticSummary:
    return ShadowDiagnosticSummary(
        model_id=model_id,
        model_version=version,
        prediction_count=predictions,
        complete_evaluation_count=complete,
        incomplete_evaluation_count=0,
        pending_prediction_count=predictions - complete,
        first_signal_date="2026-09-09" if predictions else None,
        last_signal_date="2026-09-09" if predictions else None,
        mean_weighted_target_return=0.01 if complete else None,
        median_weighted_target_return=0.01 if complete else None,
        positive_rate=1.0 if complete else None,
    )


def test_repository_registry_explicitly_binds_baseline_name_mismatch() -> None:
    audit = validate_strategy_forward_binding(
        Path("config/strategy_registry_v1.json"),
        Path("config/forward_shadow_v1.json"),
    )

    by_strategy = {item.strategy_id: item for item in audit.bindings}
    assert (
        by_strategy["momentum_20d_reversal_example"].forward_model_id
        == "return_20d_baseline"
    )
    assert (
        by_strategy["transparent_combo_v1"].forward_model_id
        == "transparent_combo_v1"
    )
    assert "price_volume_base_breakout" not in by_strategy
    assert audit.forward_config_id == "forward_shadow_v1"
    assert len(audit.binding_fingerprint) == 64


def test_registered_readiness_uses_binding_not_strategy_name() -> None:
    reports = build_registered_strategy_readiness(
        Path("config/strategy_registry_v1.json"),
        Path("config/forward_shadow_v1.json"),
        shadow_summaries=[
            _shadow("return_20d_baseline", "daily_mvp_v1", predictions=3),
            _shadow(
                "transparent_combo_v1",
                "v1_frozen_forward_candidate",
                predictions=2,
            ),
        ],
    )
    by_strategy = {item.strategy_id: item for item in reports}

    baseline = by_strategy["momentum_20d_reversal_example"]
    assert baseline.forward_model_id == "return_20d_baseline"
    assert baseline.forward_prediction_count == 3
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" not in baseline.blockers
    assert baseline.strategy_forward_binding_fingerprint is not None

    s5b = by_strategy["price_volume_base_breakout"]
    assert s5b.registry_status == "RESEARCHED"
    assert s5b.forward_prediction_count == 0
    assert s5b.ready_for_user_review is False
    assert s5b.user_approved is False
    assert s5b.broker_order_authority is False
    assert "REGISTRY_STAGE_NOT_ELIGIBLE_FOR_USER_REVIEW" in s5b.blockers
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" in s5b.blockers


def test_missing_forward_model_id_fails_closed(tmp_path: Path) -> None:
    registry, forward = _write_pair(
        tmp_path,
        strategies=[_strategy(forward_model_id=None)],
    )

    with pytest.raises(DataValidationError, match="forward_model_id"):
        validate_strategy_forward_binding(registry, forward)


def test_orphan_forward_model_fails_closed(tmp_path: Path) -> None:
    registry, forward = _write_pair(
        tmp_path,
        models=[_forward_model(), _forward_model(model_id="orphan")],
    )

    with pytest.raises(DataValidationError, match="ownership mismatch"):
        validate_strategy_forward_binding(registry, forward)


def test_two_strategies_cannot_claim_same_shadow_model(tmp_path: Path) -> None:
    registry, forward = _write_pair(
        tmp_path,
        strategies=[
            _strategy(strategy_id="first"),
            _strategy(strategy_id="second"),
        ],
    )

    with pytest.raises(DataValidationError, match="claimed by multiple strategies"):
        validate_strategy_forward_binding(registry, forward)


def test_version_drift_fails_closed(tmp_path: Path) -> None:
    registry, forward = _write_pair(
        tmp_path,
        models=[_forward_model(version="v2")],
    )

    with pytest.raises(DataValidationError, match="does not resolve"):
        validate_strategy_forward_binding(registry, forward)


@pytest.mark.parametrize(
    "score_source",
    ["other higher_is_better", "score lower_is_better"],
)
def test_score_source_or_direction_drift_fails_closed(
    tmp_path: Path,
    score_source: str,
) -> None:
    registry, forward = _write_pair(
        tmp_path,
        strategies=[_strategy(score_source=score_source)],
    )

    with pytest.raises(DataValidationError, match="score_source"):
        validate_strategy_forward_binding(registry, forward)


def test_forward_config_ref_must_identify_the_exact_file(tmp_path: Path) -> None:
    registry, forward = _write_pair(
        tmp_path,
        forward_ref="config/not-forward.json",
    )

    with pytest.raises(DataValidationError, match="does not match"):
        validate_strategy_forward_binding(registry, forward)


@pytest.mark.parametrize("forward_ref", [".", "../forward.json", "/tmp/forward.json"])
def test_forward_config_ref_rejects_unsafe_paths(
    tmp_path: Path,
    forward_ref: str,
) -> None:
    registry, forward = _write_pair(tmp_path, forward_ref=forward_ref)

    with pytest.raises(DataValidationError, match="safe repository-relative path"):
        validate_strategy_forward_binding(registry, forward)
