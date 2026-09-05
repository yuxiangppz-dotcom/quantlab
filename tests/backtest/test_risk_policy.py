from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    BacktestConfig,
    LifecycleMonitor,
    risk_policy_statistics,
    run_backtest,
)
from quantlab.data.models import Security
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)
D3 = date(2026, 1, 8)
D4 = date(2026, 1, 9)
D5 = date(2026, 1, 12)


def _prices(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": instrument_id, "trade_date": d, "adj_close": price}
            for d, values in rows.items()
            for instrument_id, price in values.items()
        ]
    )


def _target(as_of, weights, cash=0.0) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=as_of,
        positions=tuple(TargetWeight(i, w) for i, w in weights.items()),
        cash_weight=cash,
    )


def _facts(available_from=D2, instrument_id="A") -> dict:
    return {
        instrument_id: {
            "facts": [
                {
                    "fact_type": "termination_decision",
                    "fact_id": f"{instrument_id}:termination:{available_from}",
                    "content_verified": True,
                    "public_time_verified": True,
                    "available_from": available_from.isoformat(),
                    "source": "fixture",
                }
            ]
        }
    }


def _cfg(bps=10.0, initial_nav=1.0) -> BacktestConfig:
    return BacktestConfig(
        initial_nav=initial_nav,
        transaction_cost_bps=bps,
        annualization=252,
    )


def _snap(result, d, book="net"):
    return next(b for b in result.books if b.trade_date == d and b.book == book)


def _position(result, d, instrument_id="A", book="net"):
    return next(
        (p for p in _snap(result, d, book).positions if p.instrument_id == instrument_id),
        None,
    )


def _run(prices, dates, targets, facts, **kwargs):
    return run_backtest(
        prices,
        dates,
        targets,
        kwargs.pop("config", _cfg()),
        risk_facts=facts,
        risk_policy=EXIT_POLICY_ID,
        **kwargs,
    )


def _security(instrument_id, delist_date) -> Security:
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id,
        name="x",
        exchange="SSE",
        market="SH",
        board="main",
        list_status="D",
        list_date=date(2000, 1, 1),
        delist_date=delist_date,
    )


def test_future_fact_has_no_action_and_future_change_cannot_affect_prefix() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    facts = _facts(available_from=D3)
    altered = deepcopy(facts)
    altered["A"]["facts"][0]["fact_id"] = "changed-future-fact"

    left = _run(prices, dates, targets, facts)
    right = _run(prices, dates, targets, altered)

    assert all(r.decision_date >= D3 for r in left.risk_policy_audit)
    assert [r.nav_net for r in left.records[:3]] == [r.nav_net for r in right.records[:3]]
    assert [t.__dict__ for t in left.trades if t.execution_date < D3] == [
        t.__dict__ for t in right.trades if t.execution_date < D3
    ]


def test_available_day_marks_then_exits_each_book_with_own_amount_and_fee() -> None:
    dates = [D0, D1, D2]
    prices = _prices(
        {D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {"A": 110.0}}
    )
    result = _run(prices, dates, {D0: _target(D0, {"A": 1.0})}, _facts())

    gross = next(
        r for r in result.risk_policy_audit
        if r.decision_date == D2 and r.book == "gross"
    )
    net = next(
        r for r in result.risk_policy_audit
        if r.decision_date == D2 and r.book == "net"
    )
    assert gross.risk_state == net.risk_state == "exited"
    assert gross.execution_price == net.execution_price == 110.0
    assert gross.forced_sell_value == pytest.approx(1.1)
    assert gross.fee == 0.0
    assert net.forced_sell_value < gross.forced_sell_value
    assert net.fee == pytest.approx(0.001 * net.forced_sell_value)
    assert _position(result, D2, book="gross") is None
    assert _position(result, D2, book="net") is None
    assert _snap(result, D2, "gross").nav == pytest.approx(1.1)


def test_pending_persists_without_price_then_resume_marks_and_exits() -> None:
    dates = [D0, D1, D2, D3, D4]
    prices = _prices(
        {
            D0: {"A": 100.0},
            D1: {"A": 100.0},
            D2: {},
            D3: {},
            D4: {"A": 120.0},
        }
    )
    result = _run(prices, dates, {D0: _target(D0, {"A": 1.0})}, _facts())
    net_rows = [r for r in result.risk_policy_audit if r.book == "net"]

    assert [r.risk_state for r in net_rows] == [
        "pending_no_price",
        "pending_no_price",
        "exited",
    ]
    assert all(r.execution_price is None and r.fee == 0 for r in net_rows[:2])
    assert _position(result, D2).value == pytest.approx(_position(result, D3).value)
    assert net_rows[-1].pre_position_value == pytest.approx(
        _position(result, D3).value * 1.2
    )
    assert _position(result, D4) is None


def test_exit_remains_zero_upper_bound_for_new_entry_refill_and_reentry() -> None:
    dates = [D0, D1, D2, D3, D4, D5]
    prices = _prices({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 0.5, "B": 0.5}),
        D2: _target(D2, {"A": 1.0}),
        D4: _target(D4, {"A": 1.0}),
    }
    result = _run(prices, dates, targets, _facts())

    assert _position(result, D2) is None
    assert _position(result, D3) is None
    assert _position(result, D5) is None
    net_rows = [r for r in result.risk_policy_audit if r.book == "net"]
    assert next(r for r in net_rows if r.decision_date == D3).prevented_new_entry
    assert next(r for r in net_rows if r.decision_date == D2).prevented_refill is False
    assert next(r for r in net_rows if r.decision_date == D5).prevented_new_entry


