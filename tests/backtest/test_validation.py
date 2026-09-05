from datetime import date

import pytest

from quantlab.backtest.engine import (
    _accumulate_checks,
    _Book,
    _Position,
    _ResidualAccumulator,
)

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
