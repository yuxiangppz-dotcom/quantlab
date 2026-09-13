"""Artificial ledger cases; these values are not historical market evidence."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal

import pytest

from quantlab.research.quantity_kernel import (
    ResearchBook,
    ResearchFeeScenario,
    ResearchOrder,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.quantity_scheduler import (
    RawCloseMark,
    ResearchDay,
    advance_research_day,
    s4_target_exit_session,
    simulate_research_schedule,
)

# Explicit fixture holiday/weekend gaps; never inferred as calendar days.
CAL = tuple(date(2024, 1, n) for n in (2, 3, 4, 5, 8, 9, 10, 11, 12))
RULES = ResearchQuantityRules("fixture", CAL[0], CAL[-1], 100, 100, 100, 100, 100000, True)
FEES = ResearchFeeScenario(
    "fixture",
    CAL[0],
    CAL[-1],
    Decimal("0.000086"),
    500,
    Decimal(0),
    Decimal("0.001"),
    Decimal(0),
    0,
    Decimal(0),
)


def context(i, code="A", price=1000, **changes):
    return replace(
        ResearchSession(
            code,
            CAL[i],
            CAL[i + 1],
            CAL[i],
            True,
            True,
            True,
            price,
            800,
            1200,
            500,
            1500,
            100000000,
            CAL[i - 1],
            20,
            100000000,
            1000000,
            Decimal("0.01"),
            RULES,
            FEES,
        ),
        **changes,
    )


def order(i, code="A", side="buy", quantity=100, identifier=None):
    return ResearchOrder(identifier or f"{i}-{code}-{side}", code, side, quantity, CAL[i - 1])


def batch(i, orders=(), contexts=(), marks=(), corporate=True):
    return ResearchDay(CAL[i], orders, contexts, marks, corporate)


def marked(i, code="A", price=1000):
    return RawCloseMark(code, CAL[i], price)


def buy_first(quantity=100):
    return batch(1, (order(1, quantity=quantity),), (context(1),), (marked(1),))


def run(days, cash=200000, end=None):
    return simulate_research_schedule(
        ResearchBook(CAL[0], cash), CAL, days, requested_end=end or days[-1].session
    )


def test_sales_fund_later_buys_and_hand_arithmetic():
    # Input deliberately places the buy first. Sale must complete first.
    second = batch(
        2,
        (order(2, "B", quantity=200), order(2, side="sell")),
        (context(2, price=1100), context(2, "B")),
        (marked(2, price=1100), marked(2, "B")),
    )
    result = run((buy_first(), second))
    assert result.status == "completed_scenario"
    assert result.records[0].marked_equity_fen == 199500
    assert result.records[0].book.cash_fen == 99500
    assert [x.order.side for x in result.records[1].attempts] == ["sell", "buy"]
    # +10000 raw-price gain - 3*500 commission -110 stamp = +8390 fen.
    assert result.book.cash_fen == 8390
    assert result.records[-1].position_value_fen == 200000
    assert result.records[-1].marked_equity_fen == 208390
    assert sum(x.modeled_fees_fen for x in result.records) == 1610
    assert [(x.instrument_id, x.quantity) for x in result.book.lots] == [("B", 200)]
    assert result.book.lots[0].sellable_on == CAL[3]
    assert result.valid_through == CAL[2]
    assert not result.execution_authority
    assert not result.performance_eligible
    assert not result.historical_data_certified


def test_blocked_exit_keeps_position_and_cannot_finance_buy():
    second = batch(
        2,
        (order(2, side="sell"), order(2, "B")),
        (context(2, price=800, down_limit_fen=800), context(2, "B")),
        (marked(2, price=800), marked(2, "B")),
    )
    result = run((buy_first(), second))
    assert result.status == "completed_scenario"
    assert [x.transition.status for x in result.records[-1].attempts] == ["blocked", "blocked"]
    assert result.book.cash_fen == 99500
    assert result.book.lots == result.records[0].book.lots
    assert result.records[-1].marked_equity_fen == 179500


def test_partial_exit_and_explicit_next_day_retry():
    second = batch(
        2,
        (order(2, side="sell", quantity=300),),
        (context(2, session_volume_shares=10000),),
        (marked(2),),
    )
    third = batch(3, (order(3, side="sell", quantity=200),), (context(3),), (marked(3),))
    result = run((buy_first(300), second, third), cash=500000)
    assert result.records[1].book.lots[0].quantity == 200
    assert result.records[1].attempts[0].transition.simulated_quantity == 100
    assert result.book.lots == ()
    assert result.book.cash_fen == 500000 - 1500 - 300


def test_same_instrument_capacity_is_shared_across_sell_and_buy():
    second = batch(
        2,
        (order(2, side="sell"), order(2)),
        (context(2, session_volume_shares=10000),),
        (marked(2),),
    )
    result = run((buy_first(), second))
    assert result.records[1].attempts[1].transition.reason == "capacity_exhausted"
    assert not result.book.lots
    assert result.book.capacity_used[0].quantity == 100


def test_same_day_new_purchase_cannot_be_sold_or_fund_another_purchase():
    first = batch(1, (order(1), order(1, side="sell")), (context(1),), (marked(1),))
    result = run((first,))
    assert result.records[0].attempts[0].transition.reason == "no_position"
    assert result.book.cash_fen == 99500
    assert result.book.lots[0].quantity == 100
    assert result.book.lots[0].sellable_on == CAL[2]


def test_sell_order_is_deterministic_by_instrument_not_input_order():
    first = batch(
        1,
        (order(1, "B"), order(1, "A")),
        (context(1), context(1, "B")),
        (marked(1), marked(1, "B")),
    )
    second = batch(
        2,
        (order(2, "B", "sell"), order(2, "A", "sell")),
        (context(2), context(2, "B")),
        (marked(2), marked(2, "B")),
    )
    result = run((first, second), cash=400000)
    assert [x.order.instrument_id for x in result.records[-1].attempts] == ["A", "B"]
    assert result.book.cash_fen == 400000 - 4 * 500 - 2 * 100


def test_capacity_resets_on_next_calendar_day_but_no_automatic_exit():
    result = run(
        (
            buy_first(),
            batch(2, marks=(marked(2),)),
            batch(3, (order(3),), (context(3),), (marked(3),)),
        ),
        cash=400000,
    )
    assert result.records[1].attempts == ()
    assert result.records[1].book.lots == result.records[0].book.lots
    assert result.records[1].book.capacity_used == ()
    assert sum(x.quantity for x in result.book.lots) == 200
    assert result.book.capacity_used[0].trade_date == CAL[3]


@pytest.mark.parametrize("corporate", [None, False])
def test_unknown_global_corporate_processing_stops_before_any_sale(corporate):
    second = batch(2, (order(2, side="sell"),), (context(2),), (marked(2),), corporate)
    result = run((buy_first(), second))
    assert result.status == "stopped"
    assert result.book == result.records[0].book
    assert result.valid_through == CAL[1]
    assert result.stopped_on == CAL[2]
    assert len(result.records) == 1


def test_preflight_of_later_buy_prevents_earlier_sale_when_context_unknown():
    second = batch(
        2, (order(2, side="sell"), order(2, "B")), (context(2),), (marked(2), marked(2, "B"))
    )
    result = run((buy_first(), second))
    assert result.stop_reason == "order_context_unknown:B"
    assert result.book.lots[0].quantity == 100
    assert result.book.cash_fen == 99500


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"fees": None}, "fees_unknown"),
        ({"fees": replace(FEES, additional_fee_rate=None)}, "fee_components_unknown"),
        ({"rules": None}, "rules_unknown"),
        ({"rules": replace(RULES, effective_through=CAL[1])}, "rules_outside_scope"),
        ({"market_open": None}, "market_open_unknown"),
        ({"calendar_verified": None}, "calendar_context_unknown_or_false"),
        ({"calendar_verified": False}, "calendar_context_unknown_or_false"),
        ({"corporate_actions_processed": None}, "instrument_corporate_processing_unknown_or_false"),
        (
            {"corporate_actions_processed": False},
            "instrument_corporate_processing_unknown_or_false",
        ),
        ({"evidence_date": CAL[1]}, "context_evidence_date_mismatch"),
        ({"next_session": CAL[4]}, "next_session_mismatch"),
        ({"next_session": None}, "next_session_mismatch"),
        ({"down_limit_fen": None}, "down_limit_fen_unknown"),
        ({"raw_close_fen": None}, "raw_close_fen_unknown"),
        ({"session_amount_fen": None}, "session_amount_fen_unknown"),
        ({"prior20_sessions": 19}, "prior20_coverage_incomplete"),
    ],
)
def test_necessary_unknowns_stop_path(changes, reason):
    second = batch(2, (order(2, side="sell"),), (context(2, **changes),), (marked(2),))
    result = run((buy_first(), second))
    assert result.status == "stopped"
    assert result.stop_reason == reason + ":A"
    assert result.book == result.records[0].book


def test_known_suspension_preserves_marked_position_without_assuming_trade():
    second = batch(
        2, (order(2, side="sell"),), (context(2, market_open=False, fees=None),), (marked(2),)
    )
    result = run((buy_first(), second))
    assert result.status == "completed_scenario"
    assert result.records[-1].attempts[0].transition.reason == "market_open_false"
    assert result.book.lots == result.records[0].book.lots
    assert result.records[-1].marked_equity_fen == 199500


def test_suspension_does_not_hide_unknown_corporate_processing():
    second = batch(
        2,
        (order(2, side="sell"),),
        (context(2, market_open=False, corporate_actions_processed=None),),
        (marked(2),),
    )
    assert run((buy_first(), second)).status == "stopped"


@pytest.mark.parametrize("orders", [(), (order(2, side="sell"),)])
def test_held_mark_unknown_stops_even_without_order(orders):
    second = batch(2, orders, (context(2),))
    result = run((buy_first(), second))
    assert result.stop_reason == "raw_mark_unknown:A"
    assert len(result.records) == 1


def test_missing_session_does_not_compress_calendar_or_fill_suffix():
    result = run((buy_first(), batch(3, marks=(marked(3),))), end=CAL[4])
    assert result.stop_reason == "session_batch_missing"
    assert result.stopped_on == CAL[2]
    assert result.valid_through == CAL[1]


def test_no_order_flat_sessions_are_real_complete_records():
    result = run(tuple(batch(i) for i in (1, 2, 3)))
    assert [x.session for x in result.records] == list(CAL[1:4])
    assert all(x.marked_equity_fen == 200000 for x in result.records)
    assert result.valid_through == CAL[3]


def test_future_fixture_change_does_not_change_prefix_or_original_inputs():
    first = buy_first()
    future = batch(2, marks=(marked(2),))
    changed = replace(future, marks=(marked(2, price=900),))
    a, b = run((first, future)), run((first, changed))
    assert a.records[0] == b.records[0]
    assert a.records[1].book == b.records[1].book
    assert a.records[1].marked_equity_fen - b.records[1].marked_equity_fen == 10000
    assert first == buy_first()
    with pytest.raises(FrozenInstanceError):
        first.session = CAL[3]


def test_fee_date_change_is_applied_only_at_execution():
    new_fee = replace(FEES, effective_from=CAL[2], sell_stamp_rate=Decimal("0.0005"))
    second = batch(2, (order(2, side="sell"),), (context(2, fees=new_fee),), (marked(2),))
    result = run((buy_first(), second))
    assert result.records[1].attempts[0].transition.stamp_fen == 50
    assert result.book.cash_fen == 198950


def test_explicit_buy_priority_is_retained_under_cash_constraint():
    first = batch(
        1,
        (order(1, "B"), order(1, "A")),
        (context(1), context(1, "B")),
        (marked(1), marked(1, "B")),
    )
    result = run((first,))
    assert result.book.lots[0].instrument_id == "B"
    assert result.records[0].attempts[1].transition.simulated_quantity == 0


def test_blocked_attempt_identifier_cannot_be_reused_on_later_day():
    first = batch(1, (order(1, identifier="same"),), (context(1, market_open=False),), (marked(1),))
    second = batch(2, (order(2, identifier="same"),), (context(2),), (marked(2),))
    with pytest.raises(ValueError, match="attempt ID already used"):
        run((first, second))


@pytest.mark.parametrize("signal", [CAL[0], CAL[2], CAL[3]])
def test_only_immediately_previous_session_signal_allowed(signal):
    second = batch(2, (replace(order(2), signal_date=signal),), (context(2),), (marked(2),))
    with pytest.raises(ValueError, match="immediately previous"):
        run((batch(1), second))


def test_future_adv_relative_to_decision_rejected_before_simulation():
    first = batch(1, (order(1),), (context(1, prior20_asof=CAL[1]),), (marked(1),))
    with pytest.raises(ValueError, match="future information"):
        run((first,))


def test_mark_and_execution_close_cannot_diverge():
    with pytest.raises(ValueError, match="raw close.*disagree"):
        run((replace(buy_first(), marks=(marked(1, price=900),)),))


def test_s4_exit_uses_five_sessions_after_actual_acquisition():
    assert s4_target_exit_session(CAL[1], CAL) == CAL[6]
    assert s4_target_exit_session(CAL[4], CAL) is None
    with pytest.raises(ValueError, match="outside"):
        s4_target_exit_session(date(2024, 1, 6), CAL)


@pytest.mark.parametrize("calendar", [list(CAL), CAL[::-1], (CAL[0], CAL[0], CAL[1]), CAL[:2]])
def test_bad_calendars_rejected(calendar):
    with pytest.raises(ValueError, match="calendar"):
        simulate_research_schedule(ResearchBook(CAL[0], 200000), calendar, (), requested_end=CAL[1])


def test_requires_following_session_and_fresh_initial_book():
    with pytest.raises(ValueError, match="padding"):
        simulate_research_schedule(ResearchBook(CAL[0], 200000), CAL, (), requested_end=CAL[-1])
    prior = run((buy_first(),)).book
    with pytest.raises(ValueError, match="fresh initially flat"):
        simulate_research_schedule(prior, CAL, (), requested_end=CAL[2])


@pytest.mark.parametrize("days", [(batch(2), batch(1)), (batch(1), batch(1))])
def test_duplicate_or_reversed_batches_rejected(days):
    with pytest.raises(ValueError, match="unique and chronological"):
        run(days, end=CAL[2])


def test_batch_outside_window_rejected():
    with pytest.raises(ValueError, match="outside"):
        run((batch(1), batch(3)), end=CAL[2])


@pytest.mark.parametrize("price", [True, 0, -1, 1.1, Decimal(1), None])
def test_raw_mark_requires_positive_integer_fen(price):
    with pytest.raises(ValueError, match="integer fen"):
        marked(1, price=price)


def test_daily_duplicate_and_cross_date_evidence_rejected():
    with pytest.raises(ValueError, match="duplicate attempt"):
        batch(1, (order(1), order(1)))
    with pytest.raises(ValueError, match="splitting"):
        batch(1, (order(1), order(1, identifier="another")))
    with pytest.raises(ValueError, match="duplicate instrument"):
        batch(1, marks=(marked(1), marked(1)))
    with pytest.raises(ValueError, match="different batch date"):
        batch(1, contexts=(context(2),))
    with pytest.raises(ValueError, match="different batch date"):
        batch(1, marks=(marked(2),))
    with pytest.raises(ValueError, match="boolean"):
        batch(1, corporate=1)
    with pytest.raises(ValueError, match="immutable"):
        batch(1, orders=[])


class TestAdvanceResearchDayGuards:
    def _advance(self, book, i, orders=()):
        return advance_research_day(
            book, CAL, i, batch(i, orders, (context(i),), (marked(i),)), set()
        )

    def test_same_day_reentry_rejects_before_capacity_reset(self):
        book = ResearchBook(asof_date=CAL[0], cash_fen=100_000_000)
        first = self._advance(book, 1, (order(1, quantity=1000, identifier="first"),))
        assert first.status == "advanced"
        # Feeding the resulting book into the SAME session again — even with a
        # fresh order id — must reject instead of resetting the day capacity.
        with pytest.raises(ValueError, match="book must sit on the previous session"):
            self._advance(
                first.record.book, 1, (order(1, quantity=1000, identifier="second"),)
            )

    def test_reverse_and_skipped_sessions_reject(self):
        book = ResearchBook(asof_date=CAL[0], cash_fen=100_000_000)
        day1 = self._advance(book, 1)
        assert day1.status == "advanced"
        # Skipping a session: the book sits on CAL[1] but index 3 needs CAL[2].
        with pytest.raises(ValueError, match="book must sit on the previous session"):
            self._advance(day1.record.book, 3)
        # Reversing back to an earlier session after it already completed.
        day2 = self._advance(day1.record.book, 2)
        assert day2.status == "advanced"
        with pytest.raises(ValueError, match="book must sit on the previous session"):
            self._advance(day2.record.book, 1)

    def test_attempted_not_polluted_by_midbatch_exception(self):
        book = ResearchBook(asof_date=CAL[0], cash_fen=100_000_000)
        attempted = set()
        # Different instrument so the batch itself is legal; the oversized
        # second order raises inside the kernel after the first simulated.
        oversized = order(1, code="B", quantity=200_000, identifier="second")
        with pytest.raises(ValueError, match="exceeds single order maximum"):
            advance_research_day(
                book,
                CAL,
                1,
                batch(
                    1,
                    (order(1, identifier="first"), oversized),
                    (context(1), context(1, code="B")),
                    (marked(1), marked(1, code="B")),
                ),
                attempted,
            )
        # The first order simulated before the exception, but the caller's
        # attempted-identity set stays untouched for a clean retry.
        assert attempted == set()
