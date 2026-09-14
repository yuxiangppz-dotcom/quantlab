from __future__ import annotations

from datetime import date, timedelta

import pytest

from quantlab.research.s5_materialization import S5MembershipEvidence
from quantlab.research.s5_membership_audit import (
    S5MembershipAuditStatus,
    S5MembershipRequirement,
    audit_s5_membership_readiness,
    frozen_s5_diagnostic_protocol,
)

D1 = date(2025, 6, 30)
D2 = date(2025, 7, 1)
D3 = date(2026, 1, 5)


def _requirement(
    instrument_id: str,
    as_of: date,
    expected_sector_id: str | None = "POWER",
) -> S5MembershipRequirement:
    return S5MembershipRequirement(
        instrument_id=instrument_id,
        as_of=as_of,
        expected_sector_id=expected_sector_id,
    )


def _fact(
    instrument_id: str,
    sector_id: str = "POWER",
    *,
    start: date = date(2025, 1, 1),
    end: date | None = None,
    source: str = "membership_v1",
    verified: bool = True,
) -> S5MembershipEvidence:
    return S5MembershipEvidence(
        instrument_id=instrument_id,
        sector_id=sector_id,
        effective_from=start,
        effective_to=end,
        source_id=source,
        pit_verified=verified,
    )


def test_verified_agreeing_sources_are_covered_and_order_invariant() -> None:
    requirements = (
        _requirement("000001.SZ", D1),
        _requirement("600001.SH", D1, "BANK"),
    )
    evidence = (
        _fact("000001.SZ", source="source_b"),
        _fact("000001.SZ", source="source_a"),
        _fact("600001.SH", "BANK", source="bank_source"),
    )

    first = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=evidence,
    )
    second = audit_s5_membership_readiness(
        requirements=tuple(reversed(requirements)),
        evidence=tuple(reversed(evidence)),
    )

    assert first == second
    assert first.status == "ready_for_frozen_diagnostic"
    assert first.coverage_rate == 1.0
    assert first.blockers == ()
    assert first.fully_covered_dates == (D1,)
    assert first.earliest_fully_covered_date == D1
    assert first.latest_fully_covered_date == D1
    assert first.rows[0].source_ids == ("source_a", "source_b")


def test_status_precedence_missing_unverified_conflicting_and_mismatch() -> None:
    requirements = (
        _requirement("000001.SZ", D1),
        _requirement("000002.SZ", D1),
        _requirement("000003.SZ", D1),
        _requirement("000004.SZ", D1),
    )
    evidence = (
        _fact("000002.SZ", verified=False),
        _fact("000003.SZ", "POWER", source="a"),
        _fact("000003.SZ", "ENERGY", source="b"),
        _fact("000004.SZ", "ENERGY"),
    )

    audit = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=evidence,
    )
    by_id = {row.instrument_id: row for row in audit.rows}

    assert by_id["000001.SZ"].status is S5MembershipAuditStatus.MISSING
    assert by_id["000002.SZ"].status is S5MembershipAuditStatus.UNVERIFIED
    assert by_id["000003.SZ"].status is S5MembershipAuditStatus.CONFLICTING
    assert by_id["000004.SZ"].status is S5MembershipAuditStatus.MISMATCH
    assert audit.status == "blocked_membership_evidence"
    assert audit.coverage_rate == 0.0
    assert audit.fully_covered_dates == ()
    assert audit.blockers == (
        "mismatch:1",
        "missing:1",
        "unverified:1",
        "conflicting:1",
    )


def test_unverified_active_fact_blocks_even_when_verified_fact_agrees() -> None:
    audit = audit_s5_membership_readiness(
        requirements=(_requirement("000001.SZ", D1),),
        evidence=(
            _fact("000001.SZ", source="verified"),
            _fact("000001.SZ", source="unverified", verified=False),
        ),
    )

    assert audit.rows[0].status is S5MembershipAuditStatus.UNVERIFIED
    assert audit.rows[0].resolved_sector_id is None


def test_interval_boundaries_are_inclusive_and_date_gaps_are_missing() -> None:
    requirements = (
        _requirement("000001.SZ", D1),
        _requirement("000001.SZ", D2),
        _requirement("000001.SZ", D3),
    )
    evidence = (
        _fact("000001.SZ", end=D1),
        _fact("000001.SZ", start=D3, source="later"),
    )
    audit = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=evidence,
    )

    assert [row.status for row in audit.rows] == [
        S5MembershipAuditStatus.COVERED_VERIFIED,
        S5MembershipAuditStatus.MISSING,
        S5MembershipAuditStatus.COVERED_VERIFIED,
    ]
    assert audit.fully_covered_dates == (D1, D3)


def test_open_ended_fact_covers_later_required_date() -> None:
    audit = audit_s5_membership_readiness(
        requirements=(_requirement("000001.SZ", D3),),
        evidence=(_fact("000001.SZ", end=None),),
    )

    assert audit.rows[0].status is S5MembershipAuditStatus.COVERED_VERIFIED


def test_future_start_fact_does_not_repair_or_rewrite_earlier_audit() -> None:
    requirement = (_requirement("000001.SZ", D1),)
    base = audit_s5_membership_readiness(requirements=requirement, evidence=())
    future = audit_s5_membership_readiness(
        requirements=requirement,
        evidence=(
            _fact(
                "000001.SZ",
                start=D1 + timedelta(days=30),
                source="future",
            ),
        ),
    )

    assert future.rows == base.rows
    assert future.fingerprint == base.fingerprint


