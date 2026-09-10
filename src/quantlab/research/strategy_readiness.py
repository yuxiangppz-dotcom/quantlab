"""Read-only readiness reporting over strategy and forward evidence state.

This module does not transition strategy state. It only describes what evidence
is currently visible and which structural conditions still block user review.
No performance threshold, historical metric, or Forward Shadow return can create
USER_APPROVED authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import EvidenceCatalog
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    IDEA,
    REJECTED,
    STATUSES,
    USER_APPROVED,
    StrategyRegistryEntry,
)


@dataclass(frozen=True)
class StrategyReadinessReport:
    strategy_id: str
    version: str
    role: str
    registry_status: str
    evidence_ref_count: int
    catalog_evidence_id_match_count: int
    catalog_path_match_count: int
    evidence_catalog_fingerprint: str | None
    forward_prediction_count: int
    forward_complete_evaluation_count: int
    forward_incomplete_evaluation_count: int
    forward_pending_prediction_count: int
    ready_for_user_review: bool
    user_approved: bool
    strategy_approval_authority: bool
    broker_order_authority: bool
    blockers: tuple[str, ...]
    report_fingerprint: str


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_registry_entry(entry: StrategyRegistryEntry) -> None:
    if not entry.strategy_id or not entry.version:
        raise DataValidationError("strategy readiness requires non-empty strategy identity")
    if entry.role not in {"baseline", "candidate"}:
        raise DataValidationError("strategy readiness role must be baseline or candidate")
    if entry.status not in STATUSES:
        raise DataValidationError(f"unknown strategy readiness status: {entry.status!r}")
    if len(entry.evidence_refs) != len(set(entry.evidence_refs)):
        raise DataValidationError("strategy readiness evidence references must be unique")
    if any(not isinstance(ref, str) or not ref.strip() for ref in entry.evidence_refs):
        raise DataValidationError("strategy readiness evidence references must be non-empty strings")
    if entry.status not in {IDEA, REJECTED} and not entry.evidence_refs:
        raise DataValidationError(
            f"strategy readiness status {entry.status} requires evidence references"
        )
    if not isinstance(entry.user_approved, bool):
        raise DataValidationError("strategy readiness user_approved must be boolean")
    if entry.status == USER_APPROVED:
        if not entry.user_approved or entry.approval_source != "explicit_user_decision":
            raise DataValidationError(
                "USER_APPROVED readiness requires explicit_user_decision authority"
            )
    elif entry.user_approved or entry.approval_source is not None:
        raise DataValidationError("non-approved readiness entry cannot carry approval authority")
    if entry.status == REJECTED:
        if not isinstance(entry.rejection_reason, str) or not entry.rejection_reason.strip():
            raise DataValidationError("REJECTED readiness entry requires rejection_reason")
    elif entry.rejection_reason is not None:
        raise DataValidationError("non-rejected readiness entry cannot carry rejection_reason")


def _shadow_by_key(
    summaries: Iterable[ShadowDiagnosticSummary],
) -> dict[tuple[str, str], ShadowDiagnosticSummary]:
    result: dict[tuple[str, str], ShadowDiagnosticSummary] = {}
    for summary in summaries:
        key = (summary.model_id, summary.model_version)
        if key in result:
            raise DataValidationError(
                "duplicate Forward Shadow diagnostic identity: "
                f"{summary.model_id}/{summary.model_version}"
            )
        result[key] = summary
    return result


def _catalog_matches(
    entry: StrategyRegistryEntry,
    catalog: EvidenceCatalog | None,
) -> tuple[int, int, str | None]:
    if catalog is None:
        return 0, 0, None
    evidence_ids = {item.evidence_id for item in catalog.entries}
    relative_paths = {item.relative_path for item in catalog.entries}
    evidence_id_matches = sum(ref in evidence_ids for ref in entry.evidence_refs)
    path_matches = sum(ref in relative_paths for ref in entry.evidence_refs)
    return evidence_id_matches, path_matches, catalog.catalog_fingerprint


def _build_one(
    entry: StrategyRegistryEntry,
    shadow: ShadowDiagnosticSummary | None,
    catalog: EvidenceCatalog | None,
) -> StrategyReadinessReport:
    _validate_registry_entry(entry)
    prediction_count = 0 if shadow is None else shadow.prediction_count
    complete_count = 0 if shadow is None else shadow.complete_evaluation_count
    incomplete_count = 0 if shadow is None else shadow.incomplete_evaluation_count
    pending_count = 0 if shadow is None else shadow.pending_prediction_count
    catalog_id_count, catalog_path_count, catalog_fingerprint = _catalog_matches(entry, catalog)

    blockers: list[str] = []
    if entry.status not in {ELIGIBLE_FOR_USER_REVIEW, USER_APPROVED}:
        blockers.append("REGISTRY_STAGE_NOT_ELIGIBLE_FOR_USER_REVIEW")
    if shadow is None:
        blockers.append("FORWARD_SHADOW_SUMMARY_NOT_FOUND")
    else:
        if prediction_count == 0:
            blockers.append("NO_FORWARD_PREDICTIONS")
        if complete_count == 0:
            blockers.append("NO_MATURE_FORWARD_EVALUATIONS")
    if entry.status == ELIGIBLE_FOR_USER_REVIEW and not entry.user_approved:
        blockers.append("EXPLICIT_USER_APPROVAL_REQUIRED")

    ready_for_user_review = (
        entry.status == ELIGIBLE_FOR_USER_REVIEW
        and shadow is not None
        and prediction_count > 0
        and complete_count > 0
    )
    strategy_approval_authority = entry.status == USER_APPROVED and entry.user_approved

    core = {
        "strategy_id": entry.strategy_id,
        "version": entry.version,
        "role": entry.role,
        "registry_status": entry.status,
        "evidence_ref_count": len(entry.evidence_refs),
        "catalog_evidence_id_match_count": catalog_id_count,
        "catalog_path_match_count": catalog_path_count,
        "evidence_catalog_fingerprint": catalog_fingerprint,
        "forward_prediction_count": prediction_count,
        "forward_complete_evaluation_count": complete_count,
        "forward_incomplete_evaluation_count": incomplete_count,
        "forward_pending_prediction_count": pending_count,
        "ready_for_user_review": ready_for_user_review,
        "user_approved": entry.user_approved,
        "strategy_approval_authority": strategy_approval_authority,
        "broker_order_authority": False,
        "blockers": tuple(blockers),
    }
    return StrategyReadinessReport(
        **core,
        report_fingerprint=_canonical_hash(core),
    )


def build_strategy_readiness(
    entries: Iterable[StrategyRegistryEntry],
    *,
    shadow_summaries: Iterable[ShadowDiagnosticSummary] = (),
    evidence_catalog: EvidenceCatalog | None = None,
) -> tuple[StrategyReadinessReport, ...]:
    """Build deterministic, claim-neutral readiness reports for registry entries.

    Forward Shadow counts are structural evidence only. In particular,
    ``ready_for_user_review`` is not a promotion and never changes the registry.
    ``broker_order_authority`` is always false because strategy governance does
    not authorize execution or brokerage activity.
    """
    shadow_map = _shadow_by_key(shadow_summaries)
    seen: set[tuple[str, str]] = set()
    reports = []
    for entry in entries:
        if entry.key in seen:
            raise DataValidationError(
                f"duplicate strategy readiness identity: {entry.strategy_id}/{entry.version}"
            )
        seen.add(entry.key)
        reports.append(_build_one(entry, shadow_map.get(entry.key), evidence_catalog))
    return tuple(sorted(reports, key=lambda item: (item.strategy_id, item.version)))


def readiness_summary(reports: Iterable[StrategyReadinessReport]) -> dict:
    """Return aggregate counts without interpreting investment performance."""
    items = tuple(reports)
    identities = [(item.strategy_id, item.version) for item in items]
    if len(identities) != len(set(identities)):
        raise DataValidationError("duplicate strategy identity in readiness summary")
    core = {
        "schema": "quantlab_strategy_readiness_summary_v1",
        "strategy_count": len(items),
        "ready_for_user_review_count": sum(item.ready_for_user_review for item in items),
        "user_approved_count": sum(item.strategy_approval_authority for item in items),
        "broker_order_authority_count": sum(item.broker_order_authority for item in items),
        "claim": "structural_evidence_readiness_only_not_performance_or_execution_authority",
    }
    return {**core, "summary_fingerprint": _canonical_hash(core)}
