"""Synthetic-data tests for the V1 equal-weight control portfolio."""

from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    BacktestConfig,
    LifecycleMonitor,
    run_backtest,
)
from quantlab.backtest.benchmark import compare_benchmark
from quantlab.data.models import Security
from quantlab.portfolio import TargetWeight
from quantlab.portfolio.control import (
    build_equal_weight_control_targets,
    strategy_control_symmetry_audit,
)

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)
D3 = date(2026, 1, 8)
D4 = date(2026, 1, 9)


def _universe(rows) -> pd.DataFrame:
    """rows: (instrument_id, trade_date) — the PIT-filtered V1 cross-section."""
    return pd.DataFrame(rows, columns=["instrument_id", "trade_date"])


def _prices(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": i, "trade_date": d, "adj_close": p}
            for d, values in rows.items()
            for i, p in values.items()
        ]
    )


def _security(instrument_id, delist_date=None) -> Security:
    market = instrument_id.split(".")[1] if "." in instrument_id else "SH"
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id.split(".")[0],
        name="x",
        exchange="SSE" if market == "SH" else "SZSE",
        market=market,
        board="主板",
        list_status="D" if delist_date is not None else "L",
        list_date=date(2000, 1, 1),
        delist_date=delist_date,
    )


def _cfg(bps=10.0) -> BacktestConfig:
    return BacktestConfig(
        initial_nav=1.0, transaction_cost_bps=bps, annualization=252
    )


def _snap(result, d, book="net"):
    return next(b for b in result.books if b.trade_date == d and b.book == book)


def _assert_all_checks_pass(result) -> None:
    assert result.accounting_error is None
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9
        assert check.max_rel < 1e-9


# ------------------------------------------------------------------ targets --


def test_control_targets_pit_equal_weight() -> None:
    universe = _universe(
        [
            ("A", D0), ("B", D0),
            ("A", D3), ("B", D3), ("C", D3),  # C newly eligible on D3
        ]
    )
    targets = build_equal_weight_control_targets(universe, [D0, D3])
    assert set(targets) == {D0, D3}
    first = targets[D0]
    assert first.as_of == D0
    assert first.cash_weight == 0.0
    assert {p.instrument_id for p in first.positions} == {"A", "B"}
    assert all(p.target_weight == pytest.approx(0.5) for p in first.positions)
    second = targets[D3]
    assert {p.instrument_id for p in second.positions} == {"A", "B", "C"}
    assert all(p.target_weight == pytest.approx(1.0 / 3.0) for p in second.positions)


def test_control_targets_empty_cross_section_is_noop() -> None:
    universe = _universe([("A", D0)])
    targets = build_equal_weight_control_targets(universe, [D0, D3])
    assert set(targets) == {D0}


def test_control_targets_match_strategy_signal_dates() -> None:
    # the same signal-date list must yield the same target keys — the control
    # rebalances exactly when the strategy does
    universe = _universe([("A", D0), ("A", D1), ("A", D2)])
    signal_dates = [D0, D2]
    strategy_like = {d: object() for d in signal_dates}
    targets = build_equal_weight_control_targets(universe, signal_dates)
    assert set(targets) == set(strategy_like)


def test_control_targets_reject_missing_columns() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        build_equal_weight_control_targets(pd.DataFrame([{"a": 1}]), [D0])


# ------------------------------------------------------------------- engine --


def _control_run(prices, dates, universe, signal_dates, **kwargs):
    targets = build_equal_weight_control_targets(universe, signal_dates)
    return run_backtest(
        prices, dates, targets, _cfg(),
        execution_lag_sessions=1, mode="strict",
        lifecycle=LifecycleMonitor([], [], mode=DELIST_DATE_IS_FIRST_INVALID_V1),
        requested_period_start=dates[0], requested_period_end=dates[-1],
        **kwargs,
    )


def test_control_no_daily_free_rebalance_and_costs_charged() -> None:
    dates = [D0, D1, D2, D3, D4]
    prices = _prices(
        {
            D0: {"A": 100.0, "B": 100.0},
            D1: {"A": 100.0, "B": 100.0},
            D2: {"A": 120.0, "B": 100.0},  # A drifts: free rebal would trade
            D3: {"A": 120.0, "B": 100.0},
            D4: {"A": 120.0, "B": 100.0},
        }
    )
    universe = _universe([("A", d) for d in dates] + [("B", d) for d in dates])
    signal_dates = [D0, D3]  # weekly cadence
    result = _control_run(prices, dates, universe, signal_dates)

    assert result.status == "completed"
    executions = {t.execution_date for t in result.trades}
    # trades only on the T+1 sessions after a signal; drift inside the week
    # never triggers a trade (no daily free rebalance)
    assert executions == {D1, D4}
    total_net_cost = sum(r.transaction_cost for r in result.records)
    assert total_net_cost > 0.0
    assert _snap(result, D4).nav < _snap(result, D4, "gross").nav
    _assert_all_checks_pass(result)


