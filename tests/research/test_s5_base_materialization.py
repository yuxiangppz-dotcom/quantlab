from dataclasses import replace
from datetime import date, timedelta

import pytest

from quantlab.research.s5_base_completion import S5BaseState, evaluate_s5_base
from quantlab.research.s5_base_materialization import (
    S5BaseSeriesInput,
    materialize_s5_base_inputs,
)
from quantlab.research.s5_materialization import (
    S5ResearchSeries,
    S5SeriesPoint,
)


def _sessions(count: int = 120) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _series(
    series_id: str,
    sessions: tuple[date, ...],
    values: list[float],
    *,
    source_id: str = "fixture-v1",
) -> S5ResearchSeries:
    return S5ResearchSeries(
        series_id=series_id,
        source_id=source_id,
        points=tuple(
            S5SeriesPoint(trade_date=trade_date, value=value)
            for trade_date, value in zip(sessions, values, strict=True)
        ),
    )


def _input(
    prices: list[float],
    *,
    entity_id: str = "000001.SZ",
    volumes: list[float] | None = None,
    source_id: str = "fixture-v1",
) -> tuple[tuple[date, ...], S5BaseSeriesInput]:
    sessions = _sessions(len(prices))
    actual_volumes = volumes or [100.0] * len(prices)
    return sessions, S5BaseSeriesInput(
        entity_id=entity_id,
        closes=_series(entity_id, sessions, prices, source_id=source_id),
        volumes=_series(entity_id, sessions, actual_volumes, source_id=source_id),
    )


def test_materializes_shallow_new_low_and_fast_reclaim() -> None:
    prices = [100.0] * 120
    prices[109] = 90.0
    prices[112] = 89.0
    prices[113:] = [91.0, 92.0, 92.5, 93.0, 94.0, 94.5, 95.0]
    sessions, entity = _input(prices, volumes=[100.0] * 115 + [200.0] * 5)

    result = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    observation = result.observations[0]

    assert result.issues == ()
    assert observation.new_low_count_10 == 1
    assert observation.new_low_count_20 == 2
    assert observation.max_new_low_undercut_10 == pytest.approx(1.0 - 89.0 / 90.0)
    assert observation.prior_low_reclaimed is True
    assert observation.reclaim_sessions == 1
    assert observation.recovery_from_10d_low == pytest.approx(95.0 / 89.0 - 1.0)
    assert observation.volume_ratio_5_to_20 == pytest.approx(1.6)


def test_recent_new_low_without_reclaim_is_building_not_failed() -> None:
    prices = [130.0] * 90
    prices += [110.0 - index * (10.0 / 19.0) for index in range(20)]
    prices += [100.4, 100.3, 100.2, 100.1, 100.05, 100.0, 99.9, 99.8, 99.7, 99.6]
    sessions, entity = _input(prices)
    result = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    observation = result.observations[0]
    assert observation.prior_low_reclaimed is False
    assert observation.reclaim_sessions == 0
    assert evaluate_s5_base(observation).state is S5BaseState.BASE_BUILDING


def test_breakout_reference_excludes_current_close() -> None:
    prices = [80.0 + index * 0.1 for index in range(119)] + [110.0]
    sessions, entity = _input(prices)
    observation = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    ).observations[0]
    assert observation.breakout_distance_20 == pytest.approx(110.0 / prices[-2] - 1.0)
    assert observation.close_location_20 == 1.0


def test_max_drawdown_uses_peak_before_later_trough() -> None:
    prices = [80.0 + index * 0.1 for index in range(110)]
    prices += [100.0, 110.0, 105.0, 90.0, 92.0, 94.0, 96.0, 98.0, 99.0, 100.0]
    sessions, entity = _input(prices)
    observation = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    ).observations[0]
    assert observation.max_drawdown_10 == pytest.approx(1.0 - 90.0 / 110.0)


def test_flat_range_is_explicit_midpoint_but_zero_volatility_is_unknown() -> None:
    sessions, entity = _input([100.0] * 120)
    result = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    observation = result.observations[0]
    assert observation.close_location_20 == 0.5
    assert observation.volatility_ratio_10_to_prior_20 is None
    assert result.issues[-1].reason == "zero_prior_volatility"
    assert evaluate_s5_base(observation).state is S5BaseState.UNKNOWN


def test_zero_volume_denominator_stays_unknown() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, entity = _input(prices, volumes=[0.0] * 120)
    result = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    assert result.observations[0].volume_ratio_5_to_20 is None
    assert any(issue.reason == "zero_denominator" for issue in result.issues)


def test_missing_close_emits_field_issues_and_unknown_observation() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, entity = _input(prices)
    entity = replace(
        entity,
        closes=replace(entity.closes, points=entity.closes.points[1:]),
    )
    result = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    assert len(result.issues) == 14
    assert result.observations[0].history_complete is False
    assert evaluate_s5_base(result.observations[0]).state is S5BaseState.UNKNOWN


def test_future_points_do_not_change_values_issues_or_fingerprint() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, entity = _input(prices)
    baseline = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    future = S5SeriesPoint(sessions[-1] + timedelta(days=1), -999.0)
    changed = materialize_s5_base_inputs(
        as_of=sessions[-1],
        sessions=sessions,
        entities=(
            replace(
                entity,
                closes=replace(entity.closes, points=entity.closes.points + (future,)),
            ),
        ),
    )
    assert changed.observations == baseline.observations
    assert changed.issues == baseline.issues
    assert changed.fingerprint == baseline.fingerprint


def test_entity_order_does_not_change_output_or_fingerprint() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, first = _input(prices, entity_id="000002.SZ")
    _, second = _input(prices, entity_id="000001.SZ")
    left = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(first, second)
    )
    right = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(second, first)
    )
    assert left == right


def test_source_identity_changes_fingerprint() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, entity = _input(prices)
    first = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    )
    changed_entity = replace(
        entity,
        closes=replace(entity.closes, source_id="fixture-v2"),
    )
    second = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(changed_entity,)
    )
    assert first.fingerprint != second.fingerprint


def test_rejects_bad_sessions_duplicate_entity_and_duplicate_date() -> None:
    prices = [80.0 + index * 0.1 for index in range(120)]
    sessions, entity = _input(prices)
    with pytest.raises(ValueError, match="entities cannot be empty"):
        materialize_s5_base_inputs(as_of=sessions[-1], sessions=sessions, entities=())
    with pytest.raises(ValueError, match="end exactly"):
        materialize_s5_base_inputs(
            as_of=sessions[-2], sessions=sessions, entities=(entity,)
        )
    with pytest.raises(ValueError, match="duplicate entity_id"):
        materialize_s5_base_inputs(
            as_of=sessions[-1], sessions=sessions, entities=(entity, entity)
        )
    duplicate = replace(
        entity,
        closes=replace(
            entity.closes, points=entity.closes.points + (entity.closes.points[-1],)
        ),
    )
    with pytest.raises(ValueError, match="duplicate series date"):
        materialize_s5_base_inputs(
            as_of=sessions[-1], sessions=sessions, entities=(duplicate,)
        )
