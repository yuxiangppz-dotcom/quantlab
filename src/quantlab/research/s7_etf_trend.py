"""Provider-neutral S7 ETF trend and relative-strength research kernel."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.portfolio.models import TargetPortfolio, TargetWeight

_UNIVERSE_SCHEMA = "quantlab_s7_etf_universe_v1"


class S7EvidenceStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class S7State(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class S7Config:
    """Frozen single-hypothesis rule; alternate lookbacks are separate research."""

    lookback_sessions: int = 120
    absolute_return_floor: float = 0.0
    max_positions: int = 3
    weight_per_position: float = 0.30
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.lookback_sessions != 120:
            raise ValueError("S7 lookback_sessions is frozen at 120")
        if self.absolute_return_floor != 0.0:
            raise ValueError("S7 absolute_return_floor is frozen at zero")
        if self.max_positions != 3:
            raise ValueError("S7 max_positions is frozen at three")
        if self.weight_per_position != 0.30:
            raise ValueError("S7 weight_per_position is frozen at 0.30")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(
                {
                    "lookback_sessions": self.lookback_sessions,
                    "absolute_return_floor": self.absolute_return_floor,
                    "max_positions": self.max_positions,
                    "weight_per_position": self.weight_per_position,
                }
            ),
        )


@dataclass(frozen=True)
class S7UniverseMember:
    instrument_id: str
    exposure_id: str
    effective_from: date
    effective_to: date | None
    evidence_available_at: datetime
    membership_status: S7EvidenceStatus
    trading_history_status: S7EvidenceStatus
    corporate_action_status: S7EvidenceStatus
    evidence_fingerprint: str | None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("instrument_id", "exposure_id"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        _require_utc(self.evidence_available_at, "evidence_available_at")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot precede effective_from")
        if self.evidence_fingerprint is not None and (
            not self.evidence_fingerprint.strip()
            or self.evidence_fingerprint != self.evidence_fingerprint.strip()
        ):
            raise ValueError("evidence_fingerprint must be normalized when present")
        statuses = (
            self.membership_status,
            self.trading_history_status,
            self.corporate_action_status,
        )
        if all(status is S7EvidenceStatus.VERIFIED for status in statuses):
            if self.evidence_fingerprint is None:
                raise ValueError("fully verified members require evidence fingerprint")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_member_payload(self)),
        )


@dataclass(frozen=True)
class S7UniverseCatalog:
    schema: str
    members: tuple[S7UniverseMember, ...]
    fixed_membership: bool = True
    real_strategy_universe_admitted: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _UNIVERSE_SCHEMA:
            raise ValueError(f"schema must equal {_UNIVERSE_SCHEMA}")
        if not self.members:
            raise ValueError("S7 universe cannot be empty")
        expected_order = tuple(
            sorted(self.members, key=lambda row: row.instrument_id)
        )
        if self.members != expected_order:
            raise ValueError("S7 universe must use deterministic instrument order")
        instrument_ids = tuple(row.instrument_id for row in self.members)
        exposure_ids = tuple(row.exposure_id for row in self.members)
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("duplicate S7 universe instrument")
        if len(exposure_ids) != len(set(exposure_ids)):
            raise ValueError("duplicate S7 universe exposure")
        if (
            not self.fixed_membership
            or self.real_strategy_universe_admitted
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("universe catalog cannot acquire strategy authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(
                {
                    "schema": self.schema,
                    "member_fingerprints": [row.fingerprint for row in self.members],
                    "fixed_membership": self.fixed_membership,
                    "real_strategy_universe_admitted": (
                        self.real_strategy_universe_admitted
                    ),
                    "performance_claim": self.performance_claim,
                    "broker_order_authority": self.broker_order_authority,
                }
            ),
        )


@dataclass(frozen=True)
class S7TrendObservation:
    instrument_id: str
    as_of: datetime
    feature_available_at: datetime
    universe_fingerprint: str
    total_return_120: float | None
    history_complete: bool | None
    adjusted_price_evidence_verified: bool | None
    research_eligible: bool | None

    def __post_init__(self) -> None:
        for name in ("instrument_id", "universe_fingerprint"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        _require_utc(self.as_of, "as_of")
        _require_utc(self.feature_available_at, "feature_available_at")
        if self.total_return_120 is not None and (
            not math.isfinite(self.total_return_120)
            or self.total_return_120 < -1.0
        ):
            raise ValueError("total_return_120 must be finite and at least -1")
        for name in (
            "history_complete",
            "adjusted_price_evidence_verified",
            "research_eligible",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be bool or None")


@dataclass(frozen=True)
class S7InstrumentResult:
    instrument_id: str
    exposure_id: str
    state: S7State
    reasons: tuple[str, ...]
    total_return_120: float | None
    relative_strength_rank: int | None = None
    selected: bool = False

    def __post_init__(self) -> None:
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("result reasons must be sorted and unique")
        if self.state is S7State.ELIGIBLE and self.reasons:
            raise ValueError("eligible results cannot carry failure reasons")
        if (
            self.state is not S7State.ELIGIBLE
            and self.relative_strength_rank is not None
        ):
            raise ValueError("only eligible results may be ranked")
        if self.selected and self.relative_strength_rank is None:
            raise ValueError("selected results must be ranked")


@dataclass(frozen=True)
class S7Decision:
    as_of: datetime
    universe_fingerprint: str
    config_fingerprint: str
    results: tuple[S7InstrumentResult, ...]
    selected_ids: tuple[str, ...]
    target: TargetPortfolio
    dynamic_n: bool = True
    holding_period_lock: bool = False
    research_only: bool = True
    performance_claim: bool = False
    promotion_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_utc(self.as_of, "as_of")
        for name in ("universe_fingerprint", "config_fingerprint"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        expected_order = tuple(
            sorted(self.results, key=lambda row: row.instrument_id)
        )
        if self.results != expected_order:
            raise ValueError("results must use deterministic instrument order")
        eligible = tuple(row for row in self.results if row.state is S7State.ELIGIBLE)
        if any(row.relative_strength_rank is None for row in eligible):
            raise ValueError("eligible decision results must be ranked")
        ranks = tuple(
            sorted(
                row.relative_strength_rank
                for row in eligible
                if row.relative_strength_rank is not None
            )
        )
        if ranks != tuple(range(1, len(eligible) + 1)):
            raise ValueError("eligible result ranks must be contiguous")
        expected_selected_set = {
            row.instrument_id
            for row in eligible
            if row.relative_strength_rank is not None
            and row.relative_strength_rank <= 3
        }
        if any(
            row.selected != (row.instrument_id in expected_selected_set)
            for row in self.results
        ):
            raise ValueError("selected flags do not match frozen top-three rule")
        expected_selected = tuple(
            row.instrument_id
            for row in sorted(
                (row for row in eligible if row.selected),
                key=lambda row: row.relative_strength_rank or 0,
            )
        )
        if self.selected_ids != expected_selected:
            raise ValueError("selected_ids do not match ranked selected results")
        if self.target.as_of != self.as_of.date():
            raise ValueError("target date must match decision as_of date")
        target_ids = tuple(row.instrument_id for row in self.target.positions)
        if target_ids != self.selected_ids:
            raise ValueError("target positions do not match selected_ids")
        if any(row.target_weight != 0.30 for row in self.target.positions):
            raise ValueError("target weights must match frozen 0.30 sizing")
        expected_cash = 1.0 - 0.30 * len(self.selected_ids)
        if not math.isclose(self.target.cash_weight, expected_cash, abs_tol=1e-12):
            raise ValueError("target cash does not match frozen Dynamic N sizing")
        if (
            not self.dynamic_n
            or self.holding_period_lock
            or not self.research_only
            or self.performance_claim
            or self.promotion_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "S7 decision cannot acquire promotion or execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_decision_payload(self)),
        )


def build_s7_universe(
    members: Iterable[S7UniverseMember],
) -> S7UniverseCatalog:
    """Freeze an explicit candidate set without admitting real strategy use."""

    return S7UniverseCatalog(
        schema=_UNIVERSE_SCHEMA,
        members=tuple(sorted(members, key=lambda row: row.instrument_id)),
    )


def build_s7_decision(
    *,
    as_of: datetime,
    universe: S7UniverseCatalog,
    observations: Iterable[S7TrendObservation],
    config: S7Config | None = None,
) -> S7Decision:
    """Apply the frozen absolute-trend gate and relative-strength ranking."""

    normalized_as_of = _to_utc(as_of, "as_of")
    cfg = config or S7Config()
    observation_by_id: dict[str, S7TrendObservation] = {}
    for observation in observations:
        if observation.as_of != normalized_as_of:
            raise ValueError("observation as_of must match decision as_of")
        if observation.universe_fingerprint != universe.fingerprint:
            raise ValueError("observation is not bound to the exact S7 universe")
        if observation.instrument_id in observation_by_id:
            raise ValueError(f"duplicate S7 observation: {observation.instrument_id}")
        observation_by_id[observation.instrument_id] = observation
    unexpected = set(observation_by_id) - {
        member.instrument_id for member in universe.members
    }
    if unexpected:
        raise ValueError("S7 observation contains instrument outside frozen universe")

    raw_results = {
        member.instrument_id: _evaluate_member(
            member=member,
            observation=observation_by_id.get(member.instrument_id),
            as_of=normalized_as_of,
            config=cfg,
        )
        for member in universe.members
    }
    eligible = sorted(
        (row for row in raw_results.values() if row.state is S7State.ELIGIBLE),
        key=lambda row: (-_known_return(row), row.instrument_id),
    )
    ranked = {
        row.instrument_id: rank
        for rank, row in enumerate(eligible, start=1)
    }
    selected_ids = tuple(
        row.instrument_id for row in eligible[: cfg.max_positions]
    )
    selected_set = set(selected_ids)
    results = tuple(
        replace(
            raw_results[instrument_id],
            relative_strength_rank=ranked.get(instrument_id),
            selected=instrument_id in selected_set,
        )
        for instrument_id in sorted(raw_results)
    )
    positions = tuple(
        TargetWeight(
            instrument_id=instrument_id,
            target_weight=cfg.weight_per_position,
        )
        for instrument_id in selected_ids
    )
    target = TargetPortfolio(
        as_of=normalized_as_of.date(),
        positions=positions,
        cash_weight=1.0 - len(positions) * cfg.weight_per_position,
    )
    return S7Decision(
        as_of=normalized_as_of,
        universe_fingerprint=universe.fingerprint,
        config_fingerprint=cfg.fingerprint,
        results=results,
        selected_ids=selected_ids,
        target=target,
    )


def _evaluate_member(
    *,
    member: S7UniverseMember,
    observation: S7TrendObservation | None,
    as_of: datetime,
    config: S7Config,
) -> S7InstrumentResult:
    unknown: list[str] = []
    if member.evidence_available_at > as_of:
        unknown.append("membership_evidence_not_yet_available")
    for name in (
        "membership_status",
        "trading_history_status",
        "corporate_action_status",
    ):
        status = getattr(member, name)
        if status is not S7EvidenceStatus.VERIFIED:
            unknown.append(f"{name}_{status.value}")
    if unknown:
        return _result(member, S7State.UNKNOWN, unknown, observation)

    decision_date = as_of.date()
    if decision_date < member.effective_from or (
        member.effective_to is not None and decision_date > member.effective_to
    ):
        return _result(
            member,
            S7State.INELIGIBLE,
            ["outside_verified_effective_interval"],
            observation,
        )
    if observation is None:
        return _result(member, S7State.UNKNOWN, ["observation_missing"], None)

    if observation.feature_available_at > as_of:
        unknown.append("feature_not_yet_available")
    if observation.total_return_120 is None:
        unknown.append("total_return_120_missing")
    for name in ("history_complete", "adjusted_price_evidence_verified"):
        value = getattr(observation, name)
        if value is not True:
            suffix = "unknown" if value is None else "not_verified"
            unknown.append(f"{name}_{suffix}")
    if observation.research_eligible is None:
        unknown.append("research_eligibility_unknown")
    if unknown:
        return _result(member, S7State.UNKNOWN, unknown, observation)
    if observation.research_eligible is False:
        return _result(
            member,
            S7State.INELIGIBLE,
            ["research_ineligible"],
            observation,
        )

    assert observation.total_return_120 is not None
    if observation.total_return_120 <= config.absolute_return_floor:
        return _result(
            member,
            S7State.INELIGIBLE,
            ["absolute_trend_not_positive"],
            observation,
        )
    return _result(member, S7State.ELIGIBLE, [], observation)


def _result(
    member: S7UniverseMember,
    state: S7State,
    reasons: list[str],
    observation: S7TrendObservation | None,
) -> S7InstrumentResult:
    return S7InstrumentResult(
        instrument_id=member.instrument_id,
        exposure_id=member.exposure_id,
        state=state,
        reasons=tuple(sorted(set(reasons))),
        total_return_120=(
            observation.total_return_120 if observation is not None else None
        ),
    )


def _known_return(result: S7InstrumentResult) -> float:
    if result.total_return_120 is None:
        raise AssertionError("eligible S7 result cannot have missing return")
    return result.total_return_120


def _to_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be normalized to UTC")


def _member_payload(member: S7UniverseMember) -> dict[str, object]:
    return {
        "instrument_id": member.instrument_id,
        "exposure_id": member.exposure_id,
        "effective_from": member.effective_from.isoformat(),
        "effective_to": (
            member.effective_to.isoformat() if member.effective_to is not None else None
        ),
        "evidence_available_at": member.evidence_available_at.isoformat(),
        "membership_status": member.membership_status.value,
        "trading_history_status": member.trading_history_status.value,
        "corporate_action_status": member.corporate_action_status.value,
        "evidence_fingerprint": member.evidence_fingerprint,
    }


def _decision_payload(decision: S7Decision) -> dict[str, object]:
    return {
        "as_of": decision.as_of.isoformat(),
        "universe_fingerprint": decision.universe_fingerprint,
        "config_fingerprint": decision.config_fingerprint,
        "results": [
            {
                "instrument_id": row.instrument_id,
                "exposure_id": row.exposure_id,
                "state": row.state.value,
                "reasons": list(row.reasons),
                "total_return_120": row.total_return_120,
                "relative_strength_rank": row.relative_strength_rank,
                "selected": row.selected,
            }
            for row in decision.results
        ],
        "selected_ids": list(decision.selected_ids),
        "target_positions": [
            {
                "instrument_id": row.instrument_id,
                "target_weight": row.target_weight,
            }
            for row in decision.target.positions
        ],
        "cash_weight": decision.target.cash_weight,
        "dynamic_n": decision.dynamic_n,
        "holding_period_lock": decision.holding_period_lock,
        "research_only": decision.research_only,
        "performance_claim": decision.performance_claim,
        "promotion_authority": decision.promotion_authority,
        "account_mutation_authority": decision.account_mutation_authority,
        "broker_order_authority": decision.broker_order_authority,
    }
