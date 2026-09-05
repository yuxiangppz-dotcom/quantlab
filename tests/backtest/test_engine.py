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


def _snap(books, d, book="net"):
    return next(b for b in books if b.trade_date == d and b.book == book)


def _pos_value(books, d, instr, book="net") -> float:
    snap = _snap(books, d, book)
    for p in snap.positions:
        if p.instrument_id == instr:
            return p.value
    return 0.0


def test_weekly_signal_dates() -> None:
    dates = [
        date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
        date(2026, 1, 8), date(2026, 1, 9),
        date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14),
        date(2026, 1, 15), date(2026, 1, 16),
    ]
    assert weekly_signal_dates(dates) == [date(2026, 1, 9), date(2026, 1, 16)]


def test_signal_does_not_affect_t_and_t1() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 101.0},
        dates[2]: {"A": 102.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert result.rebalances[0].execution_date == dates[1]
    assert _record(result.records, dates[0]).daily_return_net == 0.0
    assert _record(result.records, dates[1]).daily_return_net == 0.0
    assert abs(_record(result.records, dates[2]).daily_return_net - (102 / 101 - 1)) < 1e-9


def test_two_stock_pnl_hand_calc() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 120.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert abs(_record(result.records, dates[2]).nav_net - 1.15) < 1e-9


def test_cash_a_b_cash_self_financing_hand_calc() -> None:
    c = 0.001
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
             date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 10)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),  # cash -> A
        dates[2]: _target(dates[2], {"B": 1.0}),  # A -> B
        dates[4]: _target(dates[4], {}, cash=1.0),  # B -> cash
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)

    nav1 = 1.0 / (1 + c)
    nav2 = nav1 * (1 - c) / (1 + c)
    nav3 = nav2 * (1 - c)

    assert abs(_record(result.records, dates[1]).nav_net - nav1) < 1e-9
    assert abs(_record(result.records, dates[3]).nav_net - nav2) < 1e-9
    assert abs(_record(result.records, dates[5]).nav_net - nav3) < 1e-9
    # gross book never pays cost
    assert abs(_record(result.records, dates[1]).nav_gross - 1.0) < 1e-9
    assert abs(_record(result.records, dates[3]).nav_gross - 1.0) < 1e-9
    assert abs(_record(result.records, dates[5]).nav_gross - 1.0) < 1e-9


def test_gross_nav_stays_initial_flat_multi_trade() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"B": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    for r in result.records:
        assert abs(r.nav_gross - 1.0) < 1e-9


def test_zero_cost_books_identical() -> None:
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
    result = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    for r in result.records:
        assert abs(r.nav_gross - r.nav_net) < 1e-9
        assert abs(r.daily_return_gross - r.daily_return_net) < 1e-9


def test_costed_gross_equals_independent_zero_cost() -> None:
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
    zero = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    costed = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert [r.nav_gross for r in costed.records] == [
        r.nav_net for r in zero.records
    ]


def test_suspension_resume_cumulative_return() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
             date(2026, 1, 8), date(2026, 1, 9)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},
        dates[3]: {},
        dates[4]: {"A": 110.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert _record(result.records, dates[2]).daily_return_net == 0.0
    assert _record(result.records, dates[3]).daily_return_net == 0.0
    assert abs(_record(result.records, dates[4]).daily_return_net - 0.10) < 1e-9
    assert abs(_record(result.records, dates[4]).nav_net - 1.10) < 1e-9


def test_frozen_delete_target_no_sell_no_fee() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
             date(2026, 1, 8), date(2026, 1, 9)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},
        dates[3]: {},  # execution date, A still frozen
        dates[4]: {"A": 110.0},
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {}, cash=1.0),  # delete A
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rb = result.rebalances[1]
    assert rb.execution_date == dates[3]
    assert rb.frozen_count == 1
    assert rb.traded_notional_ratio == 0.0
    assert rb.transaction_cost == 0.0
    # still fully invested in A, resume earns full +10% on the frozen costed NAV
    nav_after_first = _record(result.records, dates[1]).nav_net
    assert abs(_record(result.records, dates[4]).nav_net - nav_after_first * 1.10) < 1e-9


def test_frozen_add_target_no_buy() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
             date(2026, 1, 8), date(2026, 1, 9)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},
        dates[3]: {},  # A frozen; target wants 100% A
        dates[4]: {"A": 110.0},
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 0.1}, cash=0.9),
        dates[2]: _target(dates[2], {"A": 1.0}, cash=0.0),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    rb = result.rebalances[1]
    assert rb.frozen_count == 1
    assert rb.traded_notional_ratio == 0.0
    # 10% A earns +10% -> portfolio +1%
    assert abs(_record(result.records, dates[4]).nav_net - 1.01) < 1e-9


