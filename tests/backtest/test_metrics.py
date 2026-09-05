from datetime import date, timedelta

import pytest

from quantlab.backtest import (
    BacktestConfig,
    DailyBacktestRecord,
    RebalanceRecord,
    compute_metrics,
)


def _rec(
    d,
    nav_gross,
    nav_net,
    ret_gross=0.0,
    ret_net=0.0,
    cost=0.0,
    turnover=0.0,
    gross_exposure=1.0,
    cash_weight=0.0,
    holdings=1,
):
    return DailyBacktestRecord(
        trade_date=d,
        nav_gross=nav_gross,
        nav_net=nav_net,
        daily_return_gross=ret_gross,
        daily_return_net=ret_net,
        gross_exposure=gross_exposure,
        net_exposure=gross_exposure,
        cash_weight=cash_weight,
        turnover=turnover,
        traded_notional_ratio=turnover * 2,
        transaction_cost=cost,
        holdings_count=holdings,
        gross_book_gross_exposure=gross_exposure,
        gross_book_net_exposure=gross_exposure,
        gross_book_cash_weight=cash_weight,
        gross_book_turnover=turnover,
        gross_book_traded_notional_ratio=turnover * 2,
        gross_book_holdings_count=holdings,
    )


def _rebalance(d, cost=0.0, unavailable=0, turnover=0.5):
    return RebalanceRecord(
        signal_date=d,
        execution_date=d,
        target_count=1,
        nonzero_trade_count=1,
        unavailable_target_count=unavailable,
        frozen_count=0,
        restricted_binding_count=0,
        buy_notional_ratio=turnover,
        sell_notional_ratio=turnover,
        traded_notional_ratio=turnover * 2,
        turnover=turnover,
        transaction_cost=cost,
        pre_trade_gross_exposure=0.0,
        post_trade_gross_exposure=1.0,
        allocation_deviation=0.0,
        gross_book_buy_notional_ratio=turnover,
        gross_book_sell_notional_ratio=turnover,
        gross_book_traded_notional_ratio=turnover * 2,
        gross_book_turnover=turnover,
        gross_book_transaction_cost=0.0,
        gross_book_pre_trade_gross_exposure=0.0,
        gross_book_post_trade_gross_exposure=1.0,
        gross_book_allocation_deviation=0.0,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def test_max_drawdown() -> None:
    start = date(2026, 1, 5)
    navs = [1.0, 1.1, 0.99, 1.05]
    records = []
    prev = 1.0
    for i, nav in enumerate(navs):
        ret = nav / prev - 1 if i > 0 else 0.0
        records.append(_rec(start + timedelta(days=i), nav, nav, ret, ret))
        prev = nav
    m = compute_metrics(records, [], _config())
    assert m["max_drawdown_net"] == pytest.approx(0.99 / 1.1 - 1)


def test_cagr() -> None:
    start = date(2026, 1, 5)
    n_records = 253  # 252 return intervals
    records = []
    for i in range(n_records):
        nav = 1.0 + 0.21 * i / 252
        prev = 1.0 + 0.21 * (i - 1) / 252 if i > 0 else 1.0
        ret = nav / prev - 1 if i > 0 else 0.0
        records.append(_rec(start + timedelta(days=i), nav, nav, ret, ret))
    m = compute_metrics(records, [], _config())
    assert m["total_return_net"] == pytest.approx(0.21, abs=1e-6)
    assert m["cagr_net"] == pytest.approx(0.21, abs=1e-4)
    assert m["n_records"] == 253
    assert m["n_return_intervals"] == 252


def test_sharpe_positive() -> None:
    start = date(2026, 1, 5)
    records = []
    nav = 1.0
    for i in range(252):
        ret = 0.0 if i == 0 else 0.001 + 0.0002 * (1 if i % 2 == 0 else -1)
        nav *= 1 + ret
        records.append(_rec(start + timedelta(days=i), nav, nav, ret, ret))
    m = compute_metrics(records, [], _config())
    assert m["sharpe_net"] > 1


def test_vol_and_sharpe_zero() -> None:
    start = date(2026, 1, 5)
    records = [_rec(start + timedelta(days=i), 1.0, 1.0) for i in range(100)]
    m = compute_metrics(records, [], _config())
    assert m["annualized_volatility_net"] == pytest.approx(0.0, abs=1e-12)
    assert m["sharpe_net"] != m["sharpe_net"]  # NaN when variance is 0


def test_turnover_metrics() -> None:
    start = date(2026, 1, 5)
    records = [
        _rec(start + timedelta(days=i), 1.0, 1.0, turnover=0.5) for i in range(10)
    ]
    rebalances = [_rebalance(start + timedelta(days=i)) for i in range(5)]
    m = compute_metrics(records, rebalances, _config())
    assert m["total_turnover"] == pytest.approx(5.0)
    assert m["average_daily_turnover"] == pytest.approx(5.0 / 9)
    assert m["average_rebalance_turnover"] == pytest.approx(5.0 / 5)
    assert m["annualized_turnover"] == pytest.approx(5.0 / (9 / 252))
    assert m["gross_book_total_turnover"] == pytest.approx(5.0)
    assert m["gross_book_annualized_turnover"] == pytest.approx(5.0 / (9 / 252))


def test_cost_metrics_and_drag() -> None:
    start = date(2026, 1, 5)
    records = [
        _rec(start + timedelta(days=0), 1.0, 1.0, cost=0.0),
        _rec(start + timedelta(days=1), 1.0, 0.999, ret_net=-0.001, cost=0.001),
        _rec(start + timedelta(days=2), 1.0, 0.997002, ret_net=-0.002, cost=0.001998),
    ]
    rebalances = [
        _rebalance(start + timedelta(days=1), cost=0.001),
        _rebalance(start + timedelta(days=2), cost=0.001998),
    ]
    m = compute_metrics(records, rebalances, _config())
    assert m["total_transaction_cost"] == pytest.approx(0.002998)
    assert m["cumulative_cost_paid_vs_initial_nav"] == pytest.approx(0.002998)
    assert m["total_return_gross"] == pytest.approx(0.0)
    assert m["total_return_net"] == pytest.approx(0.997002 - 1.0)
    assert m["terminal_return_cost_drag"] == pytest.approx(0.002998)
    assert m["cagr_cost_drag"] > 0


def test_return_interval_uses_n_minus_1() -> None:
    start = date(2026, 1, 5)
    records = [
        _rec(start + timedelta(days=0), 1.0, 1.0),
        _rec(start + timedelta(days=1), 1.1, 1.1, 0.1, 0.1),
        _rec(start + timedelta(days=2), 1.21, 1.21, 0.1, 0.1),
    ]
    m = compute_metrics(records, [], _config())
    assert m["cagr_gross"] == pytest.approx(1.21 ** (252 / 2) - 1)
    assert m["annualized_volatility_gross"] == pytest.approx(0.0, abs=1e-12)


def test_single_record_undefined_statistics() -> None:
    start = date(2026, 1, 5)
    records = [_rec(start, 1.0, 1.0)]
    m = compute_metrics(records, [], _config())
    assert m["n_return_intervals"] == 0
    assert m["cagr_net"] != m["cagr_net"]  # NaN
    assert m["annualized_volatility_net"] != m["annualized_volatility_net"]  # NaN
    assert m["sharpe_net"] != m["sharpe_net"]  # NaN


def test_consistent_records_reconcile() -> None:
    # internally consistent: first record no trade, returns match nav ratios
    start = date(2026, 1, 5)
    navs_net = [1.0, 1.02, 1.05]
    navs_gross = [1.0, 1.02, 1.05]
    records = []
    for i in range(3):
        ret_g = navs_gross[i] / navs_gross[i - 1] - 1 if i > 0 else 0.0
        ret_n = navs_net[i] / navs_net[i - 1] - 1 if i > 0 else 0.0
        records.append(
            _rec(
                start + timedelta(days=i),
                navs_gross[i],
                navs_net[i],
                ret_g,
                ret_n,
                cost=0.0 if i == 0 else 0.001,
                turnover=0.0 if i == 0 else 0.5,
            )
        )
    m = compute_metrics(records, [], _config())
    assert m["total_return_net"] == pytest.approx(1.05 / 1.0 - 1)
    assert m["total_return_gross"] == pytest.approx(1.05 / 1.0 - 1)
    assert m["total_transaction_cost"] == pytest.approx(0.002)
    assert m["n_return_intervals"] == 2
