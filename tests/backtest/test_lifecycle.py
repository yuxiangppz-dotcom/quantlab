from datetime import date, timedelta

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    LEGACY_DELIST_DATE_INCLUSIVE,
    BacktestConfig,
    LifecycleMonitor,
    code_change_lineage_audit,
    first_invalid_open_session,
    is_instrument_invalid_on_delist_boundary,
    pit_eligibility_frame,
    pit_eligible_instrument_ids,
    run_backtest,
)
from quantlab.data.models import Security, SecurityCodeChange
from quantlab.portfolio import TargetPortfolio, TargetWeight
from quantlab.research.universe import is_v1_a_share

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)
D3 = date(2026, 1, 8)


def _security(instrument_id, delist_date=None, list_date=date(2000, 1, 1)) -> Security:
    market = instrument_id.split(".")[1] if "." in instrument_id else "SH"
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id.split(".")[0],
        name="x",
        exchange="SSE" if market == "SH" else "SZSE",
        market=market,
        board="主板",
        list_status="D" if delist_date is not None else "L",
        list_date=list_date,
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


def test_new_mode_blocks_on_delist_date() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor(
        [_security("A", delist_date=D1)], [], mode=DELIST_DATE_IS_FIRST_INVALID_V1
    )
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    # delist_date D1 is the first invalid day in the new mode
    assert result.status == "blocked_by_unsupported_event"
    assert result.first_blocking_event.blocking_session == D1
    assert result.valid_through == D0


def test_legacy_mode_inclusive_on_delist_date() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor(
        [_security("A", delist_date=D1)], [], mode=LEGACY_DELIST_DATE_INCLUSIVE
    )
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    # legacy: trade_date > delist_date -> blocked at D2, not D1
    assert result.first_blocking_event.blocking_session == D2
    assert result.valid_through == D1


def test_new_mode_price_on_effective_day_still_blocks() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {"A": 100.0}})
    targets = {D0: _target(D0, {"A": 1.0})}
    monitor = LifecycleMonitor(
        [_security("A", delist_date=D1)], [], mode=DELIST_DATE_IS_FIRST_INVALID_V1
    )
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    # price present on D1 but the instrument is already invalid -> still blocks
    assert result.first_blocking_event.blocking_session == D1


def test_new_mode_signal_valid_exec_invalid() -> None:
    dates = [D0, D1, D2]
    prices = _price_frame({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}})
    # signal on D1 (valid), exec on D2 (invalid under new mode with delist D2)
    targets = {D1: _target(D1, {"A": 1.0})}
    monitor = LifecycleMonitor(
        [_security("A", delist_date=D2)], [], mode=DELIST_DATE_IS_FIRST_INVALID_V1
    )
    result = run_backtest(prices, dates, targets, _cfg(), mode="strict", lifecycle=monitor)
    # blocked at execution date D2 (not silently opened)
    assert result.first_blocking_event.blocking_session == D2


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError):
        LifecycleMonitor([], [], mode="bogus_mode")
    with pytest.raises(ValueError):
        first_invalid_open_session(D1, [D0, D1, D2], "bogus_mode")


def test_legacy_delist_description() -> None:
    monitor = LifecycleMonitor([_security("A", delist_date=D1)], [])
    ev = monitor.event_for("A", D2)
    assert ev is not None
    assert ev.description == f"invalid after delist_date {D1}"


def test_v1_delist_description() -> None:
    monitor = LifecycleMonitor(
        [_security("A", delist_date=D1)], [], mode=DELIST_DATE_IS_FIRST_INVALID_V1
    )
    ev = monitor.event_for("A", D1)
    assert ev is not None
    assert ev.description == f"invalid from delist_date {D1}"


