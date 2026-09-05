from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.backtest import engine as eng
from quantlab.backtest.engine import (
    _accumulate_checks,
    _Book,
    _Position,
    _record_residual,
    _ResidualAccumulator,
)
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)


def _book(cash=0.0, positions=None) -> _Book:
    b = _Book(1.0)
    b.cash = cash
    b.positions = positions or {}
    return b


def test_abs_and_rel_tracked_independently() -> None:
    acc = _ResidualAccumulator()
    d1 = date(2026, 1, 5)
    d2 = date(2026, 1, 6)
    acc.add("check", abs_r=0.01, rel_r=0.001, trade_date=d1, book="gross")
    acc.add("check", abs_r=0.005, rel_r=0.01, trade_date=d2, book="net")
    (r,) = acc.results()
    assert r.max_abs == pytest.approx(0.01)
    assert r.max_abs_date == d1
    assert r.max_abs_book == "gross"
    assert r.max_rel == pytest.approx(0.01)
    assert r.max_rel_date == d2
    assert r.max_rel_book == "net"


def test_negative_cash_detected() -> None:
    acc = _ResidualAccumulator()
    book = _book(cash=-0.1, positions={"A": _Position(1.1, 100.0, D0)})
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.001, None
    )
    assert any("negative cash" in v for v in violations)


def test_negative_position_detected() -> None:
    acc = _ResidualAccumulator()
    book = _book(cash=1.0, positions={"A": _Position(-0.2, 100.0, D0)})
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.001, None
    )
    assert any("negative position" in v for v in violations)


def test_nan_inf_detected() -> None:
    acc = _ResidualAccumulator()
    for bad in (float("nan"), float("inf")):
        book = _book(cash=bad, positions={})
        violations = _accumulate_checks(
            acc, "net", D0, book, 1.0, 0.0, 0.0, 0.001, None
        )
        assert any("not finite" in v for v in violations)
    book = _book(cash=1.0, positions={"A": _Position(float("nan"), 100.0, D0)})
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.001, None
    )
    assert any("not finite" in v for v in violations)


def test_scale_invariance_of_validity() -> None:
    for nav in (1e-10, 1e-6, 1.0, 1e6):
        acc = _ResidualAccumulator()
        book = _book(cash=nav, positions={})
        violations = _accumulate_checks(
            acc, "net", D0, book, nav, 0.0, 0.0, 0.001, None
        )
        assert violations == []


def test_wrong_market_pnl_triggers_accounting_error(monkeypatch) -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = pd.DataFrame([
        {"instrument_id": "A", "trade_date": d, "adj_close": 100.0} for d in dates
    ])
    target = TargetPortfolio(
        as_of=D0, positions=(TargetWeight("A", 1.0),), cash_weight=0.0
    )
    cfg = BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)

    original = eng._Book.mark_to_market

    def fake_mtm(self, current_prices, trade_date, skip=frozenset()):
        pnl = original(self, current_prices, trade_date, skip=skip)
        return pnl + 0.01 * cfg.initial_nav  # inject 1% of initial NAV error

    monkeypatch.setattr(eng._Book, "mark_to_market", fake_mtm)

    result = run_backtest(prices, dates, {D0: target}, cfg)
    assert result.status == "accounting_error"
    assert result.accounting_error is not None
    assert "daily_nav_bridge" in result.accounting_error


def test_fee_mismatch_detected() -> None:
    acc = _ResidualAccumulator()
    cost_rate = 0.001
    fee = 0.001  # wrong: should be 0.001 * 0.5 = 0.0005
    cash_before = 1.0
    book = _book(cash=cash_before - 0.5 - fee, positions={"A": _Position(0.5, 100.0, D0)})
    summary = {
        "cash_before": cash_before,
        "v_minus": 1.0,
        "signed": {"A": 0.5},
        "pre_values": {"A": 0.0},
        "post_values": {"A": 0.5},
        "frozen_pre": {},
    }
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, fee, cost_rate, summary
    )
    assert any("fee_consistency" in v for v in violations)


def test_position_reconciliation_failure_detected() -> None:
    acc = _ResidualAccumulator()
    book = _book(cash=0.5, positions={"A": _Position(0.5, 100.0, D0)})
    summary = {
        "cash_before": 1.0,
        "v_minus": 1.0,
        "signed": {"A": 0.4},  # wrong: post - pre = 0.5
        "pre_values": {"A": 0.0},
        "post_values": {"A": 0.5},
        "frozen_pre": {},
    }
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.0, summary
    )
    assert any("position_reconciliation" in v for v in violations)


