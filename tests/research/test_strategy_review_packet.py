from __future__ import annotations

import json
from pathlib import Path

from quantlab.research.evidence_catalog import EvidenceCatalog, EvidenceCatalogEntry
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_review_packet import build_strategy_review_bundle


def _shadow(model_id: str, version: str, predictions: int = 2) -> ShadowDiagnosticSummary:
    return ShadowDiagnosticSummary(
        model_id=model_id,
        model_version=version,
        prediction_count=predictions,
        complete_evaluation_count=0,
        incomplete_evaluation_count=0,
        pending_prediction_count=predictions,
        first_signal_date="2026-09-09",
        last_signal_date="2026-09-10",
        mean_weighted_target_return=None,
        median_weighted_target_return=None,
        positive_rate=None,
    )


def _write_config_pair(
    tmp_path: Path,
    *,
    strategies: list[dict],
    models: list[dict],
) -> tuple[Path, Path]:
    config = tmp_path / "config"
    config.mkdir()
    registry = config / "registry.json"
    forward = config / "forward.json"
    registry.write_text(
        json.dumps(
            {
                "schema": "quantlab_strategy_registry_v1",
                "forward_config_ref": "config/forward.json",
                "strategies": strategies,
            }
        ),
        encoding="utf-8",
    )
    forward.write_text(
        json.dumps(
            {
                "schema": "quantlab_forward_shadow_config_v1",
                "config_id": "forward_test",
                "models": models,
            }
        ),
        encoding="utf-8",
    )
    return registry, forward


def _model(model_id: str, version: str, source: str) -> dict:
    return {
        "model_id": model_id,
        "version": version,
        "source_column": source,
        "direction": "higher_is_better",
        "status": "RESEARCH_CANDIDATE_NOT_PROMOTED",
    }


def _strategy(
    strategy_id: str,
    version: str,
    model_id: str,
    source: str,
    *,
    status: str = "FORWARD_EVIDENCE_ACCUMULATING",
    evidence_refs: list[str] | None = None,
) -> dict:
    approved = status == "USER_APPROVED"
    return {
        "strategy_id": strategy_id,
        "version": version,
        "role": "candidate",
        "score_source": f"{source} higher_is_better",
        "status": status,
        "forward_model_id": model_id,
        "evidence_refs": evidence_refs or ["config/forward.json"],
        "user_approved": approved,
        "approval_source": "explicit_user_decision" if approved else None,
    }


def test_repository_packet_resolves_existing_baseline_model_name_mismatch() -> None:
    bundle = build_strategy_review_bundle(
        Path("config/strategy_registry_v1.json"),
        Path("config/forward_shadow_v1.json"),
        shadow_summaries=(
            _shadow("return_20d_baseline", "daily_mvp_v1", 3),
            _shadow("transparent_combo_v1", "v1_frozen_forward_candidate", 2),
        ),
    )
    by_strategy = {item.strategy_id: item for item in bundle.packets}

    baseline = by_strategy["momentum_20d_reversal_example"]
    assert baseline.forward_model_id == "return_20d_baseline"
    assert baseline.forward_prediction_count == 3
    assert baseline.broker_order_authority is False
    assert bundle.evidence_catalog_fingerprint is None
    assert len(bundle.strategy_forward_binding_fingerprint) == 64
    assert len(bundle.bundle_fingerprint) == 64


def test_packet_distinguishes_content_id_mutable_path_and_static_refs(tmp_path: Path) -> None:
    catalog_entry = EvidenceCatalogEntry(
        evidence_id="e" * 64,
        relative_path="factor/run-1/summary.json",
        schema="factor_run",
        run_id="run-1",
        file_sha256="f" * 64,
        code_head="a" * 40,
        history_status="retrospective_not_fresh_oos",
        performance_claim=False,
        strategy_promoted=False,
    )
    catalog = EvidenceCatalog(
        entries=(catalog_entry,),
        issues=(),
        catalog_fingerprint="c" * 64,
    )
    registry, forward = _write_config_pair(
        tmp_path,
        strategies=[
            _strategy(
                "candidate",
                "v1",
                "shadow",
                "score",
                evidence_refs=[
                    "e" * 64,
                    "factor/run-1/summary.json",
                    "config/forward.json",
                ],
            )
        ],
        models=[_model("shadow", "v1", "score")],
    )

    packet = build_strategy_review_bundle(
        registry,
        forward,
        evidence_catalog=catalog,
    ).packets[0]
    by_ref = {item.reference: item for item in packet.evidence_references}

    assert by_ref["e" * 64].reference_kind == "content_bound_evidence_id"
    assert by_ref["e" * 64].mutable_catalog_path is False
    path_view = by_ref["factor/run-1/summary.json"]
    assert path_view.reference_kind == "catalog_path_reference"
    assert path_view.matched_evidence_id == "e" * 64
    assert path_view.mutable_catalog_path is True
    assert by_ref["config/forward.json"].reference_kind == "static_or_unresolved_reference"
    assert packet.catalog_evidence_id_match_count == 1
    assert packet.catalog_path_match_count == 1


def test_bundle_order_and_fingerprint_are_deterministic(tmp_path: Path) -> None:
    registry, forward = _write_config_pair(
        tmp_path,
        strategies=[
            _strategy("z_strategy", "v1", "z_model", "z_score"),
            _strategy("a_strategy", "v1", "a_model", "a_score"),
        ],
        models=[
            _model("a_model", "v1", "a_score"),
            _model("z_model", "v1", "z_score"),
        ],
    )
    summaries = (
        _shadow("z_model", "v1"),
        _shadow("a_model", "v1"),
    )

    first = build_strategy_review_bundle(
        registry,
        forward,
        shadow_summaries=summaries,
    )
    second = build_strategy_review_bundle(
        registry,
        forward,
        shadow_summaries=tuple(reversed(summaries)),
    )

    assert [item.strategy_id for item in first.packets] == ["a_strategy", "z_strategy"]
    assert first.bundle_fingerprint == second.bundle_fingerprint
    assert [item.packet_fingerprint for item in first.packets] == [
        item.packet_fingerprint for item in second.packets
    ]


def test_user_approved_packet_never_grants_broker_authority(tmp_path: Path) -> None:
    registry, forward = _write_config_pair(
        tmp_path,
        strategies=[
            _strategy(
                "candidate",
                "v1",
                "shadow",
                "score",
                status="USER_APPROVED",
            )
        ],
        models=[_model("shadow", "v1", "score")],
    )

    packet = build_strategy_review_bundle(
        registry,
        forward,
        shadow_summaries=(_shadow("shadow", "v1"),),
    ).packets[0]

    assert packet.user_approved is True
    assert packet.strategy_approval_authority is True
    assert packet.broker_order_authority is False
