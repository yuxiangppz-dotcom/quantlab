"""PIT-gated research decision assembly for frozen S5-B observations."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.s5_base_completion import (
    S5BaseObservation,
    S5BaseResult,
    S5BaseState,
    evaluate_s5_base,
)
from quantlab.research.s5_membership_audit import (
    S5MembershipAuditRow,
    S5MembershipAuditStatus,
)


class S5BaseAdmissionState(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class S5BaseDecisionConfig:
    max_names: int = 20
    weight_per_name: float = 0.04

    def __post_init__(self) -> None:
        if type(self.max_names) is not int or self.max_names <= 0:
            raise ValueError("max_names must be a positive integer")
        if (
            not math.isfinite(self.weight_per_name)
            or not 0.0 < self.weight_per_name <= 1.0
        ):
            raise ValueError("weight_per_name must be finite and in (0, 1]")
        if self.max_names * self.weight_per_name > 1.0 + 1e-12:
            raise ValueError("max_names * weight_per_name cannot exceed 1")


@dataclass(frozen=True)
class S5BaseCandidate:
    instrument_id: str
    sector_id: str
    observation: S5BaseObservation
    membership: S5MembershipAuditRow | None
    research_eligible: bool | None

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("sector_id", self.sector_id)
        if self.observation.entity_id != self.instrument_id:
            raise ValueError("stock observation entity_id must equal instrument_id")
        if (
            self.research_eligible is not None
            and type(self.research_eligible) is not bool
        ):
            raise ValueError("research_eligible must be bool or None")
        if self.membership is not None:
            if self.membership.instrument_id != self.instrument_id:
                raise ValueError("membership instrument_id mismatch")
            if self.membership.as_of != self.observation.as_of:
                raise ValueError("membership as_of mismatch")


@dataclass(frozen=True)
class S5BaseCandidateResult:
    instrument_id: str
    sector_id: str
    state: S5BaseAdmissionState
    base_state: S5BaseState
    reasons: tuple[str, ...]
    rank: int | None = None
    selected: bool = False


@dataclass(frozen=True)
class S5BaseDecision:
    as_of: date
    sector_results: tuple[S5BaseResult, ...]
    candidate_results: tuple[S5BaseCandidateResult, ...]
    selected_ids: tuple[str, ...]
    target: TargetPortfolio
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False


def build_s5_base_decision(
    *,
    as_of: date,
    sectors: tuple[S5BaseObservation, ...],
    candidates: tuple[S5BaseCandidate, ...],
    config: S5BaseDecisionConfig | None = None,
) -> S5BaseDecision:
    """Build a deterministic S5-B Dynamic-N target with residual cash."""

    cfg = config or S5BaseDecisionConfig()
    sector_by_id: dict[str, S5BaseObservation] = {}
    for sector in sectors:
        if sector.as_of != as_of:
            raise ValueError("sector observation as_of must equal decision as_of")
        if sector.entity_id in sector_by_id:
            raise ValueError(f"duplicate sector_id: {sector.entity_id}")
        sector_by_id[sector.entity_id] = sector

    candidate_by_id: dict[str, S5BaseCandidate] = {}
    for candidate in candidates:
        if candidate.observation.as_of != as_of:
            raise ValueError("stock observation as_of must equal decision as_of")
        if candidate.instrument_id in candidate_by_id:
            raise ValueError(f"duplicate instrument_id: {candidate.instrument_id}")
        candidate_by_id[candidate.instrument_id] = candidate

    sector_results = tuple(
        evaluate_s5_base(sector_by_id[sector_id]) for sector_id in sorted(sector_by_id)
    )
    sector_result_by_id = {result.entity_id: result for result in sector_results}
    raw_results = {
        instrument_id: _evaluate_candidate(
            candidate, sector_result_by_id.get(candidate.sector_id)
        )
        for instrument_id, candidate in candidate_by_id.items()
    }
    eligible = [
        candidate_by_id[instrument_id]
        for instrument_id, result in raw_results.items()
        if result.state is S5BaseAdmissionState.ELIGIBLE
    ]
    eligible.sort(key=_rank_key)
    ranked_ids = [candidate.instrument_id for candidate in eligible]
    selected_ids = tuple(ranked_ids[: cfg.max_names])
    selected_set = set(selected_ids)
    rank_by_id = {
        instrument_id: rank for rank, instrument_id in enumerate(ranked_ids, 1)
    }
    candidate_results = tuple(
        replace(
            raw_results[instrument_id],
            rank=rank_by_id.get(instrument_id),
            selected=instrument_id in selected_set,
        )
        for instrument_id in sorted(raw_results)
    )
    positions = tuple(
        TargetWeight(instrument_id=instrument_id, target_weight=cfg.weight_per_name)
        for instrument_id in selected_ids
    )
    target = TargetPortfolio(
        as_of=as_of,
        positions=positions,
        cash_weight=1.0 - len(positions) * cfg.weight_per_name,
    )
    return S5BaseDecision(
        as_of, sector_results, candidate_results, selected_ids, target
    )


def _evaluate_candidate(
    candidate: S5BaseCandidate, sector: S5BaseResult | None
) -> S5BaseCandidateResult:
    stock = evaluate_s5_base(candidate.observation)
    if sector is None:
        return _candidate_result(
            candidate, stock, S5BaseAdmissionState.UNKNOWN, "sector_observation_missing"
        )
    if sector.state is S5BaseState.UNKNOWN:
        return _candidate_result(
            candidate, stock, S5BaseAdmissionState.UNKNOWN, "sector_state_unknown"
        )
    if sector.state not in {S5BaseState.BASE_READY, S5BaseState.BREAKOUT_CONFIRMED}:
        return _candidate_result(
            candidate,
            stock,
            S5BaseAdmissionState.INELIGIBLE,
            f"sector_{sector.state.value}",
        )
    if candidate.membership is None:
        return _candidate_result(
            candidate, stock, S5BaseAdmissionState.UNKNOWN, "membership_missing"
        )
    membership = candidate.membership
    if membership.status in {
        S5MembershipAuditStatus.MISSING,
        S5MembershipAuditStatus.UNVERIFIED,
        S5MembershipAuditStatus.CONFLICTING,
    }:
        return _candidate_result(
            candidate,
            stock,
            S5BaseAdmissionState.UNKNOWN,
            f"membership_{membership.status.value}",
        )
    if (
        membership.status is S5MembershipAuditStatus.MISMATCH
        or membership.resolved_sector_id != candidate.sector_id
        or (
            membership.expected_sector_id is not None
            and membership.expected_sector_id != candidate.sector_id
        )
    ):
        return _candidate_result(
            candidate,
            stock,
            S5BaseAdmissionState.INELIGIBLE,
            "membership_sector_mismatch",
        )
    if candidate.research_eligible is None:
        return _candidate_result(
            candidate,
            stock,
            S5BaseAdmissionState.UNKNOWN,
            "research_eligibility_unknown",
        )
    if not candidate.research_eligible:
        return _candidate_result(
            candidate, stock, S5BaseAdmissionState.INELIGIBLE, "research_ineligible"
        )
    if stock.state is S5BaseState.UNKNOWN:
        return _candidate_result(
            candidate, stock, S5BaseAdmissionState.UNKNOWN, "stock_state_unknown"
        )
    if stock.state not in {S5BaseState.BASE_READY, S5BaseState.BREAKOUT_CONFIRMED}:
        return _candidate_result(
            candidate,
            stock,
            S5BaseAdmissionState.INELIGIBLE,
            f"stock_{stock.state.value}",
        )
    return _candidate_result(candidate, stock, S5BaseAdmissionState.ELIGIBLE)


def _rank_key(candidate: S5BaseCandidate) -> tuple[float, float, float, float, str]:
    observation = candidate.observation
    result = evaluate_s5_base(observation)
    return (
        0.0 if result.state is S5BaseState.BREAKOUT_CONFIRMED else 1.0,
        -_known(observation.recovery_from_10d_low),
        -_known(observation.close_location_20),
        -_known(observation.volume_ratio_5_to_20),
        candidate.instrument_id,
    )


def _candidate_result(
    candidate: S5BaseCandidate,
    stock: S5BaseResult,
    state: S5BaseAdmissionState,
    reason: str | None = None,
) -> S5BaseCandidateResult:
    return S5BaseCandidateResult(
        candidate.instrument_id,
        candidate.sector_id,
        state,
        stock.state,
        () if reason is None else (reason,),
    )


def _known(value: float | None) -> float:
    if value is None:
        raise AssertionError(
            "eligible S5-B observation cannot contain missing ranking values"
        )
    return value


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
