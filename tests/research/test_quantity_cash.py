"""Synthetic arithmetic scenarios; none of these inputs are actual fills/accounts."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantlab.data.models import DataValidationError
from quantlab.execution.models import Side
from quantlab.execution.rules import InstrumentIdentity, TradingCalendar, default_a_share_rule_book
from quantlab.research.costs import load_research_cost_profile
from quantlab.research.quantity_cash import (
    ResearchCashState,
    ResearchLot,
    ResearchMark,
    StockContext,
    apply_stock_quantity,
    fingerprint,
    quote_stock_quantity,
    size_stock_buy,
)

FRIDAY = date(2024, 9, 13)
WEDNESDAY = date(2024, 9, 18)
THURSDAY = date(2024, 9, 19)


def context(day=FRIDAY, board="MAIN", price="10.00"):
    # Deliberately synthetic calendar covering the intervening market holidays.
    calendar = TradingCalendar(
        (FRIDAY, WEDNESDAY, THURSDAY), FRIDAY, THURSDAY, "synthetic_calendar", "a" * 64
    )
    rule = default_a_share_rule_book().resolve("SSE", board, day)
    return StockContext(
        ResearchMark(
            "600000.SH",
            day,
            Decimal(price),
            "raw_unadjusted",
            datetime.combine(day, datetime.min.time(), UTC).replace(hour=8),
            "b" * 64,
        ),
        InstrumentIdentity("600000.SH", "SSE", board, FRIDAY, THURSDAY, "synthetic_identity"),
        rule,
        calendar,
        datetime(2024, 9, 20, tzinfo=UTC),
        load_research_cost_profile()["profile_fingerprint"],
    )


def test_minimum_fee_is_inside_budget_and_cannot_be_posthoc_subtracted():
    c = context()
    assert size_stock_buy(c, desired_quantity=1000, declared_components_budget_fen=100499) is None
    q = size_stock_buy(c, desired_quantity=1000, declared_components_budget_fen=100500)
    assert (q.quantity, q.notional_fen, q.commission_fen, q.stamp_duty_fen) == (100, 100000, 500, 0)
    assert q.cash_change_after_declared_components_fen == -100500
    assert size_stock_buy(c, desired_quantity=0, declared_components_budget_fen=10**9) is None
    assert size_stock_buy(c, desired_quantity=99, declared_components_budget_fen=10**9) is None


def test_quantity_cash_roundtrip_with_sale_stamp_and_input_immutability():
    start = ResearchCashState(FRIDAY, 200000)
    before = fingerprint(start)
    buy = apply_stock_quantity(start, context(), side=Side.BUY, quantity=100)
    assert buy.after.declared_components_cash_fen == 99500
    assert buy.after.lots == (ResearchLot("600000.SH", FRIDAY, 100),)
    sale = apply_stock_quantity(buy.after, context(WEDNESDAY), side=Side.SELL, quantity=100)
    assert sale.quote.stamp_duty_fen == 50
    assert sale.after.declared_components_cash_fen == 198950
    assert sale.after.lots == ()
    assert 200000 - sale.after.declared_components_cash_fen == 500 + 500 + 50
    assert fingerprint(start) == before and start.lots == ()
    assert sale.before_fingerprint == fingerprint(buy.after)
    assert sale.after_fingerprint == fingerprint(sale.after)
    assert sale.actual_fill is sale.complete_cash_fen is sale.after.complete_cash_fen is None
    assert sale.quote.complete_trading_cost_fen is sale.quote.fillability is None
    assert sale.quote.corporate_action_cash_fen is None
    assert sale.execution_authority is sale.quote.execution_authority is False
    assert sale.quote.performance_evidence is False
    with pytest.raises(FrozenInstanceError):
        sale.after.declared_components_cash_fen = 10


def test_t1_same_day_block_holiday_release_and_no_calendar_invention():
    start = ResearchCashState(FRIDAY, 1000, (ResearchLot("600000.SH", FRIDAY, 100),))
    with pytest.raises(DataValidationError, match="T\\+1"):
        apply_stock_quantity(start, context(), side=Side.SELL, quantity=100)
    with pytest.raises(DataValidationError, match="market session"):
        context(date(2024, 9, 16))
    result = apply_stock_quantity(start, context(WEDNESDAY), side=Side.SELL, quantity=100)
    assert result.after.lots == ()
    unknown = ResearchCashState(FRIDAY, 1000, (ResearchLot("600000.SH", date(2024, 9, 12), 100),))
    with pytest.raises(DataValidationError, match="calendar"):
        apply_stock_quantity(unknown, context(WEDNESDAY), side=Side.SELL, quantity=100)


def test_full_odd_lot_exit_cannot_sell_new_lots_or_partially_bypass_step():
    older = ResearchLot("600000.SH", FRIDAY, 137)
    state = ResearchCashState(WEDNESDAY, 1000, (older,))
    assert (
        apply_stock_quantity(state, context(WEDNESDAY), side=Side.SELL, quantity=137).after.lots
        == ()
    )
    with pytest.raises(DataValidationError, match="minimum"):
        apply_stock_quantity(state, context(WEDNESDAY), side=Side.SELL, quantity=37)
    with_new = replace(state, lots=(older, ResearchLot("600000.SH", WEDNESDAY, 100)))
    with pytest.raises(DataValidationError, match="T\\+1"):
        apply_stock_quantity(with_new, context(WEDNESDAY), side=Side.SELL, quantity=237)
    partial = apply_stock_quantity(with_new, context(WEDNESDAY), side=Side.SELL, quantity=100)
    assert partial.after.lots == (
        ResearchLot("600000.SH", FRIDAY, 37),
        ResearchLot("600000.SH", WEDNESDAY, 100),
    )


@pytest.mark.parametrize("board,desired,expected", [("MAIN", 237, 200), ("STAR", 237, 237)])
def test_board_specific_lots(board, desired, expected):
    q = size_stock_buy(
        context(board=board), desired_quantity=desired, declared_components_budget_fen=10**7
    )
    assert q.quantity == expected


def test_one_order_maximum_rejects_implicit_splitting():
    c = context(board="STAR")
    with pytest.raises(DataValidationError, match="maximum"):
        size_stock_buy(
            c, desired_quantity=c.rule.max_limit_quantity + 1, declared_components_budget_fen=10**12
        )
    with pytest.raises(DataValidationError, match="maximum"):
        quote_stock_quantity(c, side=Side.BUY, quantity=c.rule.max_limit_quantity + 1)


def test_commission_rounding_and_capital_noninvariance():
    q = quote_stock_quantity(context(price="10.01"), side=Side.BUY, quantity=10000)
    # 100,100 yuan * 0.000086 = 8.6086 yuan, component rounding -> 861 fen.
    assert q.notional_fen == 10010000 and q.commission_fen == 861
    little = size_stock_buy(
        context(), desired_quantity=100000, declared_components_budget_fen=100500
    )
    double = size_stock_buy(
        context(), desired_quantity=100000, declared_components_budget_fen=201000
    )
    assert double.quantity == 200
    assert -double.cash_change_after_declared_components_fen == 200500
    assert double.commission_fen != 2 * little.commission_fen


@pytest.mark.parametrize("value", [True, -1, 1.5, "100"])
def test_invalid_quantities_and_budgets_rejected(value):
    with pytest.raises(DataValidationError):
        quote_stock_quantity(context(), side=Side.BUY, quantity=value)
    with pytest.raises(DataValidationError):
        size_stock_buy(context(), desired_quantity=100, declared_components_budget_fen=value)


@pytest.mark.parametrize(
    "price",
    [10.0, Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), Decimal("0.001")],
)
def test_price_must_be_finite_raw_decimal_fen(price):
    with pytest.raises(DataValidationError):
        replace(context().mark, raw_price=price)


def test_adjusted_future_mismatched_unknown_evidence_rejected():
    c = context()
    with pytest.raises(DataValidationError, match="raw unadjusted"):
        replace(c.mark, basis="adjusted")
    with pytest.raises(DataValidationError, match="not available"):
        replace(c, knowledge_cutoff=datetime(2024, 9, 12, tzinfo=UTC))
    with pytest.raises(DataValidationError, match="do not match"):
        replace(c, identity=replace(c.identity, instrument_id="000001.SZ"))
    with pytest.raises(DataValidationError, match="do not match"):
        replace(c, rule=replace(c.rule, effective_to=date(2024, 9, 12)))
    with pytest.raises(DataValidationError, match="do not match"):
        replace(c, identity=replace(c.identity, board="STAR"))
    with pytest.raises(DataValidationError, match="typed evidence"):
        replace(c, rule=None)
    with pytest.raises(DataValidationError, match="profile changed"):
        size_stock_buy(
            replace(c, cost_profile_fingerprint="c" * 64),
            desired_quantity=0,
            declared_components_budget_fen=0,
        )
    with pytest.raises(DataValidationError, match="SHA-256"):
        replace(c.mark, source_fingerprint="bad")
    with pytest.raises(DataValidationError, match="timezone"):
        replace(c.mark, available_at=datetime(2024, 9, 13))


def test_insufficient_cash_wrong_side_and_future_state():
    state = ResearchCashState(FRIDAY, 100499)
    with pytest.raises(DataValidationError, match="insufficient cash"):
        apply_stock_quantity(state, context(), side=Side.BUY, quantity=100)
    with pytest.raises(DataValidationError, match="Side"):
        apply_stock_quantity(state, context(), side="buy", quantity=100)
    with pytest.raises(DataValidationError, match="exceeds"):
        apply_stock_quantity(state, context(), side=Side.SELL, quantity=100)
    with pytest.raises(DataValidationError, match="state date"):
        apply_stock_quantity(
            replace(state, as_of_session=WEDNESDAY), context(), side=Side.BUY, quantity=100
        )


def test_evidence_fingerprint_binds_sources_but_not_internal_calendar_cache():
    c = context()
    assert fingerprint(c) == fingerprint(context())
    assert fingerprint(c) != fingerprint(
        replace(c, mark=replace(c.mark, source_fingerprint="d" * 64))
    )
    assert fingerprint(c) != fingerprint(
        replace(c, calendar=replace(c.calendar, source_id="other"))
    )
    # Immutable source sessions are authority; implementation cache iteration must not hash.
    import json
    import subprocess
    import sys

    code = """
