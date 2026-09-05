from datetime import date

import pandas as pd

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


def _nav(records, d):
    return next(r.nav_net for r in records if r.trade_date == d)


def _days(n, start=date(2026, 1, 5)):
    return [start + pd.Timedelta(days=i).to_pytimedelta().date() for i in range(n)]


def test_weekly_signal_dates() -> None:
    # 两周：1/5(Mon)..1/9(Fri), 1/12(Mon)..1/16(Fri)
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
    # execution 在 D1（不是 D0）
    assert log[0].execution_date == dates[1]
    # D0 和 D1 return 都是 0（新组合从 D1 close 后承担收益）
    assert next(r.daily_return_net for r in records if r.trade_date == dates[0]) == 0.0
    assert next(r.daily_return_net for r in records if r.trade_date == dates[1]) == 0.0
    # D2 承担收益 102/101 - 1
    d2_ret = next(r.daily_return_net for r in records if r.trade_date == dates[2])
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
    # D2: 0.5*10% + 0.5*20% = 15% -> nav 1.15
    assert abs(_nav(records, dates[2]) - 1.15) < 1e-9


def test_turnover_and_cost_hand_calc() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        d: {"A": 100.0, "B": 100.0} for d in dates
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),  # 建仓 A
        dates[2]: _target(dates[2], {"B": 1.0}),  # 换仓 B
    }
    records, log = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    # 第一次 rebalance (D1): traded 0->A=1.0, ratio=1.0, turnover=0.5
    assert log[0].execution_date == dates[1]
    assert abs(log[0].traded_notional_ratio - 1.0) < 1e-9
    assert abs(log[0].turnover - 0.5) < 1e-9
    # 第二次 rebalance (D3): A 1->0 + B 0->1, ratio=2.0, turnover=1.0
    assert log[1].execution_date == dates[3]
    assert abs(log[1].traded_notional_ratio - 2.0) < 1e-9
    assert abs(log[1].turnover - 1.0) < 1e-9


def test_gross_nav_unaffected_by_cost_net_affected() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rec = next(r for r in records if r.trade_date == dates[1])
    # gross nav 不受 cost 影响（= 1.0），net nav 扣 cost（1.0 - 1.0*0.001 = 0.999）
    assert abs(rec.nav_gross - 1.0) < 1e-9
    assert abs(rec.nav_net - 0.999) < 1e-9


def test_no_rebalance_zero_turnover() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    # D1 建仓（turnover>0），D2 无 rebalance（turnover=0）
    assert next(r.turnover for r in records if r.trade_date == dates[1]) > 0
    assert next(r.turnover for r in records if r.trade_date == dates[2]) == 0.0


def test_all_cash_flat_nav() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    records, _ = run_backtest(prices, dates, {}, _cfg(), execution_lag_sessions=1)
    assert all(abs(r.nav_net - 1.0) < 1e-9 for r in records)


def test_unavailable_target_remains_cash() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {},  # A 停牌，无 execution bar
        dates[2]: {"A": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, log = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert log[0].unavailable_target_count == 1
    assert log[0].filled_target_count == 0
    # 全 cash，nav flat
    assert abs(_nav(records, dates[2]) - 1.0) < 1e-9


def test_held_missing_bar_zero_return() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},  # A 停牌
        dates[3]: {"A": 110.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    # D2 停牌，return 0（mark 不变）
    assert next(r.daily_return_net for r in records if r.trade_date == dates[2]) == 0.0


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
        dates[2]: {"A": 110.0, "B": 100.0},  # A 涨，B 平
        dates[3]: {"A": 110.0, "B": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    records, _ = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    # D2 之后 A 的 exposure 应该 > 0.5（drift），cash=0
    rec = next(r for r in records if r.trade_date == dates[2])
    assert rec.gross_exposure > 0.999  # 仍满仓
    assert rec.net_exposure > 0.999
