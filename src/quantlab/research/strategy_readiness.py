"""Read-only readiness reporting over strategy and forward evidence state.

This module does not transition strategy state. It only describes what evidence
is currently visible and which structural conditions still block user review.
No performance threshold, historical metric, or Forward Shadow return can create
USER_APPROVED authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Iterable

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import EvidenceCatalog
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
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
    catalog_bound_evidence_count: int
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


def _shadow_by_key(
    summaries: Iterable[ShadowDiagnosticSummary],
) -> dict[tuple[str, str], ShadowDiagnosticSummary]:
    result: dict[tuple[str, str], ShadowDiagnosticSummary] = {}
    for summary in summaries:
        key = (summary.model_id, summary.model_version)
        if key in result:
            raise DataValidationError(
                f"duplicate Forward Shadow diagnostic identity: {summary.model_id}/{summary.model_version}"
            )
        result[key] = summary
    return result


def _catalog_bound_refs(entry: StrategyRegistryEntry, catalog: EvidenceCatalog | None) -> int:
    if catalog is None:
        return 0
    evidence_ids = {item.evidence_id for item in catalog.entries}
    relative_paths = {item.relative_path for item in catalog.entries}
    return sum(
        ref in evidence_ids or ref in relative_paths
        for ref in entry.evidence_refs
    )


def _build_one(
    entry: StrategyRegistryEntry,
    shadow: ShadowDiagnosticSummary | None,
    catalog: EvidenceCatalog | None,
) -> StrategyReadinessReport:
    prediction_count = 0 if shadow is None else shadow.prediction_count
    complete_count = 0 if shadow is None else shadow.complete_evaluation_count
    incomplete_count = 0 if shadow is None else shadow.incomplete_evaluation_count
    pending_count = 0 if shadow is None else shadow.pending_prediction_count

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
        "catalog_bound_evidence_count": _catalog_bound_refs(entry, catalog),
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
