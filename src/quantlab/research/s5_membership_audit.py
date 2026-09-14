"""Historical industry-membership readiness audit for S5.

The audit is deliberately outcome-free.  It answers whether a predeclared
stock/date population has point-in-time industry identity evidence that is safe
to feed into the frozen S5 materialization layer.  It never infers membership
from current classifications and never evaluates S5 returns.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_materialization import S5MembershipEvidence

_AUDIT_SCHEMA = "quantlab_s5_membership_readiness_v1"
_PROTOCOL_SCHEMA = "quantlab_s5_diagnostic_protocol_v1"


class S5MembershipAuditStatus(StrEnum):
    """Readiness state for one required instrument/date membership identity."""

    COVERED_VERIFIED = "covered_verified"
    MISMATCH = "mismatch"
    MISSING = "missing"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class S5MembershipRequirement:
    """One predeclared membership identity required by a future S5 diagnostic."""

    instrument_id: str
    as_of: date
    expected_sector_id: str | None = None

    def __post_init__(self) -> None:
        _require_text("instrument_id", self.instrument_id)
        if self.expected_sector_id is not None:
            _require_text("expected_sector_id", self.expected_sector_id)


@dataclass(frozen=True)
class S5MembershipAuditRow:
    instrument_id: str
    as_of: date
    expected_sector_id: str | None
    resolved_sector_id: str | None
    status: S5MembershipAuditStatus
    source_ids: tuple[str, ...]
    active_evidence_count: int


@dataclass(frozen=True)
class S5MembershipYearSummary:
    year: int
    total: int
    covered_verified: int
    mismatch: int
    missing: int
    unverified: int
    conflicting: int
    coverage_rate: float


@dataclass(frozen=True)
class S5MembershipAudit:
    """Complete deterministic membership audit for a non-empty population."""

    rows: tuple[S5MembershipAuditRow, ...]
    year_summaries: tuple[S5MembershipYearSummary, ...]
    fully_covered_dates: tuple[date, ...]
    earliest_fully_covered_date: date | None
    latest_fully_covered_date: date | None
    status: str
    blockers: tuple[str, ...]
    fingerprint: str
    research_only: bool = True
    performance_claim: bool = False
    broker_order_authority: bool = False

    @property
    def coverage_rate(self) -> float:
        return sum(
            row.status is S5MembershipAuditStatus.COVERED_VERIFIED
            for row in self.rows
        ) / len(self.rows)


@dataclass(frozen=True)
class S5DiagnosticProtocol:
    """Frozen first-retrospective S5 diagnostic contract; no outcomes included."""

    schema: str
    strategy_id: str
    materializer_schema: str
    membership_required_rate: float
    benchmark_id: str
    forward_horizons: tuple[int, ...]
    comparison_ids: tuple[str, ...]
    signal_metrics: tuple[str, ...]
    run_budget: int
    outcome_freeze_required: bool
    allow_parameter_rescan: bool
    performance_claim: bool
    broker_order_authority: bool
    fingerprint: str


def audit_s5_membership_readiness(
    *,
    requirements: tuple[S5MembershipRequirement, ...],
    evidence: tuple[S5MembershipEvidence, ...],
) -> S5MembershipAudit:
    """Audit membership coverage without loading prices or future outcomes."""

    if not requirements:
        raise ValueError("requirements cannot be empty")

    requirement_by_key: dict[tuple[str, date], S5MembershipRequirement] = {}
    for requirement in requirements:
        key = (requirement.instrument_id, requirement.as_of)
        if key in requirement_by_key:
            raise ValueError(
                "duplicate membership requirement: "
                f"{requirement.instrument_id} {requirement.as_of}"
            )
        requirement_by_key[key] = requirement

    evidence_by_instrument: dict[str, list[S5MembershipEvidence]] = defaultdict(list)
    for fact in evidence:
        evidence_by_instrument[fact.instrument_id].append(fact)

    rows = tuple(
        _audit_requirement(
            requirement,
            evidence_by_instrument.get(requirement.instrument_id, []),
        )
        for requirement in sorted(
            requirement_by_key.values(),
            key=lambda item: (item.as_of, item.instrument_id),
        )
    )

    year_summaries = _year_summaries(rows)
    fully_covered_dates = _fully_covered_dates(rows)
    counts = Counter(row.status for row in rows)
    blockers = tuple(
        f"{status.value}:{counts[status]}"
        for status in (
            S5MembershipAuditStatus.MISMATCH,
            S5MembershipAuditStatus.MISSING,
            S5MembershipAuditStatus.UNVERIFIED,
            S5MembershipAuditStatus.CONFLICTING,
        )
        if counts[status]
    )
    status = (
        "ready_for_frozen_diagnostic"
        if not blockers
        else "blocked_membership_evidence"
    )

    fingerprint = _audit_fingerprint(
        requirements=tuple(requirement_by_key.values()),
        evidence=evidence,
    )
    return S5MembershipAudit(
        rows=rows,
        year_summaries=year_summaries,
        fully_covered_dates=fully_covered_dates,
        earliest_fully_covered_date=(
            fully_covered_dates[0] if fully_covered_dates else None
        ),
        latest_fully_covered_date=(
            fully_covered_dates[-1] if fully_covered_dates else None
        ),
        status=status,
        blockers=blockers,
        fingerprint=fingerprint,
    )


def frozen_s5_diagnostic_protocol() -> S5DiagnosticProtocol:
    """Return the versioned, outcome-free first S5 diagnostic protocol."""

    payload = {
        "schema": _PROTOCOL_SCHEMA,
        "strategy_id": "s5_sector_bottom_reversal_v1",
        "materializer_schema": "quantlab_s5_materialization_v1",
        "membership_required_rate": 1.0,
        "benchmark_id": "000985.SH",
        "forward_horizons": [5, 10, 20],
        "comparison_ids": [
            "simple_sector_reversal",
            "simple_industry_trend",
            "alpha158_ridge_existing",
            "broad_market_control",
        ],
        "signal_metrics": [
            "eligible_sector_count",
            "eligible_stock_count",
            "dynamic_n_equity_exposure",
            "stock_rank_ic_when_defined",
            "selected_vs_eligible_nonselected_spread",
        ],
        "run_budget": 1,
        "outcome_freeze_required": True,
        "allow_parameter_rescan": False,
        "performance_claim": False,
        "broker_order_authority": False,
    }
    return S5DiagnosticProtocol(
        schema=str(payload["schema"]),
        strategy_id=str(payload["strategy_id"]),
        materializer_schema=str(payload["materializer_schema"]),
        membership_required_rate=float(payload["membership_required_rate"]),
        benchmark_id=str(payload["benchmark_id"]),
        forward_horizons=tuple(payload["forward_horizons"]),
        comparison_ids=tuple(payload["comparison_ids"]),
        signal_metrics=tuple(payload["signal_metrics"]),
        run_budget=int(payload["run_budget"]),
        outcome_freeze_required=bool(payload["outcome_freeze_required"]),
        allow_parameter_rescan=bool(payload["allow_parameter_rescan"]),
        performance_claim=False,
        broker_order_authority=False,
        fingerprint=canonical_payload_fingerprint(payload),
    )


def _audit_requirement(
    requirement: S5MembershipRequirement,
    evidence: list[S5MembershipEvidence],
) -> S5MembershipAuditRow:
    active = [
        fact
        for fact in evidence
        if fact.effective_from <= requirement.as_of
        and (fact.effective_to is None or requirement.as_of <= fact.effective_to)
    ]
    source_ids = tuple(sorted(fact.source_id for fact in active))
    if not active:
        return _row(
            requirement,
            status=S5MembershipAuditStatus.MISSING,
            resolved_sector_id=None,
            source_ids=(),
            count=0,
        )

    if any(not fact.pit_verified for fact in active):
        return _row(
            requirement,
            status=S5MembershipAuditStatus.UNVERIFIED,
            resolved_sector_id=None,
            source_ids=source_ids,
            count=len(active),
        )

    sectors = {fact.sector_id for fact in active}
    if len(sectors) != 1:
        return _row(
            requirement,
            status=S5MembershipAuditStatus.CONFLICTING,
            resolved_sector_id=None,
            source_ids=source_ids,
            count=len(active),
        )

    resolved_sector_id = next(iter(sectors))
    status = S5MembershipAuditStatus.COVERED_VERIFIED
    if (
        requirement.expected_sector_id is not None
        and resolved_sector_id != requirement.expected_sector_id
    ):
        status = S5MembershipAuditStatus.MISMATCH
    return _row(
        requirement,
        status=status,
        resolved_sector_id=resolved_sector_id,
        source_ids=source_ids,
        count=len(active),
    )


def _row(
    requirement: S5MembershipRequirement,
    *,
    status: S5MembershipAuditStatus,
    resolved_sector_id: str | None,
    source_ids: tuple[str, ...],
    count: int,
) -> S5MembershipAuditRow:
    return S5MembershipAuditRow(
        instrument_id=requirement.instrument_id,
        as_of=requirement.as_of,
        expected_sector_id=requirement.expected_sector_id,
        resolved_sector_id=resolved_sector_id,
        status=status,
        source_ids=source_ids,
        active_evidence_count=count,
    )


def _year_summaries(
    rows: tuple[S5MembershipAuditRow, ...],
) -> tuple[S5MembershipYearSummary, ...]:
    by_year: dict[int, list[S5MembershipAuditRow]] = defaultdict(list)
    for row in rows:
        by_year[row.as_of.year].append(row)

    summaries: list[S5MembershipYearSummary] = []
    for year in sorted(by_year):
        year_rows = by_year[year]
        counts = Counter(row.status for row in year_rows)
        covered = counts[S5MembershipAuditStatus.COVERED_VERIFIED]
        total = len(year_rows)
        summaries.append(
            S5MembershipYearSummary(
                year=year,
                total=total,
                covered_verified=covered,
                mismatch=counts[S5MembershipAuditStatus.MISMATCH],
                missing=counts[S5MembershipAuditStatus.MISSING],
                unverified=counts[S5MembershipAuditStatus.UNVERIFIED],
                conflicting=counts[S5MembershipAuditStatus.CONFLICTING],
                coverage_rate=covered / total,
            )
        )
    return tuple(summaries)


def _fully_covered_dates(
    rows: tuple[S5MembershipAuditRow, ...],
) -> tuple[date, ...]:
    by_date: dict[date, list[S5MembershipAuditRow]] = defaultdict(list)
    for row in rows:
        by_date[row.as_of].append(row)
    return tuple(
        as_of
        for as_of in sorted(by_date)
        if all(
            row.status is S5MembershipAuditStatus.COVERED_VERIFIED
            for row in by_date[as_of]
        )
    )


def _audit_fingerprint(
    *,
    requirements: tuple[S5MembershipRequirement, ...],
    evidence: tuple[S5MembershipEvidence, ...],
) -> str:
    ordered_requirements = sorted(
        requirements,
        key=lambda item: (item.as_of, item.instrument_id),
    )
    required_instruments = {item.instrument_id for item in ordered_requirements}
    last_required_date = max(item.as_of for item in ordered_requirements)

    relevant_evidence = sorted(
        (
            fact.instrument_id,
            fact.sector_id,
            fact.effective_from.isoformat(),
            _end_for_fingerprint(fact, last_required_date),
            fact.source_id,
            fact.pit_verified,
        )
        for fact in evidence
        if fact.instrument_id in required_instruments
        and fact.effective_from <= last_required_date
    )
    payload = {
        "schema": _AUDIT_SCHEMA,
        "requirements": [
            {
                "instrument_id": item.instrument_id,
                "as_of": item.as_of.isoformat(),
                "expected_sector_id": item.expected_sector_id,
            }
            for item in ordered_requirements
        ],
        "evidence": relevant_evidence,
    }
    return canonical_payload_fingerprint(payload)


def _end_for_fingerprint(
    evidence: S5MembershipEvidence,
    last_required_date: date,
) -> str:
    if evidence.effective_to is None or evidence.effective_to > last_required_date:
        return "after_required_horizon_or_open"
    return evidence.effective_to.isoformat()


def _require_text(name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
