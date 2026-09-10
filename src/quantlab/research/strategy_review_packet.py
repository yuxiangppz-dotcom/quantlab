"""Deterministic, claim-neutral packets for human strategy review.

The packet is a read-only projection over existing governance and evidence
layers. It can make evidence easier to inspect, but it cannot transition a
strategy, infer investment success, authorize an order, or create a fill.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import EvidenceCatalog
from quantlab.research.forward_shadow_analytics import ShadowDiagnosticSummary
from quantlab.research.strategy_readiness import build_registered_strategy_readiness
from quantlab.research.strategy_registry import load_strategy_registry


@dataclass(frozen=True)
class ReviewEvidenceReference:
    reference: str
    reference_kind: str
    matched_evidence_id: str | None
    mutable_catalog_path: bool


@dataclass(frozen=True)
class StrategyReviewPacket:
    strategy_id: str
    version: str
    role: str
    registry_status: str
    forward_model_id: str | None
    evidence_references: tuple[ReviewEvidenceReference, ...]
    catalog_evidence_id_match_count: int
    catalog_path_match_count: int
    forward_prediction_count: int
    forward_complete_evaluation_count: int
    forward_incomplete_evaluation_count: int
    forward_pending_prediction_count: int
    blockers: tuple[str, ...]
    ready_for_user_review: bool
    user_approved: bool
    strategy_approval_authority: bool
    broker_order_authority: bool
    packet_fingerprint: str


@dataclass(frozen=True)
class StrategyReviewBundle:
    packets: tuple[StrategyReviewPacket, ...]
    strategy_forward_binding_fingerprint: str
    evidence_catalog_fingerprint: str | None
    bundle_fingerprint: str
    claim: str


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_views(
    references: tuple[str, ...],
    catalog: EvidenceCatalog | None,
) -> tuple[ReviewEvidenceReference, ...]:
    if catalog is None:
        return tuple(
            ReviewEvidenceReference(
                reference=reference,
                reference_kind="static_or_unresolved_reference",
                matched_evidence_id=None,
                mutable_catalog_path=False,
            )
            for reference in sorted(references)
        )

    by_id = {entry.evidence_id: entry for entry in catalog.entries}
    by_path: dict[str, list] = {}
    for entry in catalog.entries:
        by_path.setdefault(entry.relative_path, []).append(entry)

    views = []
    for reference in sorted(references):
        if reference in by_id:
            views.append(
                ReviewEvidenceReference(
                    reference=reference,
                    reference_kind="content_bound_evidence_id",
                    matched_evidence_id=reference,
                    mutable_catalog_path=False,
                )
            )
            continue
        path_matches = by_path.get(reference, [])
        if len(path_matches) > 1:
            raise DataValidationError(
                f"catalog path reference is ambiguous: {reference}"
            )
        if path_matches:
            views.append(
                ReviewEvidenceReference(
                    reference=reference,
                    reference_kind="catalog_path_reference",
                    matched_evidence_id=path_matches[0].evidence_id,
                    mutable_catalog_path=True,
                )
            )
            continue
        views.append(
            ReviewEvidenceReference(
                reference=reference,
                reference_kind="static_or_unresolved_reference",
                matched_evidence_id=None,
                mutable_catalog_path=False,
            )
        )
    return tuple(views)


def build_strategy_review_bundle(
    registry_path: Path,
    forward_config_path: Path,
    *,
    shadow_summaries: tuple[ShadowDiagnosticSummary, ...] = (),
    evidence_catalog: EvidenceCatalog | None = None,
) -> StrategyReviewBundle:
    """Build one deterministic review packet per registered strategy."""
    entries = load_strategy_registry(registry_path)
    readiness = build_registered_strategy_readiness(
        registry_path,
        forward_config_path,
        shadow_summaries=shadow_summaries,
        evidence_catalog=evidence_catalog,
    )
    if len(entries) != len(readiness):
        raise DataValidationError("strategy readiness count differs from registry")

    entry_by_key = {entry.key: entry for entry in entries}
    if len(entry_by_key) != len(entries):
        raise DataValidationError("duplicate strategy identity in review packet")

    binding_fingerprints = {
        item.strategy_forward_binding_fingerprint for item in readiness
    }
    if None in binding_fingerprints or len(binding_fingerprints) != 1:
        raise DataValidationError(
            "strategy readiness reports do not share one validated binding fingerprint"
        )
    binding_fingerprint = next(iter(binding_fingerprints))
    if not isinstance(binding_fingerprint, str) or len(binding_fingerprint) != 64:
        raise DataValidationError("invalid strategy forward binding fingerprint")

    packets = []
    for report in readiness:
        key = (report.strategy_id, report.version)
        entry = entry_by_key.get(key)
        if entry is None:
            raise DataValidationError("readiness report has no registry strategy")
        evidence_views = _evidence_views(entry.evidence_refs, evidence_catalog)
        core = {
            "strategy_id": report.strategy_id,
            "version": report.version,
            "role": report.role,
            "registry_status": report.registry_status,
            "forward_model_id": report.forward_model_id,
            "evidence_references": [asdict(item) for item in evidence_views],
            "catalog_evidence_id_match_count": report.catalog_evidence_id_match_count,
            "catalog_path_match_count": report.catalog_path_match_count,
            "forward_prediction_count": report.forward_prediction_count,
            "forward_complete_evaluation_count": (
                report.forward_complete_evaluation_count
            ),
            "forward_incomplete_evaluation_count": (
                report.forward_incomplete_evaluation_count
            ),
            "forward_pending_prediction_count": report.forward_pending_prediction_count,
            "blockers": report.blockers,
            "ready_for_user_review": report.ready_for_user_review,
            "user_approved": report.user_approved,
            "strategy_approval_authority": report.strategy_approval_authority,
            "broker_order_authority": False,
            "strategy_forward_binding_fingerprint": binding_fingerprint,
            "evidence_catalog_fingerprint": (
                evidence_catalog.catalog_fingerprint
                if evidence_catalog is not None
                else None
            ),
        }
        packets.append(
            StrategyReviewPacket(
                strategy_id=report.strategy_id,
                version=report.version,
                role=report.role,
                registry_status=report.registry_status,
                forward_model_id=report.forward_model_id,
                evidence_references=evidence_views,
                catalog_evidence_id_match_count=(
                    report.catalog_evidence_id_match_count
                ),
                catalog_path_match_count=report.catalog_path_match_count,
                forward_prediction_count=report.forward_prediction_count,
                forward_complete_evaluation_count=(
                    report.forward_complete_evaluation_count
                ),
                forward_incomplete_evaluation_count=(
                    report.forward_incomplete_evaluation_count
                ),
                forward_pending_prediction_count=(
                    report.forward_pending_prediction_count
                ),
                blockers=report.blockers,
                ready_for_user_review=report.ready_for_user_review,
                user_approved=report.user_approved,
                strategy_approval_authority=report.strategy_approval_authority,
                broker_order_authority=False,
                packet_fingerprint=_canonical_hash(core),
            )
        )

    ordered = tuple(sorted(packets, key=lambda item: (item.strategy_id, item.version)))
    catalog_fingerprint = (
        evidence_catalog.catalog_fingerprint if evidence_catalog is not None else None
    )
    bundle_core = {
        "schema": "quantlab_strategy_review_bundle_v1",
        "packet_fingerprints": [item.packet_fingerprint for item in ordered],
        "strategy_forward_binding_fingerprint": binding_fingerprint,
        "evidence_catalog_fingerprint": catalog_fingerprint,
        "claim": "human_review_projection_only_not_performance_or_execution_authority",
    }
    if any(item.broker_order_authority for item in ordered):
        raise DataValidationError("strategy review packet cannot carry broker authority")
    return StrategyReviewBundle(
        packets=ordered,
        strategy_forward_binding_fingerprint=binding_fingerprint,
        evidence_catalog_fingerprint=catalog_fingerprint,
        bundle_fingerprint=_canonical_hash(bundle_core),
        claim=bundle_core["claim"],
    )
