from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_decision import S5BaseAdmissionState
from quantlab.research.s5_base_diagnostic_inputs import (
    S5BaseBenchmarkOutcomeRow,
    S5BaseComparisonOutcomeRow,
    S5BaseDiagnosticInputPackage,
    S5BaseDiagnosticSignalRow,
    S5BaseDiagnosticSource,
    S5BaseDiagnosticTargetRow,
    S5BaseInstrumentOutcomeRow,
)
from quantlab.research.s5_base_diagnostic_metrics import (
    compute_s5_base_diagnostic_metrics,
)
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)


def _package() -> S5BaseDiagnosticInputPackage:
    protocol = frozen_s5_base_diagnostic_protocol()
    first = date(2024, 1, 31)
    second = date(2024, 2, 1)
    signals = (
        _signal("A", first, S5BaseState.BREAKOUT_CONFIRMED, True),
        _signal("B", first, S5BaseState.BASE_READY, False),
        _signal("C", first, S5BaseState.BASE_BUILDING, False, eligible=False),
        _signal("D", first, S5BaseState.BASE_READY, True),
        _signal("A", second, S5BaseState.BASE_READY, False),
        _signal("B", second, S5BaseState.BREAKOUT_CONFIRMED, True),
        _signal("C", second, S5BaseState.FAILED, False, eligible=False),
        _signal("D", second, S5BaseState.BASE_READY, False),
    )
    base_returns = {
        ("A", first): 0.10,
        ("B", first): 0.02,
        ("C", first): -0.05,
        ("D", first): 0.04,
        ("A", second): 0.00,
        ("B", second): 0.08,
        ("C", second): -0.10,
        ("D", second): 0.02,
    }
    horizon_scale = {1: 1.0, 5: 1.1, 10: 1.2, 20: 1.3}
    instrument_outcomes = tuple(
        S5BaseInstrumentOutcomeRow(
            row.instrument_id,
            row.as_of,
            horizon,
            row.as_of + timedelta(days=horizon),
            base_returns[(row.instrument_id, row.as_of)] * horizon_scale[horizon],
        )
        for row in signals
        for horizon in protocol.signal_horizons
    )
    benchmark_outcomes = tuple(
        S5BaseBenchmarkOutcomeRow(
            as_of,
            horizon,
            as_of + timedelta(days=horizon),
            (0.01 if as_of == first else -0.01) * horizon_scale[horizon],
        )
        for as_of in (first, second)
        for horizon in protocol.signal_horizons
    )
    benchmark = {
        (row.as_of, row.horizon): row.close_return for row in benchmark_outcomes
    }
    comparison_outcomes = tuple(
        S5BaseComparisonOutcomeRow(
            comparison_id,
            as_of,
            horizon,
            as_of + timedelta(days=horizon),
            (
                benchmark[(as_of, horizon)]
                if comparison_id == "broad_market_control"
                else 0.015 * horizon_scale[horizon]
            ),
        )
        for comparison_id in protocol.comparison_ids
        for as_of in (first, second)
        for horizon in protocol.signal_horizons
    )
    return S5BaseDiagnosticInputPackage(
        schema="quantlab_s5b_diagnostic_input_v1",
        protocol_fingerprint=protocol.fingerprint,
        readiness_fingerprint="readiness",
        intended_start=first,
        intended_end=second,
        population_size=len(signals),
        horizons=protocol.signal_horizons,
        comparison_ids=protocol.comparison_ids,
        signal_rows=signals,
        target_rows=(
            S5BaseDiagnosticTargetRow(first, 0.92),
            S5BaseDiagnosticTargetRow(second, 0.96),
        ),
        instrument_outcomes=instrument_outcomes,
        benchmark_outcomes=benchmark_outcomes,
        comparison_outcomes=comparison_outcomes,
        sources=(S5BaseDiagnosticSource("synthetic", "synthetic-v1"),),
        label_semantics=protocol.horizon_semantics,
    )


def _signal(
    instrument_id: str,
    as_of: date,
    state: S5BaseState,
    selected: bool,
    *,
    eligible: bool = True,
) -> S5BaseDiagnosticSignalRow:
    return S5BaseDiagnosticSignalRow(
        instrument_id,
        "sector",
        as_of,
        state,
        (
            S5BaseAdmissionState.ELIGIBLE
            if eligible
            else S5BaseAdmissionState.INELIGIBLE
        ),
        selected,
        0.04 if selected else 0.0,
    )


