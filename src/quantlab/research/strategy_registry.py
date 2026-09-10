"""Explicit lifecycle state for research strategies and forward candidates.

The registry is intentionally evidence-oriented. Historical metrics may produce
new evidence references, but no metric threshold can silently approve a
strategy for real-money use. USER_APPROVED always requires an explicit user
decision supplied to the transition API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from quantlab.data.models import DataValidationError

IDEA = "IDEA"
RESEARCHED = "RESEARCHED"
PORTFOLIO_AUDITED = "PORTFOLIO_AUDITED"
FORWARD_SHADOW = "FORWARD_SHADOW"
FORWARD_EVIDENCE_ACCUMULATING = "FORWARD_EVIDENCE_ACCUMULATING"
ELIGIBLE_FOR_USER_REVIEW = "ELIGIBLE_FOR_USER_REVIEW"
USER_APPROVED = "USER_APPROVED"
REJECTED = "REJECTED"

STATUSES = (
    IDEA,
    RESEARCHED,
    PORTFOLIO_AUDITED,
    FORWARD_SHADOW,
    FORWARD_EVIDENCE_ACCUMULATING,
    ELIGIBLE_FOR_USER_REVIEW,
    USER_APPROVED,
    REJECTED,
)

_ALLOWED_TRANSITIONS = {
    IDEA: {RESEARCHED, REJECTED},
    RESEARCHED: {PORTFOLIO_AUDITED, REJECTED},
    PORTFOLIO_AUDITED: {FORWARD_SHADOW, REJECTED},
    FORWARD_SHADOW: {FORWARD_EVIDENCE_ACCUMULATING, REJECTED},
    FORWARD_EVIDENCE_ACCUMULATING: {ELIGIBLE_FOR_USER_REVIEW, REJECTED},
    ELIGIBLE_FOR_USER_REVIEW: {USER_APPROVED, REJECTED},
    USER_APPROVED: set(),
    REJECTED: set(),
}

_EVIDENCE_REQUIRED_TARGETS = {
    RESEARCHED,
    PORTFOLIO_AUDITED,
    FORWARD_SHADOW,
    FORWARD_EVIDENCE_ACCUMULATING,
    ELIGIBLE_FOR_USER_REVIEW,
}


@dataclass(frozen=True)
class StrategyRegistryEntry:
    strategy_id: str
    version: str
    role: str
    score_source: str
    status: str
    evidence_refs: tuple[str, ...] = ()
    user_approved: bool = False
    approval_source: str | None = None
    rejection_reason: str | None = None
    notes: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.strategy_id, self.version


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"strategy registry {field} must be a non-empty string")
    return value.strip()


def _validate_entry(entry: StrategyRegistryEntry) -> StrategyRegistryEntry:
    _nonempty_string(entry.strategy_id, "strategy_id")
    _nonempty_string(entry.version, "version")
    if entry.role not in {"baseline", "candidate"}:
        raise DataValidationError("strategy registry role must be baseline or candidate")
    _nonempty_string(entry.score_source, "score_source")
    if entry.status not in STATUSES:
        raise DataValidationError(f"unknown strategy registry status: {entry.status!r}")
    if len(entry.evidence_refs) != len(set(entry.evidence_refs)):
        raise DataValidationError("strategy registry evidence_refs must be unique")
    if any(not isinstance(item, str) or not item.strip() for item in entry.evidence_refs):
        raise DataValidationError("strategy registry evidence_refs must be non-empty strings")

    if entry.status == USER_APPROVED:
        if not entry.user_approved:
            raise DataValidationError("USER_APPROVED requires user_approved=true")
        if entry.approval_source != "explicit_user_decision":
            raise DataValidationError(
                "USER_APPROVED requires approval_source=explicit_user_decision"
            )
    elif entry.user_approved or entry.approval_source is not None:
        raise DataValidationError("non-approved strategy cannot carry user approval authority")

    if entry.status == REJECTED:
        _nonempty_string(entry.rejection_reason, "rejection_reason")
    elif entry.rejection_reason is not None:
        raise DataValidationError("non-rejected strategy cannot carry rejection_reason")
    return entry


def _entry_from_dict(item: dict) -> StrategyRegistryEntry:
    required = {"strategy_id", "version", "role", "score_source", "status"}
    missing = required - set(item)
    if missing:
        raise DataValidationError(f"strategy registry entry missing fields: {sorted(missing)}")
    refs = item.get("evidence_refs", [])
    if not isinstance(refs, list):
        raise DataValidationError("strategy registry evidence_refs must be a list")
    entry = StrategyRegistryEntry(
        strategy_id=item["strategy_id"],
        version=item["version"],
        role=item["role"],
        score_source=item["score_source"],
        status=item["status"],
        evidence_refs=tuple(refs),
        user_approved=item.get("user_approved", False),
        approval_source=item.get("approval_source"),
        rejection_reason=item.get("rejection_reason"),
        notes=item.get("notes"),
    )
    return _validate_entry(entry)


def load_strategy_registry(path: Path) -> tuple[StrategyRegistryEntry, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "quantlab_strategy_registry_v1":
        raise DataValidationError("unsupported strategy registry schema")
    items = payload.get("strategies")
    if not isinstance(items, list) or not items:
        raise DataValidationError("strategy registry must contain at least one strategy")
    entries = tuple(_entry_from_dict(item) for item in items)
    keys = [entry.key for entry in entries]
    if len(keys) != len(set(keys)):
        raise DataValidationError("duplicate strategy_id/version in strategy registry")
    return entries


def transition_strategy(
    entry: StrategyRegistryEntry,
    target_status: str,
    *,
    evidence_ref: str | None = None,
    explicit_user_approval: bool = False,
    rejection_reason: str | None = None,
) -> StrategyRegistryEntry:
    """Advance one strategy through the preregistered evidence lifecycle.

    This function never inspects performance metrics. A caller may attach an
    immutable evidence reference after a research/audit/shadow step, but only an
    explicit user approval flag can create USER_APPROVED authority.
    """
    _validate_entry(entry)
    if target_status not in STATUSES:
        raise DataValidationError(f"unknown target strategy status: {target_status!r}")
    if target_status not in _ALLOWED_TRANSITIONS[entry.status]:
        raise DataValidationError(
            f"illegal strategy status transition: {entry.status} -> {target_status}"
        )

    refs = list(entry.evidence_refs)
    if target_status in _EVIDENCE_REQUIRED_TARGETS:
        ref = _nonempty_string(evidence_ref, "evidence_ref")
        if ref in refs:
            raise DataValidationError("transition evidence_ref is already registered")
        refs.append(ref)
    elif evidence_ref is not None:
        raise DataValidationError("this strategy transition does not accept evidence_ref")

    if target_status == USER_APPROVED:
        if not explicit_user_approval:
            raise DataValidationError("USER_APPROVED requires explicit user approval")
        updated = replace(
            entry,
            status=USER_APPROVED,
            user_approved=True,
            approval_source="explicit_user_decision",
            rejection_reason=None,
        )
    elif target_status == REJECTED:
        reason = _nonempty_string(rejection_reason, "rejection_reason")
        if explicit_user_approval:
            raise DataValidationError("rejection cannot carry explicit user approval")
        updated = replace(
            entry,
            status=REJECTED,
            evidence_refs=tuple(refs),
            rejection_reason=reason,
            user_approved=False,
            approval_source=None,
        )
    else:
        if explicit_user_approval:
            raise DataValidationError("user approval is only valid for USER_APPROVED")
        if rejection_reason is not None:
            raise DataValidationError("rejection_reason is only valid for REJECTED")
        updated = replace(
            entry,
            status=target_status,
            evidence_refs=tuple(refs),
            user_approved=False,
            approval_source=None,
            rejection_reason=None,
        )
    return _validate_entry(updated)
