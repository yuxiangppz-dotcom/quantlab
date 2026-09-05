from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, run_backtest, weekly_signal_dates
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


def _cfg(bps=0.0) -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=bps, annualization=252)


def _record(records, d):
    return next(r for r in records if r.trade_date == d)


def test_weekly_signal_dates() -> None:
    dates = [
        date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
        date(2026, 1, 8), date(2026, 1, 9),  # week 1
        date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14),
        date(2026, 1, 15), date(2026, 1, 16),  # week 2
    ]
    result = weekly_signal_dates(dates)
    assert result == [date(2026, 1, 9), date(2026, 1, 16)]


def test_signal_does_not_affect_t_and_t1() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 101.0},
        dates[2]: {"A": 102.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, log = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert log[0].execution_date == dates[1]
    assert _record(records, dates[0]).daily_return_net == 0.0
    assert _record(records, dates[1]).daily_return_net == 0.0
    d2_ret = _record(records, dates[2]).daily_return_net
    assert abs(d2_ret - (102 / 101 - 1)) < 1e-9


def test_two_stock_pnl_hand_calc() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 120.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert abs(_record(records, dates[2]).nav_net - 1.15) < 1e-9


def test_turnover_hand_calc() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"B": 1.0}),
    }
    _, log = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    assert log[0].execution_date == dates[1]
    assert abs(log[0].traded_notional_ratio - 1.0) < 1e-9
    assert abs(log[0].turnover - 0.5) < 1e-9
    assert log[1].execution_date == dates[3]
    assert abs(log[1].traded_notional_ratio - 2.0) < 1e-9
    assert abs(log[1].turnover - 1.0) < 1e-9


def test_gross_nav_unaffected_by_cost_net_affected() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rec = _record(records, dates[1])
    assert abs(rec.nav_gross - 1.0) < 1e-9
    assert abs(rec.nav_net - 0.999) < 1e-9


def test_cost_persistence_never_reenters_gross() -> None:
    # flat prices: gross NAV must stay exactly 1.0 through successive costs
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),  # cash -> A
        dates[2]: _target(dates[2], {"B": 1.0}),  # A -> B
    }
    records, _ = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert abs(_record(records, dates[1]).nav_gross - 1.0) < 1e-9
    assert abs(_record(records, dates[2]).nav_gross - 1.0) < 1e-9
    assert abs(_record(records, dates[3]).nav_gross - 1.0) < 1e-9
    # net monotonically decreases
    assert _record(records, dates[1]).nav_net < 1.0
    assert _record(records, dates[3]).nav_net < _record(records, dates[1]).nav_net


def test_multi_rebalance_hand_calc() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),  # traded ratio 1.0
        dates[2]: _target(dates[2], {"B": 1.0}),  # traded ratio 2.0
    }
    records, log = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert abs(log[0].traded_notional_ratio - 1.0) < 1e-9
    assert abs(log[1].traded_notional_ratio - 2.0) < 1e-9
    # rebalance 1: net = 1.0 * (1 - 0.001) = 0.999
    assert abs(_record(records, dates[1]).nav_net - 0.999) < 1e-9
    # rebalance 2: net = 0.999 * (1 - 0.002) = 0.997002
    assert abs(_record(records, dates[3]).nav_net - 0.997002) < 1e-9
    assert abs(_record(records, dates[3]).nav_gross - 1.0) < 1e-9


def test_target_weight_after_cost_no_negative_cash() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rec = _record(records, dates[1])
    assert rec.gross_exposure == pytest.approx(1.0)
    assert rec.cash_weight == pytest.approx(0.0)
    assert rec.gross_exposure <= 1.0 + 1e-9
    assert rec.cash_weight >= -1e-9


def test_unavailable_target_no_cost() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {},  # A suspended at execution -> no bar
        dates[2]: {"A": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, log = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert log[0].unavailable_target_count == 1
    assert log[0].filled_target_count == 0
    assert log[0].traded_notional_ratio == 0.0
    assert log[0].transaction_cost == 0.0
    assert _record(records, dates[1]).cash_weight == pytest.approx(1.0)
    assert abs(_record(records, dates[2]).nav_net - 1.0) < 1e-9


def test_suspension_resume_uses_last_available_price() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},  # suspended
        dates[3]: {"A": 110.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert _record(records, dates[2]).daily_return_net == 0.0
    resume = _record(records, dates[3])
    assert abs(resume.daily_return_net - 0.10) < 1e-9
    assert abs(resume.nav_net - 1.10) < 1e-9


def test_no_rebalance_zero_turnover() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert _record(records, dates[1]).turnover > 0
    assert _record(records, dates[2]).turnover == 0.0


def test_all_cash_flat_nav() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    records, _ = run_backtest(prices, dates, {}, _cfg(), execution_lag_sessions=1)
    assert all(abs(r.nav_net - 1.0) < 1e-9 for r in records)


def test_future_return_ignored() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    base = {
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {"A": 110.0},
    }
    p1 = _price_frame(base).copy()
    p1["future_return_5d"] = [0.0, 0.0, 0.0]
    p2 = _price_frame(base).copy()
    p2["future_return_5d"] = [999.0, -999.0, 123.0]
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    r1, _ = run_backtest(p1, dates, targets, _cfg())
    r2, _ = run_backtest(p2, dates, targets, _cfg())
    assert [r.nav_net for r in r1] == [r.nav_net for r in r2]


def test_row_order_independent() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 120.0},
    })
    shuffled = prices.sample(frac=1, random_state=0).reset_index(drop=True)
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    r1, _ = run_backtest(prices, dates, targets, _cfg())
    r2, _ = run_backtest(shuffled, dates, targets, _cfg())
    assert [r.nav_net for r in r1] == [r.nav_net for r in r2]


def test_portfolio_drift_between_rebalances() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 100.0},  # A up, B flat
        dates[3]: {"A": 110.0, "B": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    rec = _record(records, dates[2])
    assert rec.gross_exposure > 0.999
    assert rec.net_exposure > 0.999
    # drift realized as PnL: 0.5 * 10% = 5%
    assert abs(rec.nav_net - 1.05) < 1e-9


def test_period_boundary_records_cover_full_window() -> None:
    # signal on the last session -> T+1 outside window -> never executed
    dates = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[-1]: _target(dates[-1], {"A": 1.0})}
    records, log = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert [r.trade_date for r in records] == dates
    assert log == []
    assert all(r.cash_weight == pytest.approx(1.0) for r in records)


def test_period_boundary_signal_executes_within_window() -> None:
    dates = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, log = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert [r.trade_date for r in records] == dates
    assert log[0].execution_date == dates[1]