def test_state_counts_transitions_and_allocations_are_complete() -> None:
    result = compute_s5_base_diagnostic_metrics(_package())

    counts = {row.state: row for row in result.state_counts}
    assert counts[S5BaseState.UNKNOWN].count == 0
    assert counts[S5BaseState.BREAKOUT_CONFIRMED].count == 2
    assert counts[S5BaseState.BREAKOUT_CONFIRMED].rate == pytest.approx(0.25)
    assert counts[S5BaseState.BASE_READY].count == 4

    transitions = {
        (row.from_state, row.to_state): row for row in result.state_transitions
    }
    assert transitions[
        (S5BaseState.BREAKOUT_CONFIRMED, S5BaseState.BASE_READY)
    ].conditional_rate == pytest.approx(1.0)
    assert transitions[
        (S5BaseState.BASE_READY, S5BaseState.BREAKOUT_CONFIRMED)
    ].conditional_rate == pytest.approx(0.5)
    assert transitions[
        (S5BaseState.BASE_READY, S5BaseState.BASE_READY)
    ].conditional_rate == pytest.approx(0.5)

    assert [(row.selected_n, row.research_target_exposure, row.cash_exposure) for row in result.allocations] == [
        (2, 0.08, 0.92),
        (1, 0.04, 0.96),
    ]
    assert result.transition_semantics == "adjacent_observed_signal_dates_per_instrument"


def test_state_distribution_freezes_tail_and_hit_rate_semantics() -> None:
    result = compute_s5_base_diagnostic_metrics(_package())
    rows = {
        (row.group_id, row.horizon): row.stats
        for row in result.state_distributions
    }

    confirmed = rows[("breakout_confirmed", 1)]
    assert confirmed.observation_count == 2
    assert confirmed.mean == pytest.approx(0.09)
    assert confirmed.median == pytest.approx(0.09)
    assert confirmed.p10 == pytest.approx(0.082)
    assert confirmed.minimum == pytest.approx(0.08)
    assert confirmed.strict_positive_rate == pytest.approx(1.0)

    unknown = rows[("unknown", 1)]
    assert unknown.observation_count == 0
    assert unknown.mean is None
    assert unknown.p10 is None
    assert unknown.strict_positive_rate is None


def test_cohort_spreads_equal_weight_dates_after_within_date_means() -> None:
    result = compute_s5_base_diagnostic_metrics(_package())
    rows = {(row.spread_id, row.horizon): row.stats for row in result.cohort_spreads}

    confirmed = rows[("confirmed_vs_ready", 1)]
    assert confirmed.observation_count == 2
    assert confirmed.mean == pytest.approx(0.07)
    assert confirmed.minimum == pytest.approx(0.07)

    selected = rows[("selected_vs_eligible_nonselected", 1)]
    assert selected.observation_count == 2
    assert selected.mean == pytest.approx(0.055)
    assert result.spread_weighting == (
        "equal_weight_within_date_then_equal_weight_across_dates"
    )


def test_comparison_spreads_are_date_paired_for_every_frozen_control() -> None:
    result = compute_s5_base_diagnostic_metrics(_package())
    rows = {
        (row.spread_id, row.horizon): row.stats
        for row in result.comparison_spreads
    }

    assert len(result.comparison_spreads) == 20
    assert rows[("s5a_frozen", 1)].mean == pytest.approx(0.06)
    assert rows[("broad_market_control", 1)].mean == pytest.approx(0.075)
    assert all(stats.observation_count == 2 for stats in rows.values())


def test_month_and_same_horizon_market_regimes_are_explicit() -> None:
    result = compute_s5_base_diagnostic_metrics(_package())
    monthly = {
        (row.group_id, row.horizon): row.stats
        for row in result.monthly_selected_distributions
    }
    regimes = {
        (row.group_id, row.horizon): row.stats
        for row in result.regime_selected_distributions
    }

    assert monthly[("2024-01", 1)].mean == pytest.approx(0.07)
    assert monthly[("2024-02", 1)].mean == pytest.approx(0.08)
    assert regimes[("positive", 1)].mean == pytest.approx(0.07)
    assert regimes[("negative", 1)].mean == pytest.approx(0.08)
    assert regimes[("flat", 1)].observation_count == 0
    assert regimes[("flat", 1)].mean is None


def test_metrics_are_deterministic_and_never_acquire_execution_authority() -> None:
    package = _package()
    first = compute_s5_base_diagnostic_metrics(package)
    second = compute_s5_base_diagnostic_metrics(package)

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.input_fingerprint == package.fingerprint
    assert first.label_semantics.endswith("not_execution_pnl")
    assert first.diagnostic_only is True
    assert first.executable_pnl is False
    assert first.holding_policy_frozen is False
    assert first.performance_claim is False
    assert first.broker_order_authority is False


def test_kernel_rejects_protocol_or_grid_drift() -> None:
    package = _package()
    with pytest.raises(ValueError, match="frozen S5-B protocol"):
        compute_s5_base_diagnostic_metrics(
            replace(package, protocol_fingerprint="changed")
        )
    with pytest.raises(ValueError, match="instrument outcome grid"):
        compute_s5_base_diagnostic_metrics(
            replace(package, instrument_outcomes=package.instrument_outcomes[1:])
        )