def test_control_suspended_held_name_freezes_at_stale_mark() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(
        {
            D0: {"A": 100.0, "B": 100.0},
            D1: {"A": 100.0, "B": 100.0},
            D2: {"A": 110.0},  # B suspended: no price row
            D3: {"A": 110.0},
        }
    )
    universe = _universe([("A", d) for d in dates] + [("B", d) for d in dates])
    result = _control_run(prices, dates, universe, [D0])

    assert result.status == "completed"
    b_d2 = next(
        p for p in _snap(result, D2).positions if p.instrument_id == "B"
    )
    b_d3 = next(
        p for p in _snap(result, D3).positions if p.instrument_id == "B"
    )
    # B stays in the book at its stale mark on both sessions
    assert b_d2.value == pytest.approx(b_d3.value)
    assert b_d2.value > 0.0
    _assert_all_checks_pass(result)


def test_control_unavailable_new_target_stays_cash() -> None:
    dates = [D0, D1, D2, D3]
    # B has a target on D0 but no price row until D2: its cash share must NOT
    # be redistributed to A, and B must only be bought once tradable
    prices = _prices(
        {
            D0: {"A": 100.0},
            D1: {"A": 100.0},
            D2: {"A": 100.0, "B": 50.0},
            D3: {"A": 100.0, "B": 50.0},
        }
    )
    universe = _universe([("A", d) for d in dates] + [("B", d) for d in dates])
    result = _control_run(prices, dates, universe, [D0, D2])

    assert result.status == "completed"
    b_d1 = next(
        (p for p in _snap(result, D1).positions if p.instrument_id == "B"), None
    )
    b_d3 = next(
        (p for p in _snap(result, D3).positions if p.instrument_id == "B"), None
    )
    assert b_d1 is None  # not bought while unpriced
    assert b_d3 is not None  # bought once tradable
    a_d1 = next(p for p in _snap(result, D1).positions if p.instrument_id == "A")
    # A never absorbs B's unavailable share beyond its own equal weight
    assert a_d1.value <= 0.5 + 1e-9
    _assert_all_checks_pass(result)


# ---------------------------------------------------------------- symmetry --


def test_symmetry_audit_passes_for_matched_runs() -> None:
    common = {
        "open_dates": [D0, D1],
        "signal_dates": [D0],
        "cost_rate": 0.001,
        "mode": "strict",
        "risk_policy": "exit_after_termination_decision_v1",
        "risk_facts": {"A": []},
        "recovery_rate": 1.0,
        "execution_lag_sessions": 1,
        "missing_price_policy": "freeze_held_no_price",
    }
    checks = strategy_control_symmetry_audit(common, dict(common))
    assert all(checks.values())


def test_symmetry_audit_flags_mismatched_run_spec() -> None:
    strategy = {
        "open_dates": [D0, D1],
        "signal_dates": [D0],
        "cost_rate": 0.001,
        "mode": "strict",
        "risk_policy": "exit_after_termination_decision_v1",
        "risk_facts": {"A": []},
        "recovery_rate": 1.0,
        "execution_lag_sessions": 1,
        "missing_price_policy": "freeze_held_no_price",
    }
    control = dict(strategy, cost_rate=0.0)
    checks = strategy_control_symmetry_audit(strategy, control)
    assert checks["same_cost"] is False
    assert any(value is False for value in checks.values())


# ------------------------------------------------ strategy-vs-control bench --


def test_strategy_vs_control_benchmark_roundtrip() -> None:
    """The formal control feeds compare_benchmark like any other series."""
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0, "B": 100.0} for d in dates})
    universe = _universe([("A", d) for d in dates] + [("B", d) for d in dates])
    control = _control_run(prices, dates, universe, [D0])

    from quantlab.backtest.benchmark import strategy_daily_returns

    series = strategy_daily_returns(control.records)
    stats = compare_benchmark(control.records, "equal_weight_v1_control", series)
    assert stats.n_obs == len(dates) - 1
    # identical series on both sides => beta 1, alpha 0, active return 0
    assert stats.beta == pytest.approx(1.0, abs=1e-12)
    assert stats.alpha_daily == pytest.approx(0.0, abs=1e-12)
    assert stats.cumulative_active_return == pytest.approx(0.0, abs=1e-12)


def test_target_weight_models_available() -> None:
    # sanity: the control module re-uses the shared portfolio models
    assert TargetWeight("A", 1.0).target_weight == 1.0
