from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.strategy_registry import (
    ELIGIBLE_FOR_USER_REVIEW,
    FORWARD_EVIDENCE_ACCUMULATING,
    FORWARD_SHADOW,
    IDEA,
    PORTFOLIO_AUDITED,
    REJECTED,
    RESEARCHED,
    USER_APPROVED,
    StrategyRegistryEntry,
    load_strategy_registry,
    transition_strategy,
)


def _entry(status: str = IDEA) -> StrategyRegistryEntry:
    return StrategyRegistryEntry(
        strategy_id="candidate",
        version="v1",
        role="candidate",
        score_source="score higher_is_better",
        status=status,
    )


def test_repository_registry_loads_without_real_money_authority() -> None:
    entries = load_strategy_registry(Path("config/strategy_registry_v1.json"))
    assert {entry.strategy_id for entry in entries} == {
        "momentum_20d_reversal_example",
        "transparent_combo_v1",
    }
    assert all(entry.status == FORWARD_EVIDENCE_ACCUMULATING for entry in entries)
    assert all(entry.user_approved is False for entry in entries)


def test_strategy_must_follow_evidence_lifecycle_without_skipping() -> None:
    entry = _entry()
    with pytest.raises(DataValidationError, match="illegal strategy status transition"):
        transition_strategy(entry, PORTFOLIO_AUDITED, evidence_ref="audit")

    entry = transition_strategy(entry, RESEARCHED, evidence_ref="research/run-1")
    entry = transition_strategy(entry, PORTFOLIO_AUDITED, evidence_ref="audit/run-1")
    entry = transition_strategy(entry, FORWARD_SHADOW, evidence_ref="shadow/config-v1")
    entry = transition_strategy(
        entry,
        FORWARD_EVIDENCE_ACCUMULATING,
        evidence_ref="shadow/prediction-v1",
    )
    entry = transition_strategy(
        entry,
        ELIGIBLE_FOR_USER_REVIEW,
        evidence_ref="shadow/evaluation-review-v1",
    )

    assert entry.status == ELIGIBLE_FOR_USER_REVIEW
    assert entry.user_approved is False
    assert len(entry.evidence_refs) == 5


def test_user_approval_cannot_be_inferred_or_attached_to_other_transition() -> None:
    entry = _entry(ELIGIBLE_FOR_USER_REVIEW)
    with pytest.raises(DataValidationError, match="requires explicit user approval"):
        transition_strategy(entry, USER_APPROVED)

    approved = transition_strategy(entry, USER_APPROVED, explicit_user_approval=True)
    assert approved.status == USER_APPROVED
    assert approved.user_approved is True
    assert approved.approval_source == "explicit_user_decision"

    with pytest.raises(DataValidationError, match="illegal strategy status transition"):
        transition_strategy(approved, REJECTED, rejection_reason="later metric change")


def test_rejection_is_terminal_and_requires_reason() -> None:
    with pytest.raises(DataValidationError, match="rejection_reason"):
        transition_strategy(_entry(), REJECTED)

    rejected = transition_strategy(_entry(), REJECTED, rejection_reason="hypothesis rejected")
    assert rejected.status == REJECTED
    with pytest.raises(DataValidationError, match="illegal strategy status transition"):
        transition_strategy(rejected, RESEARCHED, evidence_ref="new-run")


def test_evidence_transition_requires_new_nonempty_reference() -> None:
    with pytest.raises(DataValidationError, match="evidence_ref"):
        transition_strategy(_entry(), RESEARCHED)

    researched = transition_strategy(_entry(), RESEARCHED, evidence_ref="run-1")
    with pytest.raises(DataValidationError, match="already registered"):
        transition_strategy(researched, PORTFOLIO_AUDITED, evidence_ref="run-1")


def test_registry_rejects_duplicate_versions_and_forged_approval(tmp_path: Path) -> None:
    duplicate = {
        "schema": "quantlab_strategy_registry_v1",
        "strategies": [
            {
                "strategy_id": "same",
                "version": "v1",
                "role": "candidate",
                "score_source": "x",
                "status": "IDEA",
            },
            {
                "strategy_id": "same",
                "version": "v1",
                "role": "candidate",
                "score_source": "x",
                "status": "IDEA",
            },
        ],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(DataValidationError, match="duplicate strategy_id/version"):
        load_strategy_registry(path)

    forged = {
        "schema": "quantlab_strategy_registry_v1",
        "strategies": [
            {
                "strategy_id": "same",
                "version": "v1",
                "role": "candidate",
                "score_source": "x",
                "status": "USER_APPROVED",
                "user_approved": True,
                "approval_source": "historical_metric_threshold",
            }
        ],
    }
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(DataValidationError, match="explicit_user_decision"):
        load_strategy_registry(path)