def test_first_invalid_open_session_open_day_gap() -> None:
    # delist_date D1 is itself an open session: legacy skips it, v1 uses it
    open_dates = [D0, D1, D2, D3]
    legacy = first_invalid_open_session(D1, open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D1, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy == D2
    assert v1 == D1


def test_first_invalid_open_session_weekend() -> None:
    # 2026-01-10 is a Saturday (not an open session); Monday 2026-01-12 follows
    fri = date(2026, 1, 9)
    sat = date(2026, 1, 10)
    mon = date(2026, 1, 12)
    open_dates = [fri, mon]
    legacy = first_invalid_open_session(sat, open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(sat, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    # no open session on the weekend date, so both collapse to the same session
    assert legacy == mon
    assert v1 == mon


def test_first_invalid_open_session_holiday() -> None:
    # D1 is a closed session (holiday): absent from open_dates
    open_dates = [D0, D2, D3]
    legacy = first_invalid_open_session(D1, open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D1, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy == D2
    assert v1 == D2


def test_first_invalid_open_session_before_window() -> None:
    # delist before all open sessions -> both modes resolve to the first session
    open_dates = [D1, D2, D3]
    legacy = first_invalid_open_session(D0, open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D0, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy == D1
    assert v1 == D1


def test_first_invalid_open_session_after_window() -> None:
    # delist after all open sessions -> no invalid session within the window
    open_dates = [D0, D1, D2]
    legacy = first_invalid_open_session(D3, open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D3, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy is None
    assert v1 is None


def test_predicate_legacy_before_on_after() -> None:
    pred = is_instrument_invalid_on_delist_boundary
    assert pred(D0, D1, LEGACY_DELIST_DATE_INCLUSIVE) is False  # before
    assert pred(D1, D1, LEGACY_DELIST_DATE_INCLUSIVE) is False  # on (inclusive)
    assert pred(D2, D1, LEGACY_DELIST_DATE_INCLUSIVE) is True   # after


def test_predicate_v1_before_on_after() -> None:
    pred = is_instrument_invalid_on_delist_boundary
    assert pred(D0, D1, DELIST_DATE_IS_FIRST_INVALID_V1) is False  # before
    assert pred(D1, D1, DELIST_DATE_IS_FIRST_INVALID_V1) is True   # on (first invalid)
    assert pred(D2, D1, DELIST_DATE_IS_FIRST_INVALID_V1) is True   # after


def test_predicate_invalid_mode() -> None:
    with pytest.raises(ValueError):
        is_instrument_invalid_on_delist_boundary(D1, D1, "bogus_mode")


def test_first_invalid_open_session_matches_predicate() -> None:
    # first_invalid_open_session is exactly "first open date where predicate is True"
    open_dates = [D0, D1, D2, D3]
    for mode in (LEGACY_DELIST_DATE_INCLUSIVE, DELIST_DATE_IS_FIRST_INVALID_V1):
        for delist in (D0, D1, D2, D3):
            got = first_invalid_open_session(delist, open_dates, mode)
            expected = next(
                (
                    d
                    for d in open_dates
                    if is_instrument_invalid_on_delist_boundary(d, delist, mode)
                ),
                None,
            )
            assert got == expected


def test_delist_before_period_first_open_session_invalid() -> None:
    # requested period = [D1, D2, D3]; delist D0 is before the period, so the
    # first open session of the period is already invalid under both modes.
    period_open_dates = [D1, D2, D3]
    legacy = first_invalid_open_session(D0, period_open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D0, period_open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy == D1
    assert v1 == D1


def test_delist_within_period_boundary() -> None:
    period_open_dates = [D0, D1, D2, D3]
    # delist D1 (open day) within period -> legacy first invalid is D2, v1 is D1
    legacy = first_invalid_open_session(D1, period_open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D1, period_open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy == D2
    assert v1 == D1


def test_delist_after_period_none() -> None:
    period_open_dates = [D0, D1, D2]
    legacy = first_invalid_open_session(D3, period_open_dates, LEGACY_DELIST_DATE_INCLUSIVE)
    v1 = first_invalid_open_session(D3, period_open_dates, DELIST_DATE_IS_FIRST_INVALID_V1)
    assert legacy is None
    assert v1 is None


# ------------------------------------------------------ PIT eligibility set --


def test_pit_eligible_instrument_ids_listing_window_and_predicate() -> None:
    securities = [
        _security("A.SZ"),                                   # plain eligible
        _security("B.SZ", list_date=D0),                     # listed exactly on D0
        _security("C.SZ", list_date=D1),                     # not yet listed on D0
        _security("200001.SZ"),                              # B-share (predicate)
        _security("830001.BJ"),                              # BJ (predicate)
    ]
    eligible = pit_eligible_instrument_ids(
        securities, [], D0, LEGACY_DELIST_DATE_INCLUSIVE, is_v1_a_share
    )
    # C is excluded (list_date > D0); B-share/BJ fail the V1 predicate; an
    # instrument listed exactly on D0 IS eligible on its list date
    assert eligible == ["A.SZ", "B.SZ"]


def test_pit_eligible_instrument_ids_respects_frozen_delist_boundary() -> None:
    securities = [
        _security("A.SZ", delist_date=D1),
        _security("B.SZ"),
    ]
    # legacy: D1 (the delist date itself) is still valid
    legacy = pit_eligible_instrument_ids(
        securities, [], D1, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert legacy == ["A.SZ", "B.SZ"]
    # v1: D1 is the first invalid day
    v1 = pit_eligible_instrument_ids(
        securities, [], D1, DELIST_DATE_IS_FIRST_INVALID_V1
    )
    assert v1 == ["B.SZ"]
    # both modes agree the day after the delist date is invalid
    after = pit_eligible_instrument_ids(
        securities, [], D2, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert after == ["B.SZ"]


def test_pit_eligible_instrument_ids_code_change_boundary() -> None:
    securities = [
        _security("A.SZ"),
        _security("A.NEW.SZ", list_date=D2),
        _security("B.SZ"),
    ]
    changes = [_code_change("A.SZ", "A.NEW.SZ", effective=D1)]
    # before the effective date only the old identity is eligible — the
    # successor master row's list_date never makes it visible early
    before = pit_eligible_instrument_ids(securities, changes, D0, LEGACY_DELIST_DATE_INCLUSIVE)
    assert before == ["A.SZ", "B.SZ"]
    # from the effective date (inclusive) the identity switch happens: the
    # successor is eligible and the old code is gone — even when the
    # successor's backfilled list_date would suggest a later start
    on_effective = pit_eligible_instrument_ids(
        securities, changes, D1, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert on_effective == ["A.NEW.SZ", "B.SZ"]
    with_new = pit_eligible_instrument_ids(
        securities, changes, D2, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert with_new == ["A.NEW.SZ", "B.SZ"]


def test_pit_eligible_instrument_ids_never_uses_list_status() -> None:
    # list_status is a CURRENT attribute; historical eligibility must not
    # consult it (a delisted-in-2026 security was listed in 2020)
    securities = [
        _security("A.SZ", delist_date=date(2026, 6, 1)),
        _security("B.SZ"),
    ]
    eligible = pit_eligible_instrument_ids(
        securities, [], date(2020, 6, 1), LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert eligible == ["A.SZ", "B.SZ"]


def test_pit_eligibility_frame_covers_every_signal_date_independently() -> None:
    securities = [
        _security("A.SZ"),
        _security("B.SZ", list_date=D2),   # newly listed by the second signal
    ]
    frame = pit_eligibility_frame(
        securities, [], [D0, D2], LEGACY_DELIST_DATE_INCLUSIVE
    )
    d0_rows = frame[frame["trade_date"] == D0]["instrument_id"].tolist()
    d2_rows = frame[frame["trade_date"] == D2]["instrument_id"].tolist()
    assert d0_rows == ["A.SZ"]
    assert d2_rows == ["A.SZ", "B.SZ"]
    # frame shape matches the (instrument_id, trade_date) cross-section contract
    assert list(frame.columns) == ["instrument_id", "trade_date"]


# ------------------------------------------------- PIT code-change lineage --


def _lineage_change(old, new, effective, original_list_date=date(2000, 1, 1)):
    return SecurityCodeChange(
        old_instrument_id=old,
        new_instrument_id=new,
        effective_date=effective,
        old_name="x",
        original_list_date=original_list_date,
    )


def test_pit_lineage_successor_backfilled_list_date_not_visible_early() -> None:
    """Production shape (300114.SZ -> 302132.SZ, effective 2025-02-17).

    The master has ONLY the successor row whose ``list_date`` is the vendor's
    backfilled original list date (2010-08-27); the old id is absent from the
    master entirely, and the vendor also backfilled 2020-2024 bars under the
    successor id. The successor must NOT be PIT-visible before its effective
    date, and the old id must stay PIT-eligible (eligible-but-unpriced) until
    the day before the effective date.
    """
    securities = [
        _security("302132.SZ", list_date=date(2010, 8, 27)),  # backfilled
    ]
    changes = [
        _lineage_change(
            "300114.SZ", "302132.SZ",
            effective=date(2025, 2, 17), original_list_date=date(2010, 8, 27),
        ),
    ]
    before = pit_eligible_instrument_ids(
        securities, changes, date(2024, 12, 31), LEGACY_DELIST_DATE_INCLUSIVE,
        is_v1_a_share,
    )
    assert before == ["300114.SZ"]  # old identity, eligible-but-unpriced

    on = pit_eligible_instrument_ids(
        securities, changes, date(2025, 2, 17), LEGACY_DELIST_DATE_INCLUSIVE,
        is_v1_a_share,
    )
    assert on == ["302132.SZ"]  # identity switches exactly on the effective date

    after = pit_eligible_instrument_ids(
        securities, changes, date(2025, 3, 3), LEGACY_DELIST_DATE_INCLUSIVE,
        is_v1_a_share,
    )
    assert after == ["302132.SZ"]


def test_pit_lineage_pre_period_lineage_uses_successor_id() -> None:
    """2018/2019 lineages: within 2020+ only the successor id is eligible."""
    securities = [
        _security("001872.SZ", list_date=date(1993, 5, 5)),
        _security("001914.SZ", list_date=date(1994, 9, 28)),
    ]
    changes = [
        _lineage_change("000022.SZ", "001872.SZ", effective=date(2018, 12, 26)),
        _lineage_change("000043.SZ", "001914.SZ", effective=date(2019, 12, 16)),
    ]
    eligible = pit_eligible_instrument_ids(
        securities, changes, date(2020, 6, 1), LEGACY_DELIST_DATE_INCLUSIVE,
        is_v1_a_share,
    )
    assert "001872.SZ" in eligible and "001914.SZ" in eligible
    assert "000022.SZ" not in eligible and "000043.SZ" not in eligible


def test_pit_lineage_old_and_new_never_co_eligible() -> None:
    """No session may carry both identities of one lineage."""
    securities = [
        _security("302132.SZ", list_date=date(2010, 8, 27)),
    ]
    changes = [
        _lineage_change(
            "300114.SZ", "302132.SZ",
            effective=date(2025, 2, 17), original_list_date=date(2010, 8, 27),
        ),
    ]
    day = date(2024, 1, 1)
    while day < date(2025, 6, 1):
        eligible = set(
            pit_eligible_instrument_ids(
                securities, changes, day, LEGACY_DELIST_DATE_INCLUSIVE,
                is_v1_a_share,
            )
        )
        assert not {"300114.SZ", "302132.SZ"} <= eligible, day
        day += timedelta(days=1)


def test_pit_lineage_master_row_predecessor_still_governed_by_monitor() -> None:
    """A predecessor present in the master (with its own list/delist facts)
    remains governed by the monitor; the lineage only removes it from the
    effective date onward."""
    securities = [
        _security("OLD.SZ", delist_date=date(2026, 12, 31), list_date=date(2015, 1, 1)),
        _security("NEW.SZ", list_date=date(2010, 1, 1)),  # backfilled list_date
    ]
    changes = [_lineage_change("OLD.SZ", "NEW.SZ", effective=D2)]
    before = pit_eligible_instrument_ids(
        securities, changes, D0, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert before == ["OLD.SZ"]  # NEW not visible before effective despite list_date
    on = pit_eligible_instrument_ids(
        securities, changes, D2, LEGACY_DELIST_DATE_INCLUSIVE
    )
    assert on == ["NEW.SZ"]


def test_code_change_lineage_audit_reports_identity_evidence() -> None:
    """The formal lineage audit proves: no future-successor visibility, no
    old/new overlap, and counts eligible-but-unpriced predecessors."""
    securities = [
        _security("302132.SZ", list_date=date(2010, 8, 27)),
    ]
    changes = [
        _lineage_change(
            "300114.SZ", "302132.SZ",
            effective=date(2025, 2, 17), original_list_date=date(2010, 8, 27),
        ),
    ]
    signal_dates = [date(2024, 12, 20), date(2024, 12, 27), date(2025, 2, 21)]
    price_frame = pd.DataFrame(
        [
            {"instrument_id": "302132.SZ", "trade_date": d, "adj_close": 10.0}
            for d in signal_dates
        ]
    )
    rows = code_change_lineage_audit(
        changes, securities, signal_dates, LEGACY_DELIST_DATE_INCLUSIVE,
        price_frame, is_v1_a_share,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["old_instrument_id"] == "300114.SZ"
    assert row["new_instrument_id"] == "302132.SZ"
    assert row["effective_date"] == "2025-02-17"
    assert row["future_successor_violation_count"] == 0
    assert row["old_new_overlap_count"] == 0
    # both pre-effective signal dates carry the old id without any price row
    assert row["eligible_but_unpriced_predecessor_count"] == 2
    assert row["old_id_in_security_master"] is False
    assert row["old_eligible_signal_date_count"] == 2
    assert row["new_eligible_signal_date_count"] == 1
