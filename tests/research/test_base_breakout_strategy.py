from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.base_breakout_strategy import (
    BaseBreakoutStrategyConfig,
    build_base_breakout_signals,
    summarize_base_breakout_signals,
)
from quantlab.research.s5_base_completion import S5BaseState
from quantlab.research.s5_base_materialization import (
    S5BaseSeriesInput,
    materialize_s5_base_inputs,
)
from quantlab.research.s5_materialization import S5ResearchSeries, S5SeriesPoint


def _sessions(count: int = 140) -> tuple[date, ...]:
    start = date(2026, 1, 1)
    return tuple(start + timedelta(days=index) for index in range(count))


def _frame(
    sessions: tuple[date, ...],
    prices: list[float],
    *,
    instrument_id: str = "000001.SZ",
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "instrument_id": instrument_id,
            "trade_date": sessions,
            "adj_close": prices,
            "volume": volumes or [100.0] * len(sessions),
        }
    )


def _ready_prices(count: int = 140) -> list[float]:
    prices = [130.0 - index * 0.35 for index in range(100)]
    middle = [95.0 if index % 2 == 0 else 96.5 for index in range(30)]
    recovery = [93.0, 95.2, 95.5, 96.0, 96.3, 96.6, 97.0, 97.3, 97.7, 98.0]
    prices += (middle + recovery)[: count - 100]
    return prices


def test_complete_strategy_emits_ready_candidate_and_residual_cash() -> None:
    sessions = _sessions()
    result = build_base_breakout_signals(
        _frame(sessions, _ready_prices()),
        sessions,
        signal_dates=(sessions[-1],),
    )

    assert len(result) == 1
    assert result.loc[0, "state"] in {
        S5BaseState.BASE_READY.value,
        S5BaseState.BREAKOUT_CONFIRMED.value,
    }
    assert bool(result.loc[0, "selected"]) is True
    assert result.loc[0, "target_weight"] == pytest.approx(0.04)
    assert result.loc[0, "cash_weight"] == pytest.approx(0.96)
    assert bool(result.loc[0, "research_only"]) is True
    assert bool(result.loc[0, "broker_order_authority"]) is False


def test_shallow_new_low_is_not_automatic_failure() -> None:
    sessions = _sessions(120)
    prices = [130.0] * 90
    prices += [110.0 - index * (10.0 / 19.0) for index in range(20)]
    prices += [100.4, 100.3, 100.2, 100.1, 100.05, 100.0, 99.9, 99.8, 99.7, 99.6]
    result = build_base_breakout_signals(
        _frame(sessions, prices), sessions, signal_dates=(sessions[-1],)
    )

    assert result.loc[0, "new_low_count_10"] > 0
    assert result.loc[0, "max_new_low_undercut_10"] < 0.03
    assert result.loc[0, "state"] == S5BaseState.BASE_BUILDING.value


def test_vectorized_features_match_frozen_materializer() -> None:
    sessions = _sessions()
    prices = _ready_prices()
    volumes = [100.0] * 135 + [200.0] * 5
    result = build_base_breakout_signals(
        _frame(sessions, prices, volumes=volumes),
        sessions,
        signal_dates=(sessions[-1],),
    ).iloc[0]
    entity = S5BaseSeriesInput(
        entity_id="000001.SZ",
        closes=S5ResearchSeries(
            "000001.SZ",
            "fixture",
            tuple(S5SeriesPoint(day, value) for day, value in zip(sessions, prices, strict=True)),
        ),
        volumes=S5ResearchSeries(
            "000001.SZ",
            "fixture",
            tuple(S5SeriesPoint(day, value) for day, value in zip(sessions, volumes, strict=True)),
        ),
    )
    reference = materialize_s5_base_inputs(
        as_of=sessions[-1], sessions=sessions, entities=(entity,)
    ).observations[0]

    for field in reference.__dataclass_fields__:
        if field in {"entity_id", "as_of"}:
            continue
        expected = getattr(reference, field)
        actual = result[field]
        if isinstance(expected, float):
            assert actual == pytest.approx(expected)
        else:
            assert actual == expected


def test_deep_recent_undercut_fails() -> None:
    sessions = _sessions(120)
    prices = [130.0] * 90 + [100.0 if index % 2 == 0 else 102.0 for index in range(29)] + [90.0]
    result = build_base_breakout_signals(
        _frame(sessions, prices), sessions, signal_dates=(sessions[-1],)
    )

    assert result.loc[0, "state"] == S5BaseState.FAILED.value
    assert "new_low_undercut_too_deep" in result.loc[0, "reasons"]


def test_missing_market_session_does_not_compress_history() -> None:
    sessions = _sessions(120)
    frame = _frame(sessions, _ready_prices(120)).drop(index=50)
    result = build_base_breakout_signals(
        frame, sessions, signal_dates=(sessions[-1],)
    )

    assert result.loc[0, "state"] == S5BaseState.UNKNOWN.value
    assert bool(result.loc[0, "selected"]) is False


def test_confirmed_ranks_before_ready_and_budget_is_enforced() -> None:
    sessions = _sessions()
    ready = _frame(sessions, _ready_prices(), instrument_id="000001.SZ")
    confirmed = _frame(
        sessions,
        _ready_prices(),
        instrument_id="000002.SZ",
        volumes=[100.0] * 135 + [200.0] * 5,
    )
    result = build_base_breakout_signals(
        pd.concat([ready, confirmed], ignore_index=True),
        sessions,
        signal_dates=(sessions[-1],),
        config=BaseBreakoutStrategyConfig(max_names=1, weight_per_name=0.8),
    )

    selected = result[result["selected"]]
    assert selected["instrument_id"].tolist() == ["000002.SZ"]
    assert selected["state"].tolist() == [S5BaseState.BREAKOUT_CONFIRMED.value]
    assert selected["target_weight"].tolist() == [pytest.approx(0.8)]
    assert selected["cash_weight"].tolist() == [pytest.approx(0.2)]


def test_future_labels_cannot_change_signals_or_targets() -> None:
    sessions = _sessions()
    base = _frame(sessions, _ready_prices())
    first = base.assign(future_return_1d=999.0)
    second = base.assign(future_return_1d=-999.0)
    left = build_base_breakout_signals(first, sessions, signal_dates=(sessions[-1],))
    right = build_base_breakout_signals(second, sessions, signal_dates=(sessions[-1],))
    pd.testing.assert_frame_equal(left, right)


def test_one_session_diagnostic_is_supported_but_not_pnl() -> None:
    sessions = _sessions()
    source = _frame(sessions, _ready_prices())
    signals = build_base_breakout_signals(
        source, sessions, signal_dates=(sessions[-1],)
    )
    labels = source[["instrument_id", "trade_date"]].copy()
    labels["future_return_1d"] = 0.02
    rows = summarize_base_breakout_signals(signals, labels, horizons=(1,))

    selected = next(row for row in rows if row["population"] == "selected")
    assert selected["horizon_sessions"] == 1
    assert selected["mean_close_return"] == pytest.approx(0.02)
    assert selected["executable_pnl"] is False
    assert selected["performance_claim"] is False


def test_invalid_input_and_overallocation_fail_closed() -> None:
    sessions = _sessions()
    duplicate = pd.concat(
        [_frame(sessions, _ready_prices()), _frame(sessions, _ready_prices())],
        ignore_index=True,
    )
    with pytest.raises(DataValidationError, match="duplicate"):
        build_base_breakout_signals(duplicate, sessions)
    with pytest.raises(ValueError, match="cannot exceed"):
        BaseBreakoutStrategyConfig(max_names=3, weight_per_name=0.34)
