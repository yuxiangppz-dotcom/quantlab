from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_decision import S5BaseAdmissionState
from quantlab.research.s5_base_diagnostic_inputs import (
    S5BaseBenchmarkOutcomeRow,
    S5BaseComparisonOutcomeRow,
    S5BaseDiagnosticSignalRow,
    S5BaseDiagnosticSource,
    S5BaseDiagnosticTargetRow,
    S5BaseInstrumentOutcomeRow,
    admit_s5_base_diagnostic_inputs,
)
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)
from quantlab.research.s5_base_diagnostic_readiness import (
    S5BaseDiagnosticPopulationKey,
    S5BaseEligibilityEvidence,
    evaluate_s5_base_diagnostic_readiness,
)
from quantlab.research.s5_membership_audit import (
    S5MembershipAudit,
    S5MembershipAuditRow,
    S5MembershipAuditStatus,
)


def _world() -> dict[str, object]:
    sessions = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(150))
    as_of = sessions[120]
    population = (
        S5BaseDiagnosticPopulationKey("000001.SZ", as_of),
        S5BaseDiagnosticPopulationKey("600000.SH", as_of),
    )
    eligibility = tuple(
        S5BaseEligibilityEvidence(
            key.instrument_id,
            key.as_of,
            True,
            "eligibility-v1",
            f"eligible-{key.instrument_id}",
        )
        for key in population
    )
    rows = tuple(
        S5MembershipAuditRow(
            key.instrument_id,
            key.as_of,
            "bank",
            "bank",
            S5MembershipAuditStatus.COVERED_VERIFIED,
            ("membership-v1",),
            1,
        )
        for key in population
    )
    membership = S5MembershipAudit(
        rows=rows,
        year_summaries=(),
        sector_summaries=(),
        source_summaries=(),
        fully_covered_dates=(as_of,),
        earliest_fully_covered_date=as_of,
        latest_fully_covered_date=as_of,
        status="ready_for_frozen_diagnostic",
        blockers=(),
        fingerprint="membership-fingerprint",
    )
    protocol = frozen_s5_base_diagnostic_protocol()
    endpoints = {h: sessions[120 + h] for h in protocol.signal_horizons}
    readiness = evaluate_s5_base_diagnostic_readiness(
        protocol=protocol,
        population=population,
        membership_audit=membership,
        eligibility_evidence=eligibility,
        benchmark_coverage_dates=(as_of, *endpoints.values()),
        market_sessions=sessions,
        frozen_period_end=sessions[-1],
    )
    signals = (
        S5BaseDiagnosticSignalRow(
            "000001.SZ",
            "bank",
            as_of,
            S5BaseState.BREAKOUT_CONFIRMED,
            S5BaseAdmissionState.ELIGIBLE,
            True,
            0.04,
        ),
        S5BaseDiagnosticSignalRow(
            "600000.SH",
            "bank",
            as_of,
            S5BaseState.BASE_READY,
            S5BaseAdmissionState.ELIGIBLE,
            True,
            0.04,
        ),
    )
    instrument = tuple(
        S5BaseInstrumentOutcomeRow(
            key.instrument_id,
            as_of,
            horizon,
            endpoints[horizon],
            0.01 * horizon + index * 0.001,
        )
        for index, key in enumerate(population)
        for horizon in protocol.signal_horizons
    )
    benchmark = tuple(
        S5BaseBenchmarkOutcomeRow(
            as_of,
            horizon,
            endpoints[horizon],
            0.002 * horizon,
        )
        for horizon in protocol.signal_horizons
    )
    benchmark_by_horizon = {row.horizon: row.close_return for row in benchmark}
    comparisons = tuple(
        S5BaseComparisonOutcomeRow(
            comparison_id,
            as_of,
            horizon,
            endpoints[horizon],
            (
                benchmark_by_horizon[horizon]
                if comparison_id == "broad_market_control"
                else 0.003 * horizon
            ),
        )
        for comparison_id in protocol.comparison_ids
        for horizon in protocol.signal_horizons
    )
    return {
        "protocol": protocol,
        "readiness": readiness,
        "eligibility_evidence": eligibility,
        "signal_rows": signals,
        "target_rows": (S5BaseDiagnosticTargetRow(as_of, 0.92),),
        "instrument_outcomes": instrument,
        "benchmark_outcomes": benchmark,
        "comparison_outcomes": comparisons,
        "sources": (
            S5BaseDiagnosticSource("signals", "signals-fingerprint"),
            S5BaseDiagnosticSource("outcomes", "outcomes-fingerprint"),
            S5BaseDiagnosticSource("comparisons", "comparisons-fingerprint"),
        ),
    }


def test_complete_exact_grid_is_admitted_without_authority() -> None:
    package = admit_s5_base_diagnostic_inputs(**_world())

    assert package.population_size == 2
    assert package.horizons == (1, 5, 10, 20)
    assert len(package.instrument_outcomes) == 8
    assert len(package.benchmark_outcomes) == 4
    assert len(package.comparison_outcomes) == 20
    assert package.label_semantics.endswith("not_execution_pnl")
    assert package.diagnostic_only is True
    assert package.executable_pnl is False
    assert package.performance_claim is False
    assert package.broker_order_authority is False
    assert package.fingerprint


