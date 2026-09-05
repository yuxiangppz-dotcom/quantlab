from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.portfolio import TargetPortfolio, TargetWeight


def _price_frame(prices_by_date) -> pd.DataFrame:
    rows = []
    for d, prices in prices_by_date.items():
        for instr, p in prices.items():
            rows.append({"instrument_id": instr, "trade_date": d, "adj_close": p})
    return pd.DataFrame(rows)


def _target(as_of, weights, cash=0.0) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=as_of,
        positions=tuple(TargetWeight(i, w) for i, w in weights.items()),
        cash_weight=cash,
    )


def _cfg(bps=10.0, initial_nav=1.0) -> BacktestConfig:
    return BacktestConfig(
        initial_nav=initial_nav, transaction_cost_bps=bps, annualization=252
    )


def _check_max(result, check_name: str) -> float:
    for c in result.accounting_checks:
        if c.check == check_name:
            return c.max_abs
    return 0.0


def _net_cash(result, d) -> float:
    return next(b.cash for b in result.books if b.trade_date == d and b.book == "net")


def test_tiny_weight_change_is_real_trade_not_zero_trade_charged() -> None:
    n = 2000
    ids = [f"A{i}" for i in range(n)]
    base = 1.0 / n
    delta = 9e-10
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {i: 100.0 for i in ids} for d in dates})

    w1 = {i: base for i in ids}
    w2 = {}
    for k, i in enumerate(ids):
        w2[i] = base + delta if k % 2 == 0 else base - delta

    targets = {
        dates[0]: _target(dates[0], w1),
        dates[2]: _target(dates[2], w2),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)

    rb = result.rebalances[1]
    assert rb.nonzero_trade_count == n  # all 2000 legs actually traded
    # fee is exactly cost_rate * actual traded notional
    assert _check_max(result, "fee_consistency") < 1e-15
    assert _check_max(result, "cash_flow") < 1e-15
    # no over-tolerance negative cash
    for b in result.books:
        if b.book == "net":
            assert b.cash > -1e-12
    # fee is not silently waived: expect ~ c * n * delta * nav
    expected_fee = 0.001 * n * delta
    assert rb.transaction_cost > 1e-9  # clearly not waived to zero
    assert rb.transaction_cost == pytest.approx(expected_fee, rel=1e-3)


def test_initial_nav_scaling() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 100.0},
        dates[3]: {"A": 110.0, "B": 100.0},
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5}),
        dates[2]: _target(dates[2], {"B": 1.0}),
    }
    base = None
    for nav in (1e-10, 1e-6, 1.0, 1e6):
        result = run_backtest(prices, dates, targets, _cfg(bps=10.0, initial_nav=nav))
        if base is None:
            base = result
            continue
        for a, b in zip(base.records, result.records, strict=True):
            assert b.nav_net == pytest.approx(a.nav_net * (nav / 1e-10), rel=1e-9)
            assert b.daily_return_net == pytest.approx(a.daily_return_net, rel=1e-9)
            assert b.cash_weight == pytest.approx(a.cash_weight, rel=1e-9)
            assert b.turnover == pytest.approx(a.turnover, rel=1e-9)
        assert result.rebalances[-1].transaction_cost == pytest.approx(
            base.rebalances[-1].transaction_cost * (nav / 1e-10), rel=1e-9
        )


def test_small_buy_not_fee_waived() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 5e-7}, cash=1.0 - 5e-7)}
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rb = result.rebalances[0]
    assert rb.transaction_cost == pytest.approx(0.001 * 5e-7, rel=1e-6)
    assert rb.nonzero_trade_count == 1


def test_fee_exactly_matches_final_traded_value() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"B": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    for rb in result.rebalances:
        signed_sum = sum(
            abs(t.signed_trade_value)
            for t in result.trades
            if t.execution_date == rb.execution_date and t.book == "net"
        )
        assert rb.transaction_cost == pytest.approx(0.001 * signed_sum, rel=1e-12)
    assert _check_max(result, "fee_consistency") < 1e-15


def test_all_cash_all_frozen_zero_cost() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},
        dates[3]: {},
    })
    # all cash -> no trade
    result = run_backtest(prices, dates, {}, _cfg(bps=10.0))
    assert result.rebalances == []
    for b in result.books:
        assert abs(b.nav - (b.cash + sum(p.value for p in b.positions))) < 1e-12

    # frozen -> no trade, no cost
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"A": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert result.rebalances[1].nonzero_trade_count == 0
    assert result.rebalances[1].transaction_cost == pytest.approx(0.0, abs=1e-15)
    assert _check_max(result, "frozen_invariance") < 1e-15


def test_root_non_convergence_raises(monkeypatch) -> None:
    from quantlab.backtest import engine as engine_module

    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    monkeypatch.setattr(engine_module, "_ROOT_MAX_ITER", 1)
    with pytest.raises(ValueError):
        run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)


def test_daily_reconciliation() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 100.0},
        dates[3]: {"A": 110.0, "B": 100.0},
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5}),
        dates[2]: _target(dates[2], {"B": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9, f"{check.check} residual too large: {check.max_abs}"
    daily_cost = sum(r.transaction_cost for r in result.records)
    rebalance_cost = sum(rb.transaction_cost for rb in result.rebalances)
    assert daily_cost == pytest.approx(rebalance_cost, abs=1e-12)
    # per-instrument signed trade values reconcile with buy/sell notional
    for rb in result.rebalances:
        net_trades = [
            t for t in result.trades
            if t.execution_date == rb.execution_date and t.book == "net"
        ]
        gross_buy = sum(max(t.signed_trade_value, 0.0) for t in net_trades)
        gross_sell = sum(max(-t.signed_trade_value, 0.0) for t in net_trades)
        # transaction_cost == cost_rate * (buy + sell)
        assert rb.transaction_cost == pytest.approx(0.001 * (gross_buy + gross_sell), rel=1e-9)