def test_unheld_available_fact_prevents_first_entry() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0} for d in dates})
    targets = {D2: _target(D2, {"A": 1.0})}
    result = _run(prices, dates, targets, _facts())

    assert _position(result, D3) is None
    row = next(
        r for r in result.risk_policy_audit
        if r.book == "net" and r.decision_date == D3
    )
    assert row.prevented_new_entry
    assert row.forced_sell_value == 0.0


def test_forced_exit_and_rebalance_same_day_no_round_trip_or_renormalization() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(
        {d: {"A": 100.0, "B": 100.0, "C": 100.0} for d in dates}
    )
    targets = {
        D0: _target(D0, {"A": 0.10, "B": 0.45, "C": 0.45}),
        D2: _target(D2, {"A": 0.20, "B": 0.40, "C": 0.40}),
    }
    result = _run(
        prices,
        dates,
        targets,
        _facts(available_from=D3),
        config=_cfg(bps=0.0),
    )

    snap = _snap(result, D3)
    weights = {p.instrument_id: p.weight for p in snap.positions}
    assert "A" not in weights
    assert weights == pytest.approx({"B": 0.40, "C": 0.40})
    assert snap.cash_weight == pytest.approx(0.20)
    a_nonzero = [
        t for t in result.trades
        if t.book == "net" and t.execution_date == D3
        and t.instrument_id == "A" and t.signed_trade_value != 0
    ]
    assert len(a_nonzero) == 1
    assert a_nonzero[0].reason == "risk_forced_exit"
    assert all(t.signed_trade_value <= 0 for t in a_nonzero)
    audit = next(
        r for r in result.risk_policy_audit
        if r.book == "net" and r.decision_date == D3 and r.instrument_id == "A"
    )
    assert audit.prevented_refill
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9


def test_forced_exit_plus_rebalance_session_fee_and_ledger_reconcile() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 0.5, "B": 0.5}),
        D2: _target(D2, {"A": 0.5, "B": 0.5}),
    }
    result = _run(prices, dates, targets, _facts(available_from=D3))
    net_trades = [
        t for t in result.trades if t.book == "net" and t.execution_date == D3
    ]
    expected_fee = 0.001 * sum(abs(t.signed_trade_value) for t in net_trades)
    assert _snap(result, D3).fee == pytest.approx(expected_fee)
    assert result.rebalances[-1].transaction_cost == pytest.approx(expected_fee)
    for book in result.books:
        assert book.nav == pytest.approx(book.cash + sum(p.value for p in book.positions))
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9


def test_no_price_until_invalidation_blocks_without_terminal_settlement() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}, D3: {}})
    lifecycle = LifecycleMonitor(
        [_security("A", D3)],
        [],
        mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )
    result = _run(
        prices,
        dates,
        {D0: _target(D0, {"A": 1.0})},
        _facts(),
        lifecycle=lifecycle,
        mode="strict",
    )

    assert result.status == "blocked_by_unsupported_event"
    assert result.valid_through == D2
    assert _position(result, D2) is not None
    assert any(r.risk_state == "pending_no_price" for r in result.risk_policy_audit)
    assert any(r.risk_state == "blocked_before_exit" for r in result.risk_policy_audit)
    assert not any(t.reason == "risk_forced_exit" for t in result.trades)


def test_risk_policy_scale_and_input_order_invariance() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(
        {
            D0: {"A": 100.0, "B": 100.0},
            D1: {"A": 100.0, "B": 100.0},
            D2: {"A": 110.0, "B": 100.0},
            D3: {"A": 110.0, "B": 100.0},
        }
    )
    targets_ab = {D0: _target(D0, {"A": 0.5, "B": 0.5})}
    targets_ba = {D0: _target(D0, {"B": 0.5, "A": 0.5})}
    base = _run(prices, dates, targets_ab, _facts())
    shuffled = _run(
        prices.sample(frac=1, random_state=7).reset_index(drop=True),
        dates,
        targets_ba,
        _facts(),
    )
    scaled = _run(
        prices,
        dates,
        targets_ab,
        _facts(),
        config=_cfg(initial_nav=10.0),
    )

    assert [r.daily_return_net for r in base.records] == pytest.approx(
        [r.daily_return_net for r in shuffled.records]
    )
    assert [r.nav_net for r in scaled.records] == pytest.approx(
        [10 * r.nav_net for r in base.records]
    )
    assert [r.cash_weight for r in scaled.records] == pytest.approx(
        [r.cash_weight for r in base.records]
    )
    base_exit = next(
        r for r in base.risk_policy_audit if r.book == "net" and r.forced_sell_value > 0
    )
    scaled_exit = next(
        r for r in scaled.risk_policy_audit if r.book == "net" and r.forced_sell_value > 0
    )
    assert scaled_exit.forced_sell_value == pytest.approx(10 * base_exit.forced_sell_value)
    assert scaled_exit.fee == pytest.approx(10 * base_exit.fee)


def test_statistics_report_overall_without_double_counting_books() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0} for d in dates})
    result = _run(prices, dates, {D0: _target(D0, {"A": 1.0})}, _facts())
    stats = risk_policy_statistics(result.risk_policy_audit)

    assert stats["unique_exit_required_instruments"] == 1
    assert stats["successful_forced_exits"] == 1
    assert stats["per_book"]["gross"]["successful_forced_exits"] == 1
    assert stats["per_book"]["net"]["successful_forced_exits"] == 1
