from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, LifecycleMonitor, run_backtest
from quantlab.data.models import Security, SecurityCodeChange
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)
D3 = date(2026, 1, 8)


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


def _code_change(old, new, effective) -> SecurityCodeChange:
    return SecurityCodeChange(
        old_instrument_id=old,
        new_instrument_id=new,
        effective_date=effective,
        old_name="x",
        original_list_date=date(2000, 1, 1),
    )


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


def _cfg() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def _ids(events) -> set[str]:
    return {e.event_id for e in events}


def test_held_delist_blocks_strict() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "blocked_by_unsupported_event"
    assert result.first_blocking_event.instrument_id == "A"
    assert result.first_blocking_event.event_type == "delist"
    assert result.first_blocking_event.blocking_session == D2
    assert result.valid_through == D1
    assert result.records[-1].trade_date == D1


def test_held_code_change_blocks_strict() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([], [_code_change("A", "A.NEW", effective=D2)])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "blocked_by_unsupported_event"
    assert result.first_blocking_event.event_type == "code_change"
    assert result.first_blocking_event.blocking_session == D2


def test_event_detected_even_with_price_present() -> None:
    # delist at D1, but price data still has a bar at D2 (data anomaly)
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {"A": 100.0}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "blocked_by_unsupported_event"
    assert result.first_blocking_event.event_type == "delist"
    assert result.first_blocking_event.blocking_session == D2


def test_unheld_delist_does_not_block() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({d: {"B": 100.0} for d in dates})
    targets = {D0: _target(D0, {"B": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "completed"
    assert result.lifecycle_events == []


def test_new_target_invalid_instrument() -> None:
    dates = [D0, D1]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D0)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "blocked_by_unsupported_event"
    assert result.first_blocking_event.book == "target"


def test_delist_inclusive_boundary() -> None:
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    assert monitor.event_for("A", D1) is None  # inclusive: still valid on delist_date
    ev = monitor.event_for("A", D2)
    assert ev is not None and ev.event_type == "delist"


def test_code_change_inclusive_boundary() -> None:
    monitor = LifecycleMonitor([], [_code_change("A", "B", effective=D1)])
    assert monitor.event_for("A", D0) is None
    ev = monitor.event_for("A", D1)
    assert ev is not None and ev.event_type == "code_change"


def test_weekend_event_maps_to_session() -> None:
    # 2026-01-10 is a Saturday; first session after it is Monday 2026-01-12
    fri = date(2026, 1, 9)
    sat = date(2026, 1, 10)
    mon = date(2026, 1, 12)
    monitor = LifecycleMonitor([_security("A", delist_date=sat)], [])
    assert monitor.event_for("A", fri) is None
    ev = monitor.event_for("A", mon)
    assert ev is not None and ev.event_type == "delist"


def test_future_event_not_affect() -> None:
    monitor = LifecycleMonitor([_security("A", delist_date=D3)], [])
    assert monitor.event_for("A", D2) is None


def test_strict_stops_diagnostic_continues() -> None:
    dates = [D0, D1, D2, D3]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}, D3: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])

    strict = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert strict.status == "blocked_by_unsupported_event"
    assert strict.valid_through == D1
    assert strict.records[-1].trade_date == D1

    diag = run_backtest(prices, dates, targets, _cfg(), mode="diagnostic", lifecycle=monitor)
    assert diag.status == "blocked_by_unsupported_event"
    assert diag.diagnostic_from == D2
    assert diag.records[-1].trade_date == D3  # continued to the end


def test_strict_diagnostic_prefix_identical() -> None:
    dates = [D0, D1, D2, D3]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}, D3: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])

    strict = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    diag = run_backtest(prices, dates, targets, _cfg(), mode="diagnostic", lifecycle=monitor)

    assert [r.trade_date for r in strict.records] == [
        r.trade_date for r in diag.records[: len(strict.records)]
    ]
    assert [r.nav_net for r in strict.records] == [
        r.nav_net for r in diag.records[: len(strict.records)]
    ]


def test_event_dedup_preserves_both_books() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    books = {e.book for e in result.lifecycle_events}
    assert books == {"gross", "net"}
    assert len(_ids(result.lifecycle_events)) == 1  # one unique event id


def test_same_invalid_target_rejected_twice() -> None:
    # A invalid from D1 (delist D0); target A on two consecutive signals
    dates = [D0, D1, D2]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 1.0}),
        D1: _target(D1, {"A": 1.0}),
    }
    monitor = LifecycleMonitor([_security("A", delist_date=D0)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="diagnostic", lifecycle=monitor)
    # A never opened
    for b in result.books:
        assert all(p.instrument_id != "A" for p in b.positions)
    # both rebalances had zero trades
    assert all(rb.nonzero_trade_count == 0 for rb in result.rebalances)
    # log dedup: one "target" event (not two)
    target_events = [e for e in result.lifecycle_events if e.book == "target"]
    assert len(target_events) == 1


def test_held_event_frozen_persistently() -> None:
    # A delisted at D1; held past D1 with quotes still present (data anomaly)
    dates = [D0, D1, D2, D3]
    prices = _price_frame({
        D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {"A": 110.0}, D3: {"A": 120.0},
    })
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="diagnostic", lifecycle=monitor)

    def a_value(d):
        for b in result.books:
            if b.trade_date == d and b.book == "net":
                for p in b.positions:
                    if p.instrument_id == "A":
                        return p.value
        return None

    v1 = a_value(D1)
    assert v1 is not None
    # frozen after D1: subsequent quotes must not change the value
    assert a_value(D2) == pytest.approx(v1)
    assert a_value(D3) == pytest.approx(v1)


def test_future_conflict_does_not_block_history() -> None:
    monitor = LifecycleMonitor(
        [_security("A", delist_date=date(2027, 1, 1))],
        [_code_change("A", "B", effective=date(2027, 2, 1))],
    )
    assert len(monitor.conflict_diagnostics()) == 1
    assert monitor.event_for("A", date(2026, 6, 1)) is None  # future, no block
    ev = monitor.event_for("A", date(2027, 1, 2))  # delist condition fires
    assert ev is not None and ev.event_type == "conflict"


def test_future_event_does_not_affect_history() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor([_security("A", delist_date=date(2027, 1, 1))], [])
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    assert result.status == "completed"