def test_missing_new_target_not_reallocated() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"B": 100.0},  # A has no execution bar
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    result = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    snap = _snap(result.books, dates[1], "net")
    assert snap.cash_weight == pytest.approx(0.5)
    b_pos = next(p for p in snap.positions if p.instrument_id == "B")
    assert b_pos.weight == pytest.approx(0.5)
    rb = result.rebalances[0]
    assert rb.unavailable_target_count == 1


def test_frozen_asset_blocks_other_trades_identity() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0, "C": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0, "C": 100.0},
        dates[2]: {"B": 100.0, "C": 100.0},  # A missing
        dates[3]: {"B": 100.0, "C": 100.0},  # A still missing
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 0.4, "B": 0.6}),
        dates[2]: _target(dates[2], {"A": 0.4, "C": 0.6}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    # frozen A value invariant in each book between D1 and D3
    assert abs(_pos_value(result.books, dates[3], "A", "net")
               - _pos_value(result.books, dates[1], "A", "net")) < 1e-9
    assert abs(_pos_value(result.books, dates[3], "A", "gross")
               - _pos_value(result.books, dates[1], "A", "gross")) < 1e-9
    # identity: nav == cash + positions, both books, every day
    for b in result.books:
        assert abs(b.nav - (b.cash + sum(p.value for p in b.positions))) < 1e-9
    # gross and net NAVs differ due to cost
    assert _record(result.records, dates[3]).nav_gross > _record(result.records, dates[3]).nav_net
    # frozen asset weight inflated in net book vs gross book
    net_a_weight = next(p.weight for p in _snap(result.books, dates[3], "net").positions
                        if p.instrument_id == "A")
    gross_a_weight = next(p.weight for p in _snap(result.books, dates[3], "gross").positions
                          if p.instrument_id == "A")
    assert net_a_weight > gross_a_weight


def test_frozen_exceeds_stock_budget() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"B": 100.0},  # A missing
        dates[3]: {"B": 100.0},  # A still missing
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"A": 0.5, "B": 0.5}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=0.0), execution_lag_sessions=1)
    rb = result.rebalances[1]
    assert rb.frozen_count == 1
    # frozen A already fills the stock budget -> B not bought
    snap = _snap(result.books, dates[3], "net")
    assert all(p.instrument_id == "A" for p in snap.positions)
    assert snap.cash_weight == pytest.approx(0.0)


def test_all_frozen_no_trade() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {},
        dates[3]: {},
    })
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"A": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    assert result.rebalances[1].traded_notional_ratio == 0.0
    assert result.rebalances[1].transaction_cost == 0.0


def test_all_cash_flat_nav() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    result = run_backtest(prices, dates, {}, _cfg(), execution_lag_sessions=1)
    assert all(abs(r.nav_net - 1.0) < 1e-9 for r in result.records)
    assert _snap(result.books, dates[0], "net").cash_weight == pytest.approx(1.0)


def test_target_already_satisfied_no_trade() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {
        dates[0]: _target(dates[0], {"A": 1.0}),
        dates[2]: _target(dates[2], {"A": 1.0}),
    }
    result = run_backtest(prices, dates, targets, _cfg(bps=10.0), execution_lag_sessions=1)
    rb = result.rebalances[1]
    assert rb.traded_notional_ratio == 0.0
    assert rb.transaction_cost == 0.0
    assert rb.filled_target_count == 0


def test_individual_weight_drift() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    snap = _snap(result.books, dates[2], "net")
    weights = {p.instrument_id: p.weight for p in snap.positions}
    assert weights["A"] > 0.5
    assert weights["B"] < 0.5
    assert weights["A"] + weights["B"] == pytest.approx(1.0)


def test_invalid_lag() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    for bad in (0, -1, 1.0, True):
        with pytest.raises(ValueError):
            run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=bad)


def test_as_of_mismatch() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[0]: _target(date(2026, 1, 4), {"A": 1.0})}
    with pytest.raises(ValueError):
        run_backtest(prices, dates, targets, _cfg())


def test_negative_target_weight_rejected() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    target = TargetPortfolio(
        as_of=dates[0],
        positions=(TargetWeight("A", -1.0),),
        cash_weight=2.0,
    )
    with pytest.raises(ValueError):
        run_backtest(prices, dates, {dates[0]: target}, _cfg())


def test_invalid_price_rejected() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    for bad in (0.0, -1.0, float("inf")):
        frame = pd.DataFrame({
            "instrument_id": ["A"],
            "trade_date": [dates[0]],
            "adj_close": [bad],
        })
        with pytest.raises(ValueError):
            run_backtest(frame, dates, targets, _cfg())


def test_nan_price_means_missing() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    frame = pd.DataFrame({
        "instrument_id": ["A"],
        "trade_date": [dates[0]],
        "adj_close": [float("nan")],
    })
    # NaN is treated as a missing price, not an invalid quote -> no raise
    result = run_backtest(frame, dates, targets, _cfg(), execution_lag_sessions=1)
    assert result.rebalances[0].unavailable_target_count == 1


