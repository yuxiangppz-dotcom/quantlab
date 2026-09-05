from datetime import date, timedelta

import pytest

from quantlab.backtest import BacktestConfig, DailyBacktestRecord, compute_metrics


def _rec(d, nav, ret, cost=0.0, turnover=0.0) -> DailyBacktestRecord:
    return DailyBacktestRecord(
        trade_date=d,
        nav_gross=nav,
        nav_net=nav - cost,
        daily_return_gross=ret,
        daily_return_net=ret,
        gross_exposure=1.0,
        net_exposure=1.0,
        cash_weight=0.0,
        turnover=turnover,
        traded_notional_ratio=turnover * 2,
        transaction_cost=cost,
        holdings_count=1,
    )


def _config() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def test_max_drawdown() -> None:
    start = date(2026, 1, 5)
    navs = [1.0, 1.1, 0.99, 1.05]
    records = []
    prev = 1.0
    for i, nav in enumerate(navs):
        records.append(_rec(start + timedelta(days=i), nav, nav / prev - 1))
        prev = nav
    m = compute_metrics(records, [], _config())
    assert m["max_drawdown_net"] == pytest.approx(0.99 / 1.1 - 1)


def test_cagr() -> None:
    start = date(2026, 1, 5)
    # 252 天，nav 从 1.0 线性到 1.21
    records = []
    for i in range(252):
        nav = 1.0 + 0.21 * i / 251
        ret = (1.0 + 0.21 * i / 251) / (1.0 + 0.21 * (i - 1) / 251) - 1 if i > 0 else 0.0
        records.append(_rec(start + timedelta(days=i), nav, ret))
    m = compute_metrics(records, [], _config())
    assert m["total_return_net"] == pytest.approx(0.21, abs=1e-6)
    assert m["cagr_net"] == pytest.approx(0.21, abs=1e-4)


def test_sharpe_positive() -> None:
    start = date(2026, 1, 5)
    records = []
    nav = 1.0
    for i in range(252):
        ret = 0.001 + 0.0002 * (1 if i % 2 == 0 else -1)
        nav *= 1 + ret
        records.append(_rec(start + timedelta(days=i), nav, ret))
    m = compute_metrics(records, [], _config())
    assert m["sharpe_net"] > 1


def test_vol_and_sharpe_zero() -> None:
    start = date(2026, 1, 5)
    records = [_rec(start + timedelta(days=i), 1.0, 0.0) for i in range(100)]
    m = compute_metrics(records, [], _config())
    assert m["annualized_volatility_net"] == pytest.approx(0.0, abs=1e-12)


def test_cost_metrics() -> None:
    start = date(2026, 1, 5)
    records = [
        _rec(start + timedelta(days=i), 1.0, 0.0, cost=0.001, turnover=0.5)
        for i in range(10)
    ]
    m = compute_metrics(records, [], _config())
    assert m["total_transaction_cost"] == pytest.approx(0.01)
    assert m["cost_drag"] == pytest.approx(0.01)
    assert m["average_turnover"] == pytest.approx(0.5)
