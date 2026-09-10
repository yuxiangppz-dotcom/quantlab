from __future__ import annotations

from dataclasses import replace

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import EvidenceCatalog, EvidenceCatalogEntry
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_readiness import (
    build_strategy_readiness,
    readiness_summary,
)
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    FORWARD_EVIDENCE_ACCUMULATING,
    REJECTED,
    USER_APPROVED,
    StrategyRegistryEntry,
)


def _entry(status: str = FORWARD_EVIDENCE_ACCUMULATING) -> StrategyRegistryEntry:
    approved = status == USER_APPROVED
    rejected = status == REJECTED
    return StrategyRegistryEntry(
        strategy_id="candidate",
        version="v1",
        role="candidate",
        score_source="score higher_is_better",
        status=status,
        evidence_refs=("factor/run-1/summary.json", "config/forward_shadow_v1.json"),
        user_approved=approved,
        approval_source="explicit_user_decision" if approved else None,
        rejection_reason="hypothesis rejected" if rejected else None,
    )


def _shadow(
    *,
    predictions: int = 3,
    complete: int = 0,
    incomplete: int = 0,
    pending: int | None = None,
) -> ShadowDiagnosticSummary:
    pending = predictions - complete - incomplete if pending is None else pending
    return ShadowDiagnosticSummary(
        model_id="candidate",
        model_version="v1",
        prediction_count=predictions,
        complete_evaluation_count=complete,
        incomplete_evaluation_count=incomplete,
        pending_prediction_count=pending,
        first_signal_date="2026-09-09" if predictions else None,
        last_signal_date="2026-09-11" if predictions else None,
        mean_weighted_target_return=0.02 if complete else None,
        median_weighted_target_return=0.02 if complete else None,
        positive_rate=1.0 if complete else None,
    )


def _catalog() -> EvidenceCatalog:
    entry = EvidenceCatalogEntry(
        evidence_id="e" * 64,
        relative_path="factor/run-1/summary.json",
        schema="test_evidence",
        run_id="run-1",
        file_sha256="f" * 64,
        code_head="a" * 40,
        history_status="retrospective_not_fresh_oos",
        performance_claim=False,
        strategy_promoted=False,
    )
    return EvidenceCatalog(entries=(entry,), issues=(), catalog_fingerprint="c" * 64)


def test_accumulating_strategy_reports_pending_forward_blockers() -> None:
    report = build_strategy_readiness([_entry()], shadow_summaries=[_shadow()])[0]

    assert report.forward_prediction_count == 3
    assert report.forward_complete_evaluation_count == 0
    assert report.forward_pending_prediction_count == 3
    assert report.ready_for_user_review is False
    assert report.user_approved is False
    assert report.strategy_approval_authority is False
    assert report.broker_order_authority is False
    assert "REGISTRY_STAGE_NOT_ELIGIBLE_FOR_USER_REVIEW" in report.blockers
    assert "NO_MATURE_FORWARD_EVALUATIONS" in report.blockers
    assert len(report.report_fingerprint) == 64


def test_eligible_strategy_with_mature_forward_evidence_still_requires_user_approval() -> None:
    entry = _entry(ELIGIBLE_FOR_USER_REVIEW)
    report = build_strategy_readiness(
        [entry],
        shadow_summaries=[_shadow(complete=2, pending=1)],
        evidence_catalog=_catalog(),
    )[0]

    assert report.ready_for_user_review is True
    assert report.catalog_evidence_id_match_count == 0
    assert report.catalog_path_match_count == 1
    assert report.evidence_catalog_fingerprint == "c" * 64
    assert report.strategy_approval_authority is False
    assert report.broker_order_authority is False
    assert report.blockers == ("EXPLICIT_USER_APPROVAL_REQUIRED",)


def test_user_approved_strategy_does_not_create_broker_authority() -> None:
    report = build_strategy_readiness(
        [_entry(USER_APPROVED)],
        shadow_summaries=[_shadow(complete=1)],
    )[0]

    assert report.user_approved is True
    assert report.strategy_approval_authority is True
    assert report.ready_for_user_review is False
    assert report.broker_order_authority is False
    assert report.blockers == ()

    summary = readiness_summary([report])
    assert summary["user_approved_count"] == 1
    assert summary["broker_order_authority_count"] == 0
    assert summary["claim"].startswith("structural_evidence_readiness_only")


def test_missing_shadow_is_explicitly_visible() -> None:
    report = build_strategy_readiness([_entry()])[0]

    assert report.forward_prediction_count == 0
    assert report.forward_complete_evaluation_count == 0
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" in report.blockers


def test_forged_approval_and_missing_advanced_evidence_fail_closed() -> None:
    forged = replace(
        _entry(USER_APPROVED),
        approval_source="historical_metric_threshold",
    )
    with pytest.raises(DataValidationError, match="explicit_user_decision"):
        build_strategy_readiness([forged], shadow_summaries=[_shadow(complete=1)])

    missing_evidence = replace(_entry(), evidence_refs=())
    with pytest.raises(DataValidationError, match="requires evidence references"):
        build_strategy_readiness([missing_evidence], shadow_summaries=[_shadow()])


def test_rejected_entry_requires_reason_even_if_constructed_directly() -> None:
    forged = replace(_entry(REJECTED), rejection_reason=None)

    with pytest.raises(DataValidationError, match="requires rejection_reason"):
        build_strategy_readiness([forged])


def test_duplicate_shadow_or_strategy_identity_fails_closed() -> None:
    with pytest.raises(DataValidationError, match="duplicate Forward Shadow diagnostic identity"):
        build_strategy_readiness([_entry()], shadow_summaries=[_shadow(), _shadow()])

    with pytest.raises(DataValidationError, match="duplicate strategy readiness identity"):
        build_strategy_readiness([_entry(), replace(_entry(), notes="duplicate")])


def test_readiness_summary_rejects_duplicate_identity() -> None:
    report = build_strategy_readiness(
        [_entry(ELIGIBLE_FOR_USER_REVIEW)],
        shadow_summaries=[_shadow(complete=1)],
    )[0]

    with pytest.raises(DataValidationError, match="duplicate strategy identity"):
        readiness_summary([report, report])
