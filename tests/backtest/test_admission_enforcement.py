from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import BacktestConfig, run_backtest
from quantlab.backtest.admission import compute_restricted_by_signal
from quantlab.backtest.delisting_facts import load_validated_facts
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)


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


def _snap(books, d, book="net"):
    return next(b for b in books if b.trade_date == d and b.book == book)


def _weight(books, d, instr, book="net"):
    snap = _snap(books, d, book)
    for p in snap.positions:
        if p.instrument_id == instr:
            return p.weight
    return 0.0


def _value(books, d, instr, book="net"):
    snap = _snap(books, d, book)
    for p in snap.positions:
        if p.instrument_id == instr:
            return p.value
    return 0.0


def test_restricted_new_buy_forbidden_no_fee() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    restricted = {D0: frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(bps=10.0), restricted_by_signal=restricted
    )
    rb = result.rebalances[0]
    assert rb.nonzero_trade_count == 0
    assert rb.transaction_cost == pytest.approx(0.0, abs=1e-12)
    assert _snap(result.books, dates[1], "net").cash_weight == pytest.approx(1.0)


def test_restricted_held_add_forbidden() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 0.5}, cash=0.5),
        date(2026, 1, 7): _target(date(2026, 1, 7), {"A": 1.0}),
    }
    restricted = {date(2026, 1, 7): frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(), restricted_by_signal=restricted
    )
    assert _weight(result.books, dates[3], "A") == pytest.approx(0.5)


def test_restricted_held_reduce_allowed() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 1.0}),
        date(2026, 1, 7): _target(date(2026, 1, 7), {"A": 0.5}, cash=0.5),
    }
    restricted = {date(2026, 1, 7): frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(), restricted_by_signal=restricted
    )
    assert _weight(result.books, dates[3], "A") == pytest.approx(0.5)
    # a sell happened (signed < 0)
    assert any(
        t.instrument_id == "A" and t.signed_trade_value < 0
        for t in result.trades if t.execution_date == dates[3]
    )


def test_restricted_price_drop_refill_forbidden() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        D0: {"A": 100.0, "B": 100.0},
        date(2026, 1, 6): {"A": 100.0, "B": 100.0},
        date(2026, 1, 7): {"A": 90.0, "B": 100.0},
        date(2026, 1, 8): {"A": 90.0, "B": 100.0},
    })
    targets = {
        D0: _target(D0, {"A": 0.5, "B": 0.5}),
        date(2026, 1, 7): _target(date(2026, 1, 7), {"A": 0.5, "B": 0.5}),
    }
    restricted = {date(2026, 1, 7): frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(), restricted_by_signal=restricted
    )
    # A's weight drifted below 0.5 and was NOT refilled
    assert _weight(result.books, dates[3], "A") < 0.5


def test_restricted_frozen_not_sold() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({
        D0: {"A": 100.0}, date(2026, 1, 6): {"A": 100.0},
        date(2026, 1, 7): {}, date(2026, 1, 8): {},
    })
    targets = {
        D0: _target(D0, {"A": 1.0}),
        date(2026, 1, 7): _target(date(2026, 1, 7), {}, cash=1.0),
    }
    restricted = {date(2026, 1, 7): frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(bps=10.0), restricted_by_signal=restricted
    )
    # A frozen (no price) -> stays held regardless of admission
    assert _value(result.books, dates[3], "A") > 0.0


def test_restricted_no_reallocation() -> None:
    dates = [D0, date(2026, 1, 6)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 0.5, "B": 0.5})}
    restricted = {D0: frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(), restricted_by_signal=restricted
    )
    snap = _snap(result.books, dates[1], "net")
    assert _weight(result.books, dates[1], "B") == pytest.approx(0.5)
    assert snap.cash_weight == pytest.approx(0.5)


def test_empty_restriction_matches_baseline() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 0.5, "B": 0.5}),
        date(2026, 1, 7): _target(date(2026, 1, 7), {"B": 1.0}),
    }
    base = run_backtest(prices, dates, targets, _cfg(bps=10.0))
    empty = run_backtest(
        prices, dates, targets, _cfg(bps=10.0), restricted_by_signal={}
    )
    assert [r.nav_net for r in base.records] == [r.nav_net for r in empty.records]


def test_nonzero_cost_self_financing_with_cap() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0, "B": 100.0} for d in dates})
    targets = {
        D0: _target(D0, {"A": 0.5, "B": 0.5}),
        date(2026, 1, 7): _target(date(2026, 1, 7), {"A": 1.0}),
    }
    restricted = {date(2026, 1, 7): frozenset({"A"})}
    result = run_backtest(
        prices, dates, targets, _cfg(bps=10.0), restricted_by_signal=restricted
    )
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9, f"{check.check} too large"


def test_shadow_only_does_not_change_engine() -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    facts = {
        "A": {
            "facts": [
                {
                    "fact_type": "termination_decision",
                    "fact_id": "A:termination_decision:2026-01-01",
                    "content_verified": True,
                    "public_time_verified": True,
                    "publication_date": "2026-01-01",
                    "available_from": "2026-01-02",
                    "source": "s",
                }
            ]
        }
    }
    restricted = compute_restricted_by_signal(targets, facts)
    assert restricted == {D0: frozenset({"A"})}
    # running WITHOUT the restriction (baseline) still buys A
    base = run_backtest(prices, dates, targets, _cfg())
    assert _value(base.books, dates[1], "A") > 0.0


def test_failed_attempts_empty_and_readable(tmp_path) -> None:
    dates = [D0, date(2026, 1, 6), date(2026, 1, 7)]
    prices = _price_frame({d: {"A": 100.0} for d in dates})
    targets = {D0: _target(D0, {"A": 1.0})}
    result = run_backtest(prices, dates, targets, _cfg())
    assert result.failed_attempts == []


def test_validated_facts_disk_change_detected(tmp_path) -> None:
    import json

    facts = {
        "X": {
            "facts": [
                {
                    "fact_type": "termination_decision",
                    "fact_id": "x",
                    "content_verified": True,
                    "public_time_verified": True,
                    "publication_date": "2026-01-02",
                    "available_from": None,
                    "source": "s",
                }
            ]
        }
    }
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts))
    open_dates = [date(2026, 1, 2), date(2026, 1, 5)]
    _, sha1, errors1 = load_validated_facts(path, open_dates)
    assert errors1 == []
    facts["X"]["facts"][0]["content_verified"] = False
    path.write_text(json.dumps(facts))
    _, sha2, _ = load_validated_facts(path, open_dates)
    assert sha1 != sha2


def test_future_fact_does_not_change_past() -> None:
    facts = {
        "A": {
            "facts": [
                {
                    "fact_type": "termination_decision",
                    "fact_id": "a",
                    "content_verified": True,
                    "public_time_verified": True,
                    "publication_date": "2026-02-01",
                    "available_from": "2026-02-02",
                    "source": "s",
                }
            ]
        }
    }
    targets = {D0: _target(D0, {"A": 1.0})}
    restricted = compute_restricted_by_signal(targets, facts)
    # available_from 2026-02-02 > signal 2026-01-05 -> not restricted
    assert restricted == {}