def test_no_false_positive_across_scales() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = pd.DataFrame([
        {"instrument_id": "A", "trade_date": d, "adj_close": 100.0} for d in dates
    ])
    target = TargetPortfolio(
        as_of=D0, positions=(TargetWeight("A", 1.0),), cash_weight=0.0
    )
    for nav in (1e-10, 1e-6, 1.0, 1e6):
        cfg = BacktestConfig(initial_nav=nav, transaction_cost_bps=0.0, annualization=252)
        result = run_backtest(prices, dates, {D0: target}, cfg)
        assert result.status == "completed"
        assert result.accounting_error is None


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


def test_position_tamper_detected(monkeypatch) -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 0.5, "B": 0.5})}
    cfg = _cfg()

    original = eng._rebalance

    def tamper(book, target, current_prices, cost_rate, signal_date,
               execution_date, blocked=frozenset()):
        summary = original(book, target, current_prices, cost_rate,
                           signal_date, execution_date, blocked=blocked)
        if "A" in book.positions and "B" in book.positions:
            book.positions["A"].value += 0.1 * cfg.initial_nav
            book.positions["B"].value -= 0.1 * cfg.initial_nav
        return summary

    monkeypatch.setattr(eng, "_rebalance", tamper)
    result = run_backtest(prices, dates, targets, cfg)
    assert result.status == "accounting_error"
    assert "position_reconciliation" in result.accounting_error


def test_frozen_invariance_uses_actual_book() -> None:
    acc = _ResidualAccumulator()
    book = _book(cash=0.0, positions={"A": _Position(1.0, 100.0, D0)})
    summary = {
        "cash_before": 0.0,
        "v_minus": 1.0,
        "signed": {},
        "pre_values": {"A": 1.0},
        "frozen_pre": {"A": 1.0},
    }
    book.positions["A"].value = 1.1  # tamper frozen value
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.0, summary
    )
    assert any("frozen_invariance" in v for v in violations)


def test_unexpected_position_detected() -> None:
    acc = _ResidualAccumulator()
    book = _book(cash=0.5, positions={
        "A": _Position(0.3, 100.0, D0),
        "X": _Position(0.2, 100.0, D0),
    })
    summary = {
        "cash_before": 1.0,
        "v_minus": 1.0,
        "signed": {"A": 0.3},
        "pre_values": {"A": 0.0},
        "frozen_pre": {},
    }
    violations = _accumulate_checks(
        acc, "net", D0, book, 1.0, 0.0, 0.0, 0.0, summary
    )
    assert any("position_reconciliation" in v for v in violations)


def test_market_pnl_nan_triggers_accounting_error(monkeypatch) -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    cfg = _cfg()

    original = eng._Book.mark_to_market

    def fake_mtm(self, current_prices, trade_date, skip=frozenset()):
        original(self, current_prices, trade_date, skip=skip)
        return float("nan")

    monkeypatch.setattr(eng._Book, "mark_to_market", fake_mtm)
    result = run_backtest(prices, dates, targets, cfg)
    assert result.status == "accounting_error"
    assert "market_pnl not finite" in result.accounting_error


def test_nonfinite_residual_and_scale_rejected() -> None:
    acc = _ResidualAccumulator()
    violations: list[str] = []
    _record_residual(acc, violations, "x", float("inf"), 1.0, D0, "net")
    assert any("residual not finite" in v for v in violations)
    violations.clear()
    _record_residual(acc, violations, "y", 1.0, 0.0, D0, "net")
    assert any("invalid scale" in v for v in violations)
    violations.clear()
    _record_residual(acc, violations, "z", 1.0, float("nan"), D0, "net")
    assert any("invalid scale" in v for v in violations)


def test_atomic_commit_on_failure(monkeypatch) -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 1.0}),
        date(2026, 1, 6): _target(date(2026, 1, 6), {"A": 1.0}),
    }
    cfg = _cfg()

    original = eng._rebalance

    def tamper(book, target, current_prices, cost_rate, signal_date,
               execution_date, blocked=frozenset()):
        summary = original(book, target, current_prices, cost_rate,
                           signal_date, execution_date, blocked=blocked)
        if execution_date == date(2026, 1, 7):
            book.cash += 0.1  # inject cash error on second rebalance session
        return summary

    monkeypatch.setattr(eng, "_rebalance", tamper)
    result = run_backtest(prices, dates, targets, cfg)
    assert result.status == "accounting_error"
    # failed day (exec of second signal = 2026-01-07) excluded from valid records
    assert all(r.trade_date != date(2026, 1, 7) for r in result.records)
    assert all(t.execution_date != date(2026, 1, 7) for t in result.trades)
    assert all(rb.execution_date != date(2026, 1, 7) for rb in result.rebalances)
    assert len(result.failed_attempts) == 1
    assert result.failed_attempts[0].trade_date == date(2026, 1, 7)
    assert result.records[-1].trade_date == date(2026, 1, 6)
