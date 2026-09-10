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
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import EvidenceCatalog
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_forward_binding import (
    StrategyForwardBinding,
    validate_strategy_forward_binding,
)
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    FORWARD_EVIDENCE_ACCUMULATING,
    FORWARD_SHADOW,
    IDEA,
    REJECTED,
    STATUSES,
    USER_APPROVED,
    StrategyRegistryEntry,
    load_strategy_registry,
)

_FORWARD_STATES = {
    FORWARD_SHADOW,
    FORWARD_EVIDENCE_ACCUMULATING,
    ELIGIBLE_FOR_USER_REVIEW,
    USER_APPROVED,
}


@dataclass(frozen=True)
class StrategyReadinessReport:
    strategy_id: str
    version: str
    role: str
    registry_status: str
    forward_model_id: str | None
    strategy_forward_binding_fingerprint: str | None
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
        raise DataValidationError(
            "strategy readiness evidence references must be non-empty strings"
        )
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
        raise DataValidationError(
            "non-approved readiness entry cannot carry approval authority"
        )
    if entry.status == REJECTED:
        if not isinstance(entry.rejection_reason, str) or not entry.rejection_reason.strip():
            raise DataValidationError(
                "REJECTED readiness entry requires rejection_reason"
            )
    elif entry.rejection_reason is not None:
        raise DataValidationError(
            "non-rejected readiness entry cannot carry rejection_reason"
        )


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


def _binding_by_strategy(
    bindings: Iterable[StrategyForwardBinding],
) -> dict[tuple[str, str], StrategyForwardBinding]:
    result: dict[tuple[str, str], StrategyForwardBinding] = {}
    claimed_models: set[tuple[str, str]] = set()
    for binding in bindings:
        if binding.strategy_key in result:
            raise DataValidationError(
                "duplicate strategy identity in readiness forward bindings"
            )
        if binding.model_key in claimed_models:
            raise DataValidationError(
                "duplicate Forward Shadow model ownership in readiness bindings"
            )
        result[binding.strategy_key] = binding
        claimed_models.add(binding.model_key)
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
    *,
    forward_model_id: str | None,
    binding_fingerprint: str | None,
    binding_required: bool,
) -> StrategyReadinessReport:
    _validate_registry_entry(entry)
    prediction_count = 0 if shadow is None else shadow.prediction_count
    complete_count = 0 if shadow is None else shadow.complete_evaluation_count
    incomplete_count = 0 if shadow is None else shadow.incomplete_evaluation_count
    pending_count = 0 if shadow is None else shadow.pending_prediction_count
    catalog_id_count, catalog_path_count, catalog_fingerprint = _catalog_matches(
        entry,
        catalog,
    )

    blockers: list[str] = []
    if entry.status not in {ELIGIBLE_FOR_USER_REVIEW, USER_APPROVED}:
        blockers.append("REGISTRY_STAGE_NOT_ELIGIBLE_FOR_USER_REVIEW")
    if binding_required and forward_model_id is None:
        blockers.append("FORWARD_MODEL_BINDING_NOT_FOUND")
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
        and (not binding_required or forward_model_id is not None)
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
        "forward_model_id": forward_model_id,
        "strategy_forward_binding_fingerprint": binding_fingerprint,
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
    forward_bindings: Iterable[StrategyForwardBinding] | None = None,
    binding_fingerprint: str | None = None,
) -> tuple[StrategyReadinessReport, ...]:
    """Build deterministic, claim-neutral readiness reports for registry entries.

    Passing ``forward_bindings`` disables the legacy identity assumption and
    resolves diagnostics through explicit strategy → Forward Shadow ownership.
    Canonical repository workflows should use ``build_registered_strategy_readiness``.
    """
    shadow_map = _shadow_by_key(shadow_summaries)
    binding_required = forward_bindings is not None
    binding_map = _binding_by_strategy(forward_bindings or ())
    seen: set[tuple[str, str]] = set()
    reports = []
    for entry in entries:
        if entry.key in seen:
            raise DataValidationError(
                f"duplicate strategy readiness identity: {entry.strategy_id}/{entry.version}"
            )
        seen.add(entry.key)
        binding = binding_map.get(entry.key)
        if binding is not None and binding.role != entry.role:
            raise DataValidationError(
                "strategy readiness binding role differs from registry role"
            )
        if binding is not None:
            forward_model_id = binding.forward_model_id
            shadow_key = binding.model_key
        elif binding_required and entry.status in _FORWARD_STATES:
            forward_model_id = None
            shadow_key = None
        else:
            forward_model_id = entry.strategy_id
            shadow_key = entry.key
        shadow = shadow_map.get(shadow_key) if shadow_key is not None else None
        reports.append(
            _build_one(
                entry,
                shadow,
                evidence_catalog,
                forward_model_id=forward_model_id,
                binding_fingerprint=binding_fingerprint,
                binding_required=binding_required and entry.status in _FORWARD_STATES,
            )
        )
    return tuple(sorted(reports, key=lambda item: (item.strategy_id, item.version)))


def build_registered_strategy_readiness(
    registry_path: Path,
    forward_config_path: Path,
    *,
    shadow_summaries: Iterable[ShadowDiagnosticSummary] = (),
    evidence_catalog: EvidenceCatalog | None = None,
) -> tuple[StrategyReadinessReport, ...]:
    """Build readiness through the validated repository strategy/shadow binding."""
    binding_audit = validate_strategy_forward_binding(
        registry_path,
        forward_config_path,
    )
    entries = load_strategy_registry(registry_path)
    return build_strategy_readiness(
        entries,
        shadow_summaries=shadow_summaries,
        evidence_catalog=evidence_catalog,
        forward_bindings=binding_audit.bindings,
        binding_fingerprint=binding_audit.binding_fingerprint,
    )


def readiness_summary(reports: Iterable[StrategyReadinessReport]) -> dict:
    """Return aggregate counts without interpreting investment performance."""
    items = tuple(reports)
    identities = [(item.strategy_id, item.version) for item in items]
    if len(identities) != len(set(identities)):
        raise DataValidationError("duplicate strategy identity in readiness summary")
    core = {
        "schema": "quantlab_strategy_readiness_summary_v1",
        "strategy_count": len(items),
        "ready_for_user_review_count": sum(
            item.ready_for_user_review for item in items
        ),
        "user_approved_count": sum(
            item.strategy_approval_authority for item in items
        ),
        "broker_order_authority_count": sum(
            item.broker_order_authority for item in items
        ),
        "claim": (
            "structural_evidence_readiness_only_not_performance_or_execution_authority"
        ),
    }
    return {**core, "summary_fingerprint": _canonical_hash(core)}
