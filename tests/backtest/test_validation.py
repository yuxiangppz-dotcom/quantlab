from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.backtest import engine as eng
from quantlab.backtest.engine import (
    _accumulate_checks,
    _Book,
    _Position,
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
