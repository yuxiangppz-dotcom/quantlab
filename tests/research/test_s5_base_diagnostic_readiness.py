from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, timedelta

import pytest

from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_base_diagnostic_readiness import (
    S5BaseDiagnosticPopulationKey,
    S5BaseDiagnosticReadiness,
    S5BaseDiagnosticReadinessVerdict,
    S5BaseEligibilityEvidence,
    evaluate_s5_base_diagnostic_readiness,
)
from quantlab.research.s5_membership_audit import (
    S5MembershipAudit,
    S5MembershipAuditRow,
    S5MembershipAuditStatus,
)


def _sessions(count: int = 150) -> tuple[date, ...]:
    start = date(2024, 1, 1)
    return tuple(start + timedelta(days=value) for value in range(count))


def _population(as_of: date) -> tuple[S5BaseDiagnosticPopulationKey, ...]:
    return (
        S5BaseDiagnosticPopulationKey("000001.SZ", as_of),
        S5BaseDiagnosticPopulationKey("600000.SH", as_of),
    )


def _audit(
    population: tuple[S5BaseDiagnosticPopulationKey, ...],
    *,
    status: S5MembershipAuditStatus = S5MembershipAuditStatus.COVERED_VERIFIED,
    reverse: bool = False,
) -> S5MembershipAudit:
    rows = tuple(
        S5MembershipAuditRow(
            instrument_id=key.instrument_id,
            as_of=key.as_of,
            expected_sector_id="bank",
            resolved_sector_id="bank" if status is S5MembershipAuditStatus.COVERED_VERIFIED else None,
            status=status,
            source_ids=("membership-v1",),
            active_evidence_count=1,
        )
        for key in population
    )
    if reverse:
        rows = tuple(reversed(rows))
    dates = tuple(sorted({key.as_of for key in population}))
    return S5MembershipAudit(
        rows=rows,
        year_summaries=(),
        sector_summaries=(),
        source_summaries=(),
        fully_covered_dates=(
            dates if status is S5MembershipAuditStatus.COVERED_VERIFIED else ()
        ),
        earliest_fully_covered_date=(
            dates[0] if status is S5MembershipAuditStatus.COVERED_VERIFIED else None
        ),
        latest_fully_covered_date=(
            dates[-1] if status is S5MembershipAuditStatus.COVERED_VERIFIED else None
        ),
        status=(
            "ready_for_frozen_diagnostic"
            if status is S5MembershipAuditStatus.COVERED_VERIFIED
            else "blocked_membership_evidence"
        ),
        blockers=(),
        fingerprint="membership-fingerprint",
    )


def _eligibility(
    population: tuple[S5BaseDiagnosticPopulationKey, ...],
    *,
    eligible: bool | None = True,
    reverse: bool = False,
) -> tuple[S5BaseEligibilityEvidence, ...]:
    result = tuple(
        S5BaseEligibilityEvidence(
            key.instrument_id,
            key.as_of,
            eligible,
            "eligibility-v1",
            f"eligible-{key.instrument_id}-{key.as_of}",
        )
        for key in population
    )
    return tuple(reversed(result)) if reverse else result


def _benchmark(
    sessions: tuple[date, ...],
    as_of: date,
    frozen_end: date,
) -> tuple[date, ...]:
    index = sessions.index(as_of)
    required = {as_of}
    for horizon in (1, 5, 10, 20):
        endpoint = sessions[index + horizon]
        if endpoint <= frozen_end:
            required.add(endpoint)
    return tuple(sorted(required))


def _ready_inputs() -> dict[str, object]:
    sessions = _sessions()
    as_of = sessions[120]
    population = _population(as_of)
    return {
        "protocol": frozen_s5_base_diagnostic_protocol(),
        "population": population,
        "membership_audit": _audit(population),
        "eligibility_evidence": _eligibility(population),
        "benchmark_coverage_dates": _benchmark(sessions, as_of, sessions[-1]),
        "market_sessions": sessions,
        "frozen_period_end": sessions[-1],
    }


def test_exact_population_is_ready_for_one_frozen_run() -> None:
    result = evaluate_s5_base_diagnostic_readiness(**_ready_inputs())

    assert result.verdict is S5BaseDiagnosticReadinessVerdict.READY
    assert result.population_size == 2
    assert result.admissible_start == result.intended_start
    assert result.admissible_end == result.intended_end
    assert result.blockers == ()
    assert result.outcome_data_loaded is False
    assert result.performance_claim is False
    assert result.broker_order_authority is False
    assert result.fingerprint