def test_blocked_readiness_cannot_admit_outcomes() -> None:
    values = _world()
    values["readiness"] = replace(
        values["readiness"],
        verdict="blocked_before_outcomes",
        admissible_start=None,
        admissible_end=None,
        blockers=values["readiness"].blockers,
    )
    with pytest.raises(ValueError):
        admit_s5_base_diagnostic_inputs(**values)


def test_eligibility_must_match_the_readiness_fingerprint() -> None:
    values = _world()
    evidence = list(values["eligibility_evidence"])
    evidence[0] = replace(evidence[0], evidence_fingerprint="changed")
    values["eligibility_evidence"] = tuple(evidence)

    with pytest.raises(ValueError, match="does not match readiness"):
        admit_s5_base_diagnostic_inputs(**values)


@pytest.mark.parametrize(
    "field",
    [
        "signal_rows",
        "instrument_outcomes",
        "benchmark_outcomes",
        "comparison_outcomes",
        "sources",
    ],
)
def test_duplicate_rows_fail(field: str) -> None:
    values = _world()
    values[field] = values[field] + (values[field][0],)
    with pytest.raises(ValueError, match="duplicate"):
        admit_s5_base_diagnostic_inputs(**values)


@pytest.mark.parametrize(
    "field,message",
    [
        (
            "instrument_outcomes",
            "instrument outcomes must equal population x frozen horizons",
        ),
        ("benchmark_outcomes", "benchmark outcomes must equal dates x frozen horizons"),
        (
            "comparison_outcomes",
            "comparison outcomes must equal comparison ids x dates x horizons",
        ),
    ],
)
def test_missing_grid_row_fails(field: str, message: str) -> None:
    values = _world()
    values[field] = values[field][1:]
    with pytest.raises(ValueError, match=message):
        admit_s5_base_diagnostic_inputs(**values)


def test_extra_signal_identity_fails() -> None:
    values = _world()
    extra = replace(values["signal_rows"][0], instrument_id="000002.SZ")
    values["signal_rows"] = values["signal_rows"] + (extra,)
    with pytest.raises(ValueError, match="signal population"):
        admit_s5_base_diagnostic_inputs(**values)


def test_selected_signal_must_be_eligible_and_use_four_percent() -> None:
    row = _world()["signal_rows"][0]
    with pytest.raises(ValueError, match="eligible"):
        replace(row, admission_state=S5BaseAdmissionState.INELIGIBLE)
    with pytest.raises(ValueError, match="4%"):
        replace(row, target_weight=0.05)


def test_unselected_signal_must_have_zero_weight() -> None:
    row = _world()["signal_rows"][0]
    with pytest.raises(ValueError, match="zero target weight"):
        replace(row, selected=False)


def test_target_cash_must_reconcile_without_renormalizing() -> None:
    values = _world()
    as_of = values["target_rows"][0].as_of
    values["target_rows"] = (S5BaseDiagnosticTargetRow(as_of, 0.91),)
    with pytest.raises(ValueError, match="sum exactly to one"):
        admit_s5_base_diagnostic_inputs(**values)


def test_label_end_date_must_agree_for_every_comparison() -> None:
    values = _world()
    rows = list(values["comparison_outcomes"])
    rows[0] = replace(rows[0], label_end_date=rows[0].label_end_date + timedelta(days=1))
    values["comparison_outcomes"] = tuple(rows)
    with pytest.raises(ValueError, match="label_end_date"):
        admit_s5_base_diagnostic_inputs(**values)


def test_broad_market_control_must_equal_benchmark() -> None:
    values = _world()
    rows = list(values["comparison_outcomes"])
    index = next(
        i for i, row in enumerate(rows) if row.comparison_id == "broad_market_control"
    )
    rows[index] = replace(rows[index], close_return=rows[index].close_return + 0.01)
    values["comparison_outcomes"] = tuple(rows)
    with pytest.raises(ValueError, match="must equal"):
        admit_s5_base_diagnostic_inputs(**values)


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0])
def test_outcome_values_must_be_finite_and_above_total_loss(value: float) -> None:
    row = _world()["instrument_outcomes"][0]
    with pytest.raises(ValueError, match="finite and greater"):
        replace(row, close_return=value)


def test_input_order_does_not_change_package_or_fingerprint() -> None:
    first_values = _world()
    second_values = _world()
    for field in (
        "eligibility_evidence",
        "signal_rows",
        "instrument_outcomes",
        "benchmark_outcomes",
        "comparison_outcomes",
        "sources",
    ):
        second_values[field] = tuple(reversed(second_values[field]))

    first = admit_s5_base_diagnostic_inputs(**first_values)
    second = admit_s5_base_diagnostic_inputs(**second_values)

    assert first == second
    assert first.fingerprint == second.fingerprint


def test_protocol_change_is_rejected_before_outcome_admission() -> None:
    values = _world()
    values["protocol"] = replace(values["protocol"], benchmark_id="other")
    with pytest.raises(ValueError, match="must equal the frozen"):
        admit_s5_base_diagnostic_inputs(**values)
