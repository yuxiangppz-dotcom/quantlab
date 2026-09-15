"""Outcome-free readiness gate for the frozen S5-B diagnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_diagnostic_protocol import (
    S5BaseDiagnosticProtocol,
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_membership_audit import (
    S5MembershipAudit,
    S5MembershipAuditStatus,
)

_SCHEMA = "quantlab_s5b_diagnostic_readiness_v1"
_HISTORY_SESSIONS = 120


class S5BaseDiagnosticReadinessVerdict(StrEnum):
    READY = "ready_for_single_frozen_run"
    BLOCKED = "blocked_before_outcomes"


@dataclass(frozen=True, order=True)
class S5BaseDiagnosticPopulationKey:
    instrument_id: str
    as_of: date

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)


@dataclass(frozen=True)
class S5BaseEligibilityEvidence:
    instrument_id: str
    as_of: date
    eligible: bool | None
    source_id: str
    evidence_fingerprint: str

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        _require_text("source_id", self.source_id)
        _require_text("evidence_fingerprint", self.evidence_fingerprint)
        if self.eligible is not None and type(self.eligible) is not bool:
            raise ValueError("eligible must be bool or None")

    @property
    def key(self) -> S5BaseDiagnosticPopulationKey:
        return S5BaseDiagnosticPopulationKey(self.instrument_id, self.as_of)


@dataclass(frozen=True, order=True)
class S5BaseDiagnosticReadinessBlocker:
    code: str
    reason: str
    instrument_id: str | None = None
    as_of: date | None = None
    horizon: int | None = None

    def __post_init__(self) -> None:
        _require_text("code", self.code)
        _require_text("reason", self.reason)
        if self.instrument_id is not None:
            _require_text("instrument_id", self.instrument_id)
        if self.horizon is not None and (
            type(self.horizon) is not int or self.horizon <= 0
        ):
            raise ValueError("horizon must be a positive integer when present")


@dataclass(frozen=True)
class S5BaseDiagnosticReadiness:
    schema: str
    verdict: S5BaseDiagnosticReadinessVerdict
    intended_start: date
    intended_end: date
    admissible_start: date | None
    admissible_end: date | None
    frozen_period_end: date
    population_size: int
    protocol_fingerprint: str
    membership_audit_fingerprint: str
    eligibility_evidence_fingerprint: str
    benchmark_coverage_fingerprint: str
    sessions_fingerprint: str
    blockers: tuple[S5BaseDiagnosticReadinessBlocker, ...]
    research_only: bool = True
    outcome_data_loaded: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.population_size <= 0:
            raise ValueError("population_size must be positive")
        for name in (
            "protocol_fingerprint",
            "membership_audit_fingerprint",
            "eligibility_evidence_fingerprint",
            "benchmark_coverage_fingerprint",
            "sessions_fingerprint",
        ):
            _require_text(name, getattr(self, name))
        if self.intended_start > self.intended_end:
            raise ValueError("intended_start cannot be after intended_end")
        if self.intended_end > self.frozen_period_end:
            raise ValueError("intended_end cannot exceed frozen_period_end")
        if self.verdict is S5BaseDiagnosticReadinessVerdict.READY:
            if self.blockers:
                raise ValueError("ready verdict cannot contain blockers")
            if (
                self.admissible_start != self.intended_start
                or self.admissible_end != self.intended_end
            ):
                raise ValueError("ready verdict must admit the exact intended period")
        else:
            if not self.blockers:
                raise ValueError("blocked verdict requires blockers")
            if self.admissible_start is not None or self.admissible_end is not None:
                raise ValueError("blocked verdict cannot expose a shrunken period")
        if (
            not self.research_only
            or self.outcome_data_loaded
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("readiness result cannot claim outcomes or execution")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_result_payload(self)),
        )


def evaluate_s5_base_diagnostic_readiness(
    *,
    protocol: S5BaseDiagnosticProtocol,
    population: tuple[S5BaseDiagnosticPopulationKey, ...],
    membership_audit: S5MembershipAudit,
    eligibility_evidence: tuple[S5BaseEligibilityEvidence, ...],
    benchmark_coverage_dates: tuple[date, ...],
    market_sessions: tuple[date, ...],
    frozen_period_end: date,
) -> S5BaseDiagnosticReadiness:
    """Decide whether the exact intended S5-B population may load outcomes."""

    frozen = frozen_s5_base_diagnostic_protocol()
    if protocol.fingerprint != frozen.fingerprint:
        raise ValueError("protocol must equal the frozen S5-B diagnostic protocol")
    if not population:
        raise ValueError("population cannot be empty")
    _validate_strict_sessions(market_sessions)
    if len(benchmark_coverage_dates) != len(set(benchmark_coverage_dates)):
        raise ValueError("benchmark_coverage_dates cannot contain duplicates")

    population_by_key = _unique_population(population)
    intended_keys = set(population_by_key)
    intended_dates = sorted({key.as_of for key in intended_keys})
    consumed_sessions = tuple(
        session for session in market_sessions if session <= frozen_period_end
    )
    if not consumed_sessions:
        raise ValueError("no market session exists on or before frozen_period_end")
    session_index = {session: index for index, session in enumerate(consumed_sessions)}
    for as_of in intended_dates:
        if as_of not in session_index:
            raise ValueError(
                "every population as_of must be a market session on or before "
                "frozen_period_end"
            )

    membership_by_key = _unique_membership_rows(membership_audit)
    eligibility_by_key = _unique_eligibility(eligibility_evidence)
    blockers: list[S5BaseDiagnosticReadinessBlocker] = []

    _reconcile_membership(
        intended_keys=intended_keys,
        membership_by_key=membership_by_key,
        blocker_codes=set(protocol.blocker_codes),
        blockers=blockers,
    )
    _reconcile_eligibility(
        intended_keys=intended_keys,
        eligibility_by_key=eligibility_by_key,
        blocker_codes=set(protocol.blocker_codes),
        blockers=blockers,
    )

    required_benchmark_dates: set[date] = set(intended_dates)
    for key in sorted(intended_keys):
        index = session_index[key.as_of]
        if index + 1 < _HISTORY_SESSIONS:
            blockers.append(
                _blocker(
                    protocol,
                    "blocked_eligibility_evidence",
                    "insufficient_120_session_feature_history",
                    key,
                )
            )
        for horizon in protocol.signal_horizons:
            endpoint_index = index + horizon
            if endpoint_index >= len(consumed_sessions):
                blockers.append(
                    _blocker(
                        protocol,
                        "blocked_forward_window_boundary",
                        "forward_endpoint_crosses_frozen_end",
                        key,
                        horizon,
                    )
                )
            else:
                required_benchmark_dates.add(consumed_sessions[endpoint_index])

    consumed_benchmark = tuple(
        sorted(
            coverage
            for coverage in set(benchmark_coverage_dates)
            if coverage <= frozen_period_end
        )
    )
    benchmark_set = set(consumed_benchmark)
    for missing_date in sorted(required_benchmark_dates - benchmark_set):
        blockers.append(
            S5BaseDiagnosticReadinessBlocker(
                code=_known_blocker_code(
                    protocol, "blocked_benchmark_evidence"
                ),
                reason="benchmark_date_missing",
                as_of=missing_date,
            )
        )

    ordered_blockers = tuple(sorted(set(blockers), key=_blocker_sort_key))
    verdict = (
        S5BaseDiagnosticReadinessVerdict.READY
        if not ordered_blockers
        else S5BaseDiagnosticReadinessVerdict.BLOCKED
    )
    intended_start, intended_end = intended_dates[0], intended_dates[-1]
    return S5BaseDiagnosticReadiness(
        schema=_SCHEMA,
        verdict=verdict,
        intended_start=intended_start,
        intended_end=intended_end,
        admissible_start=(
            intended_start
            if verdict is S5BaseDiagnosticReadinessVerdict.READY
            else None
        ),
        admissible_end=(
            intended_end
            if verdict is S5BaseDiagnosticReadinessVerdict.READY
            else None
        ),
        frozen_period_end=frozen_period_end,
        population_size=len(intended_keys),
        protocol_fingerprint=protocol.fingerprint,
        membership_audit_fingerprint=_nonempty_fingerprint(
            "membership_audit.fingerprint", membership_audit.fingerprint
        ),
        eligibility_evidence_fingerprint=canonical_payload_fingerprint(
            [
                {
                    "instrument_id": item.instrument_id,
                    "as_of": item.as_of.isoformat(),
                    "eligible": item.eligible,
                    "source_id": item.source_id,
                    "evidence_fingerprint": item.evidence_fingerprint,
                }
                for item in sorted(
                    eligibility_evidence,
                    key=lambda value: (
                        value.as_of,
                        value.instrument_id,
                        value.source_id,
                    ),
                )
            ]
        ),
        benchmark_coverage_fingerprint=canonical_payload_fingerprint(
            [value.isoformat() for value in consumed_benchmark]
        ),
        sessions_fingerprint=canonical_payload_fingerprint(
            [value.isoformat() for value in consumed_sessions]
        ),
        blockers=ordered_blockers,
    )


def _unique_population(
    population: tuple[S5BaseDiagnosticPopulationKey, ...],
) -> dict[S5BaseDiagnosticPopulationKey, S5BaseDiagnosticPopulationKey]:
    result: dict[S5BaseDiagnosticPopulationKey, S5BaseDiagnosticPopulationKey] = {}
    for key in population:
        if key in result:
            raise ValueError(
                f"duplicate population key: {key.instrument_id} {key.as_of}"
            )
        result[key] = key
    return result


def _unique_membership_rows(
    audit: S5MembershipAudit,
) -> dict[S5BaseDiagnosticPopulationKey, object]:
    result: dict[S5BaseDiagnosticPopulationKey, object] = {}
    for row in audit.rows:
        key = S5BaseDiagnosticPopulationKey(row.instrument_id, row.as_of)
        if key in result:
            raise ValueError(
                f"duplicate membership row: {key.instrument_id} {key.as_of}"
            )
        result[key] = row
    return result


def _unique_eligibility(
    evidence: tuple[S5BaseEligibilityEvidence, ...],
) -> dict[S5BaseDiagnosticPopulationKey, S5BaseEligibilityEvidence]:
    result: dict[S5BaseDiagnosticPopulationKey, S5BaseEligibilityEvidence] = {}
    for item in evidence:
        if item.key in result:
            raise ValueError(
                f"duplicate eligibility evidence: {item.instrument_id} {item.as_of}"
            )
        result[item.key] = item
    return result


def _reconcile_membership(
    *,
    intended_keys: set[S5BaseDiagnosticPopulationKey],
    membership_by_key: dict[S5BaseDiagnosticPopulationKey, object],
    blocker_codes: set[str],
    blockers: list[S5BaseDiagnosticReadinessBlocker],
) -> None:
    code = _known_code(blocker_codes, "blocked_membership_evidence")
    membership_keys = set(membership_by_key)
    for key in sorted(intended_keys - membership_keys):
        blockers.append(
            S5BaseDiagnosticReadinessBlocker(
                code, "membership_population_missing", key.instrument_id, key.as_of
            )
        )
    for key in sorted(membership_keys - intended_keys):
        blockers.append(
            S5BaseDiagnosticReadinessBlocker(
                code, "membership_population_unexpected", key.instrument_id, key.as_of
            )
        )
    for key in sorted(intended_keys & membership_keys):
        row = membership_by_key[key]
        if row.status is not S5MembershipAuditStatus.COVERED_VERIFIED:
            blockers.append(
                S5BaseDiagnosticReadinessBlocker(
                    code,
                    f"membership_{row.status.value}",
                    key.instrument_id,
                    key.as_of,
                )
            )


def _reconcile_eligibility(
    *,
    intended_keys: set[S5BaseDiagnosticPopulationKey],
    eligibility_by_key: dict[
        S5BaseDiagnosticPopulationKey, S5BaseEligibilityEvidence
    ],
    blocker_codes: set[str],
    blockers: list[S5BaseDiagnosticReadinessBlocker],
) -> None:
    code = _known_code(blocker_codes, "blocked_eligibility_evidence")
    evidence_keys = set(eligibility_by_key)
    for key in sorted(intended_keys - evidence_keys):
        blockers.append(
            S5BaseDiagnosticReadinessBlocker(
                code, "eligibility_population_missing", key.instrument_id, key.as_of
            )
        )
    for key in sorted(evidence_keys - intended_keys):
        blockers.append(
            S5BaseDiagnosticReadinessBlocker(
                code, "eligibility_population_unexpected", key.instrument_id, key.as_of
            )
        )
    for key in sorted(intended_keys & evidence_keys):
        value = eligibility_by_key[key].eligible
        if value is not True:
            blockers.append(
                S5BaseDiagnosticReadinessBlocker(
                    code,
                    (
                        "eligibility_missing"
                        if value is None
                        else "eligibility_nonpositive"
                    ),
                    key.instrument_id,
                    key.as_of,
                )
            )


def _blocker(
    protocol: S5BaseDiagnosticProtocol,
    code: str,
    reason: str,
    key: S5BaseDiagnosticPopulationKey,
    horizon: int | None = None,
) -> S5BaseDiagnosticReadinessBlocker:
    return S5BaseDiagnosticReadinessBlocker(
        _known_blocker_code(protocol, code),
        reason,
        key.instrument_id,
        key.as_of,
        horizon,
    )


def _known_blocker_code(
    protocol: S5BaseDiagnosticProtocol, code: str
) -> str:
    return _known_code(set(protocol.blocker_codes), code)


def _known_code(codes: set[str], code: str) -> str:
    if code not in codes:
        raise ValueError(f"frozen protocol is missing blocker code: {code}")
    return code


def _validate_strict_sessions(sessions: tuple[date, ...]) -> None:
    if not sessions:
        raise ValueError("market_sessions cannot be empty")
    if any(left >= right for left, right in pairwise(sessions)):
        raise ValueError("market_sessions must be strictly increasing")


def _blocker_sort_key(
    blocker: S5BaseDiagnosticReadinessBlocker,
) -> tuple[str, str, str, str, int]:
    return (
        blocker.code,
        blocker.reason,
        blocker.as_of.isoformat() if blocker.as_of else "",
        blocker.instrument_id or "",
        blocker.horizon or 0,
    )


def _nonempty_fingerprint(name: str, value: str) -> str:
    _require_text(name, value)
    return value


def _result_payload(result: S5BaseDiagnosticReadiness) -> dict[str, object]:
    return {
        "schema": result.schema,
        "verdict": result.verdict.value,
        "intended_start": result.intended_start.isoformat(),
        "intended_end": result.intended_end.isoformat(),
        "admissible_start": (
            result.admissible_start.isoformat() if result.admissible_start else None
        ),
        "admissible_end": (
            result.admissible_end.isoformat() if result.admissible_end else None
        ),
        "frozen_period_end": result.frozen_period_end.isoformat(),
        "population_size": result.population_size,
        "protocol_fingerprint": result.protocol_fingerprint,
        "membership_audit_fingerprint": result.membership_audit_fingerprint,
        "eligibility_evidence_fingerprint": result.eligibility_evidence_fingerprint,
        "benchmark_coverage_fingerprint": result.benchmark_coverage_fingerprint,
        "sessions_fingerprint": result.sessions_fingerprint,
        "blockers": [
            {
                "code": blocker.code,
                "reason": blocker.reason,
                "instrument_id": blocker.instrument_id,
                "as_of": blocker.as_of.isoformat() if blocker.as_of else None,
                "horizon": blocker.horizon,
            }
            for blocker in result.blockers
        ],
        "research_only": result.research_only,
        "outcome_data_loaded": result.outcome_data_loaded,
        "performance_claim": result.performance_claim,
        "broker_order_authority": result.broker_order_authority,
    }


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