def test_empty_population_fails_before_a_verdict() -> None:
    inputs = _ready_inputs()
    inputs["population"] = ()
    with pytest.raises(ValueError, match="population cannot be empty"):
        evaluate_s5_base_diagnostic_readiness(**inputs)


@pytest.mark.parametrize(
    "field,value,match",
    [
        (
            "population",
            lambda values: values + (values[0],),
            "duplicate population key",
        ),
        (
            "eligibility_evidence",
            lambda values: values + (values[0],),
            "duplicate eligibility evidence",
        ),
        (
            "benchmark_coverage_dates",
            lambda values: values + (values[0],),
            "benchmark_coverage_dates cannot contain duplicates",
        ),
    ],
)
def test_duplicate_inputs_fail(
    field: str, value: object, match: str
) -> None:
    inputs = _ready_inputs()
    inputs[field] = value(inputs[field])
    with pytest.raises(ValueError, match=match):
        evaluate_s5_base_diagnostic_readiness(**inputs)


def test_duplicate_membership_rows_fail() -> None:
    inputs = _ready_inputs()
    audit = inputs["membership_audit"]
    inputs["membership_audit"] = replace(audit, rows=audit.rows + (audit.rows[0],))
    with pytest.raises(ValueError, match="duplicate membership row"):
        evaluate_s5_base_diagnostic_readiness(**inputs)


def test_population_as_of_must_be_a_consumed_market_session() -> None:
    inputs = _ready_inputs()
    population = _population(date(2030, 1, 1))
    inputs.update(
        population=population,
        membership_audit=_audit(population),
        eligibility_evidence=_eligibility(population),
    )
    with pytest.raises(ValueError, match="every population as_of"):
        evaluate_s5_base_diagnostic_readiness(**inputs)


def test_market_sessions_must_be_ordered_and_unique() -> None:
    inputs = _ready_inputs()
    sessions = inputs["market_sessions"]
    inputs["market_sessions"] = sessions[:2] + (sessions[1],) + sessions[2:]
    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_s5_base_diagnostic_readiness(**inputs)


def test_protocol_must_be_the_exact_frozen_contract() -> None:
    inputs = _ready_inputs()
    inputs["protocol"] = replace(inputs["protocol"], benchmark_id="other")
    with pytest.raises(ValueError, match="must equal the frozen"):
        evaluate_s5_base_diagnostic_readiness(**inputs)


def test_missing_and_unexpected_membership_keys_block_without_shrinking() -> None:
    inputs = _ready_inputs()
    population = inputs["population"]
    extra = S5BaseDiagnosticPopulationKey("000002.SZ", population[0].as_of)
    inputs["membership_audit"] = _audit((population[0], extra))

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    reasons = {blocker.reason for blocker in result.blockers}
    assert reasons == {
        "membership_population_missing",
        "membership_population_unexpected",
    }
    assert result.verdict is S5BaseDiagnosticReadinessVerdict.BLOCKED
    assert result.admissible_start is None
    assert result.admissible_end is None
    assert result.population_size == 2


def test_nonverified_membership_blocks() -> None:
    inputs = _ready_inputs()
    inputs["membership_audit"] = _audit(
        inputs["population"], status=S5MembershipAuditStatus.UNVERIFIED
    )

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert {item.reason for item in result.blockers} == {"membership_unverified"}
    assert len(result.blockers) == 2


@pytest.mark.parametrize(
    "eligible,reason",
    [(None, "eligibility_missing"), (False, "eligibility_nonpositive")],
)
def test_missing_or_nonpositive_eligibility_blocks(
    eligible: bool | None, reason: str
) -> None:
    inputs = _ready_inputs()
    inputs["eligibility_evidence"] = _eligibility(
        inputs["population"], eligible=eligible
    )

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert {item.reason for item in result.blockers} == {reason}
    assert len(result.blockers) == 2


def test_eligibility_population_mismatch_is_complete() -> None:
    inputs = _ready_inputs()
    population = inputs["population"]
    extra = S5BaseDiagnosticPopulationKey("000002.SZ", population[0].as_of)
    inputs["eligibility_evidence"] = _eligibility((population[0], extra))

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert {item.reason for item in result.blockers} == {
        "eligibility_population_missing",
        "eligibility_population_unexpected",
    }