def test_future_end_date_is_equivalent_to_open_end_for_audit_horizon() -> None:
    requirements = (_requirement("000001.SZ", D1),)
    open_end = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=(_fact("000001.SZ", end=None),),
    )
    future_end = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=(_fact("000001.SZ", end=D1 + timedelta(days=365)),),
    )

    assert future_end.rows == open_end.rows
    assert future_end.fingerprint == open_end.fingerprint


def test_historical_source_or_sector_drift_changes_fingerprint() -> None:
    requirements = (_requirement("000001.SZ", D1),)
    base = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=(_fact("000001.SZ"),),
    )
    source_drift = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=(_fact("000001.SZ", source="membership_revised"),),
    )
    sector_drift = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=(_fact("000001.SZ", "ENERGY"),),
    )

    assert source_drift.fingerprint != base.fingerprint
    assert sector_drift.fingerprint != base.fingerprint


def test_year_summaries_preserve_full_denominator() -> None:
    requirements = (
        _requirement("000001.SZ", D1),
        _requirement("000002.SZ", D1),
        _requirement("000001.SZ", D3),
    )
    evidence = (
        _fact("000001.SZ"),
        _fact("000002.SZ", verified=False),
    )
    audit = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=evidence,
    )

    y2025, y2026 = audit.year_summaries
    assert (y2025.year, y2025.total, y2025.covered_verified) == (2025, 2, 1)
    assert y2025.unverified == 1
    assert y2025.coverage_rate == 0.5
    assert (y2026.year, y2026.total, y2026.covered_verified) == (2026, 1, 1)
    assert y2026.coverage_rate == 1.0


def test_sector_and_source_summaries_preserve_population_and_provenance() -> None:
    requirements = (
        _requirement("000001.SZ", D1, "POWER"),
        _requirement("000002.SZ", D1, "POWER"),
        _requirement("600001.SH", D1, "BANK"),
        _requirement("600002.SH", D1, None),
    )
    evidence = (
        _fact("000001.SZ", source="source_a"),
        _fact("000001.SZ", source="source_b"),
        _fact("000002.SZ", verified=False, source="source_a"),
        _fact("600001.SH", "ENERGY", source="source_c"),
    )
    audit = audit_s5_membership_readiness(
        requirements=requirements,
        evidence=evidence,
    )

    by_sector = {summary.sector_id: summary for summary in audit.sector_summaries}
    power = by_sector["POWER"]
    bank = by_sector["BANK"]
    unknown = by_sector[None]
    assert (
        power.sector_id,
        power.total,
        power.covered_verified,
        power.unverified,
        power.coverage_rate,
    ) == ("POWER", 2, 1, 1, 0.5)
    assert (
        bank.sector_id,
        bank.total,
        bank.mismatch,
        bank.coverage_rate,
    ) == ("BANK", 1, 1, 0.0)
    assert (
        unknown.sector_id,
        unknown.total,
        unknown.missing,
        unknown.coverage_rate,
    ) == (None, 1, 1, 0.0)

    by_source = {summary.source_id: summary for summary in audit.source_summaries}
    assert set(by_source) == {"source_a", "source_b", "source_c"}
    assert (
        by_source["source_a"].requirement_count,
        by_source["source_a"].covered_verified,
        by_source["source_a"].unverified,
        by_source["source_a"].coverage_rate,
    ) == (2, 1, 1, 0.5)
    assert (
        by_source["source_b"].requirement_count,
        by_source["source_b"].covered_verified,
        by_source["source_b"].coverage_rate,
    ) == (1, 1, 1.0)
    assert (
        by_source["source_c"].requirement_count,
        by_source["source_c"].mismatch,
        by_source["source_c"].coverage_rate,
    ) == (1, 1, 0.0)


def test_empty_requirement_population_is_rejected_but_empty_evidence_is_blocked() -> None:
    with pytest.raises(ValueError, match="requirements cannot be empty"):
        audit_s5_membership_readiness(requirements=(), evidence=())

    blocked = audit_s5_membership_readiness(
        requirements=(_requirement("000001.SZ", D1),),
        evidence=(),
    )
    assert blocked.status == "blocked_membership_evidence"
    assert blocked.rows[0].status is S5MembershipAuditStatus.MISSING
    assert blocked.coverage_rate == 0.0


def test_duplicate_requirement_is_rejected() -> None:
    requirement = _requirement("000001.SZ", D1)
    with pytest.raises(ValueError, match="duplicate membership requirement"):
        audit_s5_membership_readiness(
            requirements=(requirement, requirement),
            evidence=(_fact("000001.SZ"),),
        )


def test_frozen_diagnostic_protocol_is_outcome_free_and_non_authoritative() -> None:
    protocol = frozen_s5_diagnostic_protocol()

    assert protocol.schema == "quantlab_s5_diagnostic_protocol_v1"
    assert protocol.strategy_id == "s5_sector_bottom_reversal_v1"
    assert protocol.materializer_schema == "quantlab_s5_materialization_v1"
    assert protocol.membership_required_rate == 1.0
    assert protocol.benchmark_id == "000985.SH"
    assert protocol.forward_horizons == (5, 10, 20)
    assert protocol.run_budget == 1
    assert protocol.outcome_freeze_required is True
    assert protocol.allow_parameter_rescan is False
    assert protocol.performance_claim is False
    assert protocol.broker_order_authority is False
    assert len(protocol.fingerprint) == 64