def test_duplicate_price_rows_rejected() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    frame = pd.DataFrame({
        "instrument_id": ["A", "A"],
        "trade_date": [dates[0], dates[0]],
        "adj_close": [100.0, 101.0],
    })
    with pytest.raises(ValueError):
        run_backtest(frame, dates, targets, _cfg())


def test_signal_date_not_in_sessions() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {date(2026, 1, 7): _target(date(2026, 1, 7), {"A": 1.0})}
    with pytest.raises(ValueError):
        run_backtest(prices, dates, targets, _cfg())


def test_first_session_all_cash() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {"A": 100.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    first = _record(result.records, dates[0])
    assert first.nav_net == pytest.approx(1.0)
    assert first.cash_weight == pytest.approx(1.0)
    assert first.transaction_cost == 0.0
    assert first.turnover == 0.0


def test_end_window_signal_skipped() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {dates[-1]: _target(dates[-1], {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    assert result.rebalances == []
    assert len(result.skipped_executions) == 1
    assert result.skipped_executions[0].reason == "execution_beyond_simulation_end"


def test_last_trade_then_continue_valuation() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {"A": 110.0},
        dates[3]: {"A": 121.0},
    })
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg(), execution_lag_sessions=1)
    # last rebalance at D1; D2 and D3 keep valuing
    assert result.rebalances[-1].execution_date == dates[1]
    assert abs(_record(result.records, dates[3]).nav_net - 1.21) < 1e-9


def test_no_signal_full_window() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    result = run_backtest(prices, dates, {}, _cfg())
    assert [r.trade_date for r in result.records] == dates
    assert all(r.cash_weight == pytest.approx(1.0) for r in result.records)


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
    r1 = run_backtest(p1, dates, targets, _cfg())
    r2 = run_backtest(p2, dates, targets, _cfg())
    assert [r.nav_net for r in r1.records] == [r.nav_net for r in r2.records]


def test_future_price_does_not_affect_prior() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0},
        dates[1]: {"A": 100.0},
        dates[2]: {"A": 110.0},
    })
    altered = prices.copy()
    altered.loc[altered["trade_date"] == dates[2], "adj_close"] = 999.0
    targets = {dates[0]: _target(dates[0], {"A": 1.0})}
    r1 = run_backtest(prices, dates, targets, _cfg())
    r2 = run_backtest(altered, dates, targets, _cfg())
    # D0 and D1 unaffected
    for d in dates[:2]:
        assert _record(r1.records, d).nav_net == _record(r2.records, d).nav_net
        assert _record(r1.records, d).daily_return_net == _record(r2.records, d).daily_return_net


def test_row_order_independent() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({
        dates[0]: {"A": 100.0, "B": 100.0},
        dates[1]: {"A": 100.0, "B": 100.0},
        dates[2]: {"A": 110.0, "B": 120.0},
    })
    shuffled = prices.sample(frac=1, random_state=0).reset_index(drop=True)
    targets = {dates[0]: _target(dates[0], {"A": 0.5, "B": 0.5})}
    r1 = run_backtest(prices, dates, targets, _cfg())
    r2 = run_backtest(shuffled, dates, targets, _cfg())
    assert [r.nav_net for r in r1.records] == [r.nav_net for r in r2.records]


def test_target_order_independent() -> None:
    dates = [date(2026, 1, 5), date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    t1 = _target(dates[0], {"A": 0.5, "B": 0.5})
    t2 = _target(dates[0], {"B": 0.5, "A": 0.5})
    r1 = run_backtest(prices, dates, {dates[0]: t1}, _cfg())
    r2 = run_backtest(prices, dates, {dates[0]: t2}, _cfg())
    assert [r.nav_net for r in r1.records] == [r.nav_net for r in r2.records]


def test_nav_scaling() -> None:
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
    cfg1 = BacktestConfig(initial_nav=1.0, transaction_cost_bps=10.0, annualization=252)
    cfg10 = BacktestConfig(initial_nav=10.0, transaction_cost_bps=10.0, annualization=252)
    r1 = run_backtest(prices, dates, targets, cfg1)
    r10 = run_backtest(prices, dates, targets, cfg10)
    for a, b in zip(r1.records, r10.records, strict=True):
        assert b.nav_net == pytest.approx(a.nav_net * 10)
        assert b.nav_gross == pytest.approx(a.nav_gross * 10)
        assert b.daily_return_net == pytest.approx(a.daily_return_net)
        assert b.cash_weight == pytest.approx(a.cash_weight)
        assert b.turnover == pytest.approx(a.turnover)


def test_daily_identity_reconciliation() -> None:
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
    for b in result.books:
        assert abs(b.nav - (b.cash + sum(p.value for p in b.positions))) < 1e-9
    # total daily net cost equals sum of rebalance net cost
    daily_cost = sum(r.transaction_cost for r in result.records)
    rebalance_cost = sum(rb.transaction_cost for rb in result.rebalances)
    assert daily_cost == pytest.approx(rebalance_cost)
    assert result.max_conservation_residual < 1e-9
