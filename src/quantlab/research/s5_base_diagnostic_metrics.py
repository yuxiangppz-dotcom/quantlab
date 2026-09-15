"""Pure metric kernel for the single frozen S5-B retrospective diagnostic."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_decision import S5BaseAdmissionState
from quantlab.research.s5_base_diagnostic_inputs import (
    S5BaseDiagnosticInputPackage,
    S5BaseDiagnosticSignalRow,
)
from quantlab.research.s5_base_diagnostic_protocol import (
    frozen_s5_base_diagnostic_protocol,
)

_SCHEMA = "quantlab_s5b_diagnostic_metrics_v1"
_COHORT_SPREADS = (
    "confirmed_vs_ready",
    "selected_vs_eligible_nonselected",
)
_REGIMES = ("negative", "flat", "positive")


@dataclass(frozen=True)
class S5BaseSummaryStats:
    observation_count: int
    mean: float | None
    median: float | None
    p10: float | None
    minimum: float | None
    strict_positive_rate: float | None


@dataclass(frozen=True)
class S5BaseStateCountRow:
    state: S5BaseState
    count: int
    rate: float


@dataclass(frozen=True)
class S5BaseStateTransitionRow:
    from_state: S5BaseState
    to_state: S5BaseState
    count: int
    conditional_rate: float


@dataclass(frozen=True)
class S5BaseAllocationRow:
    as_of: date
    selected_n: int
    research_target_exposure: float
    cash_exposure: float


@dataclass(frozen=True)
class S5BaseDistributionRow:
    grouping: str
    group_id: str
    period_id: str
    horizon: int
    stats: S5BaseSummaryStats


@dataclass(frozen=True)
class S5BaseSpreadRow:
    spread_id: str
    horizon: int
    stats: S5BaseSummaryStats


@dataclass(frozen=True)
class S5BaseDiagnosticMetrics:
    schema: str
    protocol_fingerprint: str
    input_fingerprint: str
    label_semantics: str
    transition_semantics: str
    spread_weighting: str
    state_counts: tuple[S5BaseStateCountRow, ...]
    state_transitions: tuple[S5BaseStateTransitionRow, ...]
    allocations: tuple[S5BaseAllocationRow, ...]
    state_distributions: tuple[S5BaseDistributionRow, ...]
    cohort_spreads: tuple[S5BaseSpreadRow, ...]
    comparison_spreads: tuple[S5BaseSpreadRow, ...]
    monthly_selected_distributions: tuple[S5BaseDistributionRow, ...]
    regime_selected_distributions: tuple[S5BaseDistributionRow, ...]
    history_already_observed: bool = True
    diagnostic_only: bool = True
    executable_pnl: bool = False
    holding_policy_frozen: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if (
            not self.history_already_observed
            or not self.diagnostic_only
            or self.executable_pnl
            or self.holding_policy_frozen
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("diagnostic metrics cannot acquire performance or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_payload(self)),
        )


def compute_s5_base_diagnostic_metrics(
    package: S5BaseDiagnosticInputPackage,
) -> S5BaseDiagnosticMetrics:
    """Compute the preregistered metrics without reading data or choosing a holding period."""

    protocol = frozen_s5_base_diagnostic_protocol()
    _validate_package(
        package,
        protocol.fingerprint,
        protocol.signal_horizons,
        protocol.comparison_ids,
    )

    signals = package.signal_rows
    outcome_by_key = {
        (row.instrument_id, row.as_of, row.horizon): row.close_return
        for row in package.instrument_outcomes
    }
    benchmark_by_key = {
        (row.as_of, row.horizon): row.close_return
        for row in package.benchmark_outcomes
    }
    comparison_by_key = {
        (row.comparison_id, row.as_of, row.horizon): row.close_return
        for row in package.comparison_outcomes
    }
    dates = tuple(sorted({row.as_of for row in signals}))

    state_counts = tuple(
        S5BaseStateCountRow(
            state,
            sum(row.base_state is state for row in signals),
            sum(row.base_state is state for row in signals) / package.population_size,
        )
        for state in S5BaseState
    )
    state_transitions = _state_transitions(signals)
    targets = {row.as_of: row for row in package.target_rows}
    allocations = tuple(
        S5BaseAllocationRow(
            as_of,
            sum(row.selected for row in signals if row.as_of == as_of),
            math.fsum(row.target_weight for row in signals if row.as_of == as_of),
            targets[as_of].cash_weight,
        )
        for as_of in dates
    )

    state_distributions = tuple(
        S5BaseDistributionRow(
            "state",
            state.value,
            "all",
            horizon,
            _stats(
                [
                    outcome_by_key[(row.instrument_id, row.as_of, horizon)]
                    for row in signals
                    if row.base_state is state
                ]
            ),
        )
        for state in S5BaseState
        for horizon in package.horizons
    )

    cohort_spreads = tuple(
        S5BaseSpreadRow(
            spread_id,
            horizon,
            _stats(
                _date_spreads(
                    dates,
                    signals,
                    outcome_by_key,
                    horizon,
                    *_cohort_predicates(spread_id),
                )
            ),
        )
        for spread_id in _COHORT_SPREADS
        for horizon in package.horizons
    )

    comparison_spreads = tuple(
        S5BaseSpreadRow(
            comparison_id,
            horizon,
            _stats(
                _comparison_date_spreads(
                    dates,
                    signals,
                    outcome_by_key,
                    comparison_by_key,
                    comparison_id,
                    horizon,
                )
            ),
        )
        for comparison_id in package.comparison_ids
        for horizon in package.horizons
    )

    months = tuple(sorted({row.as_of.strftime("%Y-%m") for row in signals}))
    monthly_selected = tuple(
        S5BaseDistributionRow(
            "selected_month",
            month,
            month,
            horizon,
            _stats(
                [
                    outcome_by_key[(row.instrument_id, row.as_of, horizon)]
                    for row in signals
                    if row.selected and row.as_of.strftime("%Y-%m") == month
                ]
            ),
        )
        for month in months
        for horizon in package.horizons
    )

    regime_selected = tuple(
        S5BaseDistributionRow(
            "selected_broad_market_regime",
            regime,
            "all",
            horizon,
            _stats(
                [
                    outcome_by_key[(row.instrument_id, row.as_of, horizon)]
                    for row in signals
                    if row.selected
                    and _regime(benchmark_by_key[(row.as_of, horizon)]) == regime
                ]
            ),
        )
        for regime in _REGIMES
        for horizon in package.horizons
    )

    return S5BaseDiagnosticMetrics(
        schema=_SCHEMA,
        protocol_fingerprint=package.protocol_fingerprint,
        input_fingerprint=package.fingerprint,
        label_semantics=package.label_semantics,
        transition_semantics="adjacent_observed_signal_dates_per_instrument",
        spread_weighting="equal_weight_within_date_then_equal_weight_across_dates",
        state_counts=state_counts,
        state_transitions=state_transitions,
        allocations=allocations,
        state_distributions=state_distributions,
        cohort_spreads=cohort_spreads,
        comparison_spreads=comparison_spreads,
        monthly_selected_distributions=monthly_selected,
        regime_selected_distributions=regime_selected,
    )


def _validate_package(
    package: S5BaseDiagnosticInputPackage,
    protocol_fingerprint: str,
    horizons: tuple[int, ...],
    comparison_ids: tuple[str, ...],
) -> None:
    if package.protocol_fingerprint != protocol_fingerprint:
        raise ValueError("input package must bind the frozen S5-B protocol")
    if package.horizons != horizons or package.comparison_ids != comparison_ids:
        raise ValueError("input package grids must equal the frozen protocol")
    if (
        not package.history_already_observed
        or not package.diagnostic_only
        or package.executable_pnl
        or package.holding_policy_frozen
        or package.performance_claim
        or package.broker_order_authority
    ):
        raise ValueError("input package lacks diagnostic-only authority flags")
    if package.label_semantics != frozen_s5_base_diagnostic_protocol().horizon_semantics:
        raise ValueError("input package label semantics changed")
    signal_keys = [(row.instrument_id, row.as_of) for row in package.signal_rows]
    if len(signal_keys) != package.population_size or len(set(signal_keys)) != len(signal_keys):
        raise ValueError("input package signal population is not exact")
    if (
        min(row.as_of for row in package.signal_rows) != package.intended_start
        or max(row.as_of for row in package.signal_rows) != package.intended_end
    ):
        raise ValueError("input package dates do not match intended bounds")
    dates = {row.as_of for row in package.signal_rows}
    target_dates = [row.as_of for row in package.target_rows]
    if len(target_dates) != len(set(target_dates)) or set(target_dates) != dates:
        raise ValueError("input package target dates are not exact")

    instrument_keys = [
        (row.instrument_id, row.as_of, row.horizon)
        for row in package.instrument_outcomes
    ]
    expected_instruments = {
        (instrument_id, as_of, horizon)
        for instrument_id, as_of in signal_keys
        for horizon in horizons
    }
    if (
        len(instrument_keys) != len(set(instrument_keys))
        or set(instrument_keys) != expected_instruments
    ):
        raise ValueError("input package instrument outcome grid is not exact")

    benchmark_keys = [
        (row.as_of, row.horizon) for row in package.benchmark_outcomes
    ]
    expected_date_horizons = {
        (as_of, horizon) for as_of in dates for horizon in horizons
    }
    if (
        len(benchmark_keys) != len(set(benchmark_keys))
        or set(benchmark_keys) != expected_date_horizons
    ):
        raise ValueError("input package benchmark outcome grid is not exact")

    comparison_keys = [
        (row.comparison_id, row.as_of, row.horizon)
        for row in package.comparison_outcomes
    ]
    expected_comparisons = {
        (comparison_id, as_of, horizon)
        for comparison_id in comparison_ids
        for as_of, horizon in expected_date_horizons
    }
    if (
        len(comparison_keys) != len(set(comparison_keys))
        or set(comparison_keys) != expected_comparisons
    ):
        raise ValueError("input package comparison outcome grid is not exact")


def _state_transitions(
    signals: tuple[S5BaseDiagnosticSignalRow, ...],
) -> tuple[S5BaseStateTransitionRow, ...]:
    by_instrument: dict[str, list[S5BaseDiagnosticSignalRow]] = {}
    for row in signals:
        by_instrument.setdefault(row.instrument_id, []).append(row)
    counts: dict[tuple[S5BaseState, S5BaseState], int] = {}
    outgoing: dict[S5BaseState, int] = {}
    for rows in by_instrument.values():
        ordered = sorted(rows, key=lambda row: row.as_of)
        for prior, current in zip(ordered, ordered[1:], strict=False):
            key = (prior.base_state, current.base_state)
            counts[key] = counts.get(key, 0) + 1
            outgoing[prior.base_state] = outgoing.get(prior.base_state, 0) + 1
    order = {state: index for index, state in enumerate(S5BaseState)}
    return tuple(
        S5BaseStateTransitionRow(
            from_state,
            to_state,
            count,
            count / outgoing[from_state],
        )
        for (from_state, to_state), count in sorted(
            counts.items(),
            key=lambda item: (order[item[0][0]], order[item[0][1]]),
        )
    )


def _cohort_predicates(spread_id: str):
    if spread_id == "confirmed_vs_ready":
        return (
            lambda row: row.base_state is S5BaseState.BREAKOUT_CONFIRMED,
            lambda row: row.base_state is S5BaseState.BASE_READY,
        )
    if spread_id == "selected_vs_eligible_nonselected":
        return (
            lambda row: row.selected,
            lambda row: (
                row.admission_state is S5BaseAdmissionState.ELIGIBLE
                and not row.selected
            ),
        )
    raise AssertionError(f"unsupported frozen cohort spread: {spread_id}")


def _date_spreads(
    dates: tuple[date, ...],
    signals: tuple[S5BaseDiagnosticSignalRow, ...],
    outcomes: dict[tuple[str, date, int], float],
    horizon: int,
    left_predicate,
    right_predicate,
) -> list[float]:
    spreads: list[float] = []
    for as_of in dates:
        left = [
            outcomes[(row.instrument_id, row.as_of, horizon)]
            for row in signals
            if row.as_of == as_of and left_predicate(row)
        ]
        right = [
            outcomes[(row.instrument_id, row.as_of, horizon)]
            for row in signals
            if row.as_of == as_of and right_predicate(row)
        ]
        if left and right:
            spreads.append(_mean(left) - _mean(right))
    return spreads


def _comparison_date_spreads(
    dates: tuple[date, ...],
    signals: tuple[S5BaseDiagnosticSignalRow, ...],
    outcomes: dict[tuple[str, date, int], float],
    comparisons: dict[tuple[str, date, int], float],
    comparison_id: str,
    horizon: int,
) -> list[float]:
    spreads: list[float] = []
    for as_of in dates:
        selected = [
            outcomes[(row.instrument_id, row.as_of, horizon)]
            for row in signals
            if row.as_of == as_of and row.selected
        ]
        if selected:
            spreads.append(
                _mean(selected) - comparisons[(comparison_id, as_of, horizon)]
            )
    return spreads


def _stats(values: list[float]) -> S5BaseSummaryStats:
    ordered = sorted(values)
    if not ordered:
        return S5BaseSummaryStats(0, None, None, None, None, None)
    return S5BaseSummaryStats(
        observation_count=len(ordered),
        mean=_mean(ordered),
        median=float(statistics.median(ordered)),
        p10=_linear_quantile(ordered, 0.10),
        minimum=ordered[0],
        strict_positive_rate=sum(value > 0.0 for value in ordered) / len(ordered),
    )


def _linear_quantile(ordered: list[float], quantile: float) -> float:
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values)


def _regime(value: float) -> str:
    if value > 0.0:
        return "positive"
    if value < 0.0:
        return "negative"
    return "flat"


def _payload(metrics: S5BaseDiagnosticMetrics) -> dict[str, object]:
    def convert(value):
        if isinstance(value, date):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        if hasattr(value, "__dataclass_fields__"):
            return {
                name: convert(getattr(value, name))
                for name in value.__dataclass_fields__
                if name != "fingerprint"
            }
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        return value

    return {
        name: convert(getattr(metrics, name))
        for name in metrics.__dataclass_fields__
        if name != "fingerprint"
    }