def test_insufficient_120_session_history_blocks_each_identity() -> None:
    inputs = _ready_inputs()
    sessions = inputs["market_sessions"]
    population = _population(sessions[118])
    inputs.update(
        population=population,
        membership_audit=_audit(population),
        eligibility_evidence=_eligibility(population),
        benchmark_coverage_dates=_benchmark(sessions, sessions[118], sessions[-1]),
    )

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert {item.reason for item in result.blockers} == {
        "insufficient_120_session_feature_history"
    }
    assert len(result.blockers) == 2


def test_every_forward_endpoint_must_fit_before_frozen_end() -> None:
    inputs = _ready_inputs()
    sessions = inputs["market_sessions"]
    frozen_end = sessions[130]
    population = _population(sessions[125])
    inputs.update(
        population=population,
        membership_audit=_audit(population),
        eligibility_evidence=_eligibility(population),
        benchmark_coverage_dates=_benchmark(
            sessions, population[0].as_of, frozen_end
        ),
        frozen_period_end=frozen_end,
    )

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    boundary = [
        item
        for item in result.blockers
        if item.reason == "forward_endpoint_crosses_frozen_end"
    ]
    assert {(item.instrument_id, item.horizon) for item in boundary} == {
        ("000001.SZ", 10),
        ("000001.SZ", 20),
        ("600000.SH", 10),
        ("600000.SH", 20),
    }
    assert result.admissible_start is None


def test_benchmark_must_cover_signal_and_all_in_period_endpoints() -> None:
    inputs = _ready_inputs()
    missing = inputs["benchmark_coverage_dates"][2]
    inputs["benchmark_coverage_dates"] = tuple(
        value for value in inputs["benchmark_coverage_dates"] if value != missing
    )

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    blockers = [
        item for item in result.blockers if item.reason == "benchmark_date_missing"
    ]
    assert len(blockers) == 1
    assert blockers[0].as_of == missing
    assert blockers[0].instrument_id is None


def test_one_bad_identity_blocks_the_whole_population() -> None:
    inputs = _ready_inputs()
    evidence = list(inputs["eligibility_evidence"])
    evidence[0] = replace(evidence[0], eligible=False)
    inputs["eligibility_evidence"] = tuple(evidence)

    result = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert result.population_size == 2
    assert result.verdict is S5BaseDiagnosticReadinessVerdict.BLOCKED
    assert result.admissible_start is None
    assert result.admissible_end is None


def test_population_and_evidence_input_order_do_not_change_identity() -> None:
    original = _ready_inputs()
    reversed_inputs = _ready_inputs()
    population = tuple(reversed(reversed_inputs["population"]))
    reversed_inputs.update(
        population=population,
        membership_audit=_audit(population, reverse=True),
        eligibility_evidence=_eligibility(population, reverse=True),
        benchmark_coverage_dates=tuple(
            reversed(reversed_inputs["benchmark_coverage_dates"])
        ),
    )

    first = evaluate_s5_base_diagnostic_readiness(**original)
    second = evaluate_s5_base_diagnostic_readiness(**reversed_inputs)

    assert first == second
    assert first.fingerprint == second.fingerprint


def test_future_sessions_and_benchmark_dates_are_isolated() -> None:
    inputs = _ready_inputs()
    sessions = inputs["market_sessions"]
    frozen_end = sessions[140]
    inputs["frozen_period_end"] = frozen_end
    inputs["benchmark_coverage_dates"] = _benchmark(
        sessions, inputs["population"][0].as_of, frozen_end
    )
    first = evaluate_s5_base_diagnostic_readiness(**inputs)

    future = tuple(
        sessions[-1] + timedelta(days=value) for value in range(1, 11)
    )
    inputs["market_sessions"] = sessions + future
    inputs["benchmark_coverage_dates"] = (
        inputs["benchmark_coverage_dates"] + future
    )
    second = evaluate_s5_base_diagnostic_readiness(**inputs)

    assert first == second
    assert first.sessions_fingerprint == second.sessions_fingerprint
    assert (
        first.benchmark_coverage_fingerprint
        == second.benchmark_coverage_fingerprint
    )


def test_result_has_no_economic_outcome_payload_fields() -> None:
    names = {item.name for item in fields(S5BaseDiagnosticReadiness)}
    assert names.isdisjoint(
        {
            "returns",
            "forward_returns",
            "nav",
            "pnl",
            "orders",
            "fills",
            "positions",
        }
    )