from dataclasses import dataclass
from datetime import date
from quantlab.execution.rules import TradingCalendar
from quantlab.research.quantity_cash import fingerprint
print(fingerprint(TradingCalendar((date(2024,1,2),date(2024,1,3)),date(2024,1,1),date(2024,1,5),'test','a'*64)))
"""
    import os

    results = [
        subprocess.check_output(
            [sys.executable, "-c", code], env={**os.environ, "PYTHONHASHSEED": seed}, text=True
        ).strip()
        for seed in ("1", "17")
    ]
    assert results[0] == results[1], json.dumps(results)


def test_buy_sizer_matches_independent_exhaustive_integer_search():
    c = context(price="2.37")
    for budget in (0, 24199, 24200, 100000, 350001, 600000):
        # These small orders stay below the minimum commission crossover.
        possible = [q for q in range(100, 2501, 100) if q * 237 + 500 <= budget]
        expected = max(possible, default=0)
        actual = size_stock_buy(c, desired_quantity=2500, declared_components_budget_fen=budget)
        assert (actual.quantity if actual else 0) == expected


def test_unrelated_positions_and_fifo_partial_reduction_are_preserved():
    other = ResearchLot("000001.SZ", FRIDAY, 200)
    lots = (ResearchLot("600000.SH", WEDNESDAY, 100), other, ResearchLot("600000.SH", FRIDAY, 200))
    state = ResearchCashState(THURSDAY, 12345, lots)
    result = apply_stock_quantity(state, context(THURSDAY), side=Side.SELL, quantity=200)
    assert result.after.lots == (lots[0], other)
    assert result.after.declared_components_cash_fen == 12345 + 200000 - 500 - 100
    assert state.lots == lots


def test_future_publication_mutable_sources_and_off_tick_prices_are_rejected():
    c = context()
    with pytest.raises(DataValidationError, match="published after"):
        replace(
            c,
            rule=replace(
                c.rule,
                sources=(
                    *c.rule.sources,
                    replace(
                        c.rule.sources[0],
                        published_on=date(2025, 1, 1),
                        url=c.rule.sources[0].url + "?synthetic_future_source=1",
                    ),
                ),
            ),
        )
    with pytest.raises(DataValidationError, match="immutable"):
        replace(c, rule=replace(c.rule, sources=list(c.rule.sources)))
    with pytest.raises(DataValidationError, match="off-tick"):
        replace(
            c,
            mark=replace(c.mark, raw_price=Decimal("10.01")),
            rule=replace(c.rule, price_tick=Decimal("0.02")),
        )
    with pytest.raises(DataValidationError, match="explicit date"):
        replace(c.mark, session=datetime(2024, 9, 13, tzinfo=UTC))


def test_exact_fen_and_fees_do_not_depend_on_decimal_context_precision():
    from decimal import localcontext

    c = context(price="12345.67")
    with localcontext() as arithmetic:
        arithmetic.prec = 6
        q = quote_stock_quantity(c, side=Side.BUY, quantity=1000)
        assert q.notional_fen == 1234567000
        assert q.commission_fen == 106173
        with pytest.raises(DataValidationError, match="exactly representable"):
            replace(c.mark, raw_price=Decimal("0.0099999999999999999999999999999999"))
