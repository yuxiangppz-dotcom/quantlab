"""Synthetic accounting cases only; fixtures are not historical rules or fills."""

from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal, localcontext

import pytest

from quantlab.research.quantity_kernel import (
    CapacityUsed,
    ResearchBook,
    ResearchFeeScenario,
    ResearchLot,
    ResearchOrder,
    ResearchQuantityRules,
    ResearchSession,
    simulate_research_order,
)

D = Decimal
DAY = date(2024, 2, 5)
PREV = date(2024, 2, 2)
NEXT = date(2024, 2, 6)
CODE = "synthetic_stock"


@pytest.fixture
def session():
    rules = ResearchQuantityRules(
        "synthetic_main", date(2020, 1, 1), date(2024, 12, 31), 100, 100, 100, 100, 1_000_000, True
    )
    fees = ResearchFeeScenario(
        "synthetic_user_like",
        date(2020, 1, 1),
        date(2024, 12, 31),
        D("0.000086"),
        500,
        D("0"),
        D("0.0005"),
        D("0"),
        0,
        D("0"),
    )
    return ResearchSession(
        CODE,
        DAY,
        NEXT,
        DAY,
        True,
        True,
        True,
        1000,
        900,
        1100,
        800,
        1200,
        100_000_000,
        PREV,
        20,
        100_000_000,
        100_000,
        D("0.05"),
        rules,
        fees,
    )


def order(side="buy", qty=100, identifier="test-order", signal=PREV):
    return ResearchOrder(identifier, CODE, side, qty, signal)


def lot(qty=100, identifier="old-lot", acquired=date(2024, 1, 31), sellable=PREV, code=CODE):
    return ResearchLot(identifier, code, qty, acquired, sellable)


def book(cash=20_000_000, lots=(), asof=PREV):
    return ResearchBook(asof, cash, lots)


def quantity(state, code=CODE):
    return sum(x.quantity for x in state.lots if x.instrument_id == code)


def assert_identity(before, result, side):
    sign = 1 if side == "buy" else -1
    assert result.book.cash_fen == (
        before.cash_fen - sign * result.simulated_notional_fen - result.modeled_fee_fen
    )
    assert quantity(result.book) == quantity(before) + sign * result.simulated_quantity
    assert result.book.cash_fen >= 0
    assert result.execution_authority is False
    assert result.performance_eligible is False


def test_buy_includes_minimum_fee_and_t1_lock(session):
    before = book(cash=200_000)
    result = simulate_research_order(before, order(qty=200), session)
    assert result.simulated_quantity == 100
    assert result.commission_fen == 500
    assert result.book.cash_fen == 99_500
    assert result.book.lots[0].acquired_on == DAY
    assert result.book.lots[0].sellable_on == NEXT
    assert_identity(before, result, "buy")
    assert before == book(cash=200_000)


def test_exact_cash_boundary(session):
    for cash, qty in ((100_499, 0), (100_500, 100), (200_499, 100), (200_500, 200)):
        before = book(cash=cash)
        result = simulate_research_order(before, order(qty=200), session)
        assert result.simulated_quantity == qty
        assert_identity(before, result, "buy")


def test_locked_sale_does_not_fund_another_buy(session):
    before = book(cash=500, lots=(lot(acquired=DAY, sellable=NEXT),), asof=DAY)
    sale = simulate_research_order(before, order("sell"), session)
    assert sale.reason == "t1_locked" and sale.book is before
    purchase = simulate_research_order(sale.book, order(identifier="buy-second"), session)
    assert purchase.status == "blocked"
    assert purchase.book.cash_fen == 500 and quantity(purchase.book) == 100


def test_completed_simulated_sale_can_fund_later_order(session):
    before = book(cash=0, lots=(lot(qty=200),))
    sold = simulate_research_order(before, order("sell", 200), session)
    assert sold.book.cash_fen == 199_400
    bought = simulate_research_order(sold.book, order(qty=200, identifier="buy-again"), session)
    assert bought.simulated_quantity == 100 and bought.book.cash_fen == 98_900
    assert_identity(before, sold, "sell")
    assert_identity(sold.book, bought, "buy")


def test_partial_sale_preserves_locked_and_other_instrument_lots(session):
    before = book(
        lots=(
            lot(qty=100),
            lot(qty=100, identifier="locked", acquired=DAY, sellable=NEXT),
            lot(identifier="other", code="elsewhere"),
        ),
        asof=DAY,
    )
    result = simulate_research_order(before, order("sell", 200), session)
    assert result.simulated_quantity == 100
    assert {x.lot_id for x in result.book.lots} == {"locked", "other"}
    assert_identity(before, result, "sell")


def test_fifo_eligible_lots_and_partial_remainder(session):
    before = book(lots=(lot(200, "later", acquired=date(2024, 2, 1)), lot(100, "first")))
    result = simulate_research_order(before, order("sell", 200), session)
    assert [(x.lot_id, x.quantity) for x in result.book.lots] == [("later", 100)]


def test_main_odd_full_exit_and_capacity_grid(session):
    before = book(lots=(lot(155),))
    full = simulate_research_order(before, order("sell", 155), session)
    assert full.simulated_quantity == 155 and full.book.lots == ()
    capped = replace(session, session_volume_shares=2400)  # 5% => 120 shares
    partial = simulate_research_order(before, order("sell", 155), capped)
    assert partial.simulated_quantity == 100 and quantity(partial.book) == 55
    tail = simulate_research_order(book(lots=(lot(55),)), order("sell", 55), session)
    assert tail.simulated_quantity == 55


def test_locked_odd_remainder_cannot_be_included_in_full_exit(session):
    before = book(lots=(lot(55), lot(100, "locked", acquired=DAY, sellable=NEXT)), asof=DAY)
    result = simulate_research_order(before, order("sell", 155), session)
    assert result.status == "blocked" and result.book == before


def test_explicit_odd_exit_disabled(session):
    session = replace(session, rules=replace(session.rules, full_position_odd_exit=False))
    result = simulate_research_order(book(lots=(lot(55),)), order("sell", 55), session)
    assert result.simulated_quantity == 0


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_single_order_maximum_is_not_bypassed_by_capacity_sizing(session, side):
    s = replace(session, rules=replace(session.rules, max_order_quantity=200))
    with pytest.raises(ValueError, match="single order maximum"):
        simulate_research_order(book(lots=(lot(300),)), order(side, 300), s)


def test_single_order_maximum_must_accommodate_minimum(session):
    with pytest.raises(ValueError, match="below a minimum"):
        replace(session.rules, max_order_quantity=99)


def test_star_style_minimum_and_single_share_steps_are_fixture_only(session):
    star = replace(
        session,
        rules=replace(
            session.rules,
            scenario_id="synthetic_star",
            buy_minimum=200,
            buy_increment=1,
            sell_minimum=200,
            sell_increment=1,
        ),
    )
    assert simulate_research_order(book(), order(qty=199), star).status == "blocked"
    bought = simulate_research_order(book(), order(qty=201), star)
    assert bought.simulated_quantity == 201
    sold = simulate_research_order(book(lots=(lot(250),)), order("sell", 201), star)
    assert sold.simulated_quantity == 201 and quantity(sold.book) == 49
    assert (
        simulate_research_order(book(lots=(lot(155),)), order("sell", 155), star).simulated_quantity
        == 155
    )


@pytest.mark.parametrize(
    "name",
    [
        "next_session",
        "evidence_date",
        "calendar_verified",
        "market_open",
        "corporate_actions_processed",
        "raw_close_fen",
        "low_fen",
        "high_fen",
        "down_limit_fen",
        "up_limit_fen",
        "prior20_amount_fen",
        "prior20_asof",
        "prior20_sessions",
        "session_amount_fen",
        "session_volume_shares",
        "participation",
        "rules",
        "fees",
    ],
)
def test_unknown_input_does_not_manufacture_sale_cash(session, name):
    before = book(lots=(lot(),))
    result = simulate_research_order(before, order("sell"), replace(session, **{name: None}))
    assert result.book is before and result.status == "blocked"
    assert result.reason.endswith("_unknown")


@pytest.mark.parametrize(
    "name", ["calendar_verified", "market_open", "corporate_actions_processed"]
)
def test_known_false_evidence_is_separate_from_unknown(session, name):
    before = book(lots=(lot(),))
    result = simulate_research_order(before, order("sell"), replace(session, **{name: False}))
    assert result.book is before and result.reason == name + "_false"


@pytest.mark.parametrize(
    "name",
    [
        "commission_rate",
        "minimum_commission_fen",
        "buy_stamp_rate",
        "sell_stamp_rate",
        "additional_fee_rate",
        "additional_fee_fixed_fen",
        "adverse_slippage_rate",
    ],
)
def test_partial_cost_components_cannot_be_used_as_complete_fee(session, name):
    uncertain = replace(session, fees=replace(session.fees, **{name: None}))
    result = simulate_research_order(book(), order(), uncertain)
    assert result.reason == "fee_components_unknown"


@pytest.mark.parametrize(
    "side,changes",
    [
        ("buy", {"raw_close_fen": 1200, "high_fen": 1200}),
        ("sell", {"raw_close_fen": 800, "low_fen": 800}),
    ],
)
def test_directional_limit_rejects_and_keeps_positions(session, side, changes):
    before = book(lots=(lot(),))
    result = simulate_research_order(before, order(side), replace(session, **changes))
    assert result.reason == "directional_close_limit" and result.book is before


def test_limit_on_opposite_side_is_not_claimed_as_a_real_fill(session):
    s = replace(session, raw_close_fen=800, low_fen=800)
    result = simulate_research_order(book(), order(), s)
    assert result.status == "simulated" and result.execution_authority is False


def test_adverse_price_rounding_and_range_check(session):
    s = replace(session, fees=replace(session.fees, adverse_slippage_rate=D("0.0001")))
    buy = simulate_research_order(book(), order(), s)
    sell = simulate_research_order(book(lots=(lot(),)), order("sell"), s)
    assert buy.modeled_price_fen == 1001 and sell.modeled_price_fen == 999
    assert buy.simulated_notional_fen == 100100 and sell.simulated_notional_fen == 99900
    narrow = replace(s, high_fen=1000)
    result = simulate_research_order(book(), order(), narrow)
    assert result.reason == "modeled_price_outside_bar_or_limits"


def test_capacity_binds_prior_amount_actual_amount_and_actual_shares(session):
    for field, value in (
        ("prior20_amount_fen", 2_400_000),
        ("session_amount_fen", 2_400_000),
        ("session_volume_shares", 2400),
    ):
        result = simulate_research_order(book(), order(qty=500), replace(session, **{field: value}))
        assert result.simulated_quantity == 100


@pytest.mark.parametrize(
    "name", ["prior20_amount_fen", "session_amount_fen", "session_volume_shares"]
)
def test_known_zero_capacity_blocks_without_guessing(session, name):
    result = simulate_research_order(book(), order(), replace(session, **{name: 0}))
    assert result.reason == "capacity_exhausted"


def test_split_requests_share_day_capacity_and_cannot_change_scenario(session):
    s = replace(session, session_volume_shares=5000)  # 250-share total capacity
    first = simulate_research_order(book(), order(qty=200), s)
    second = simulate_research_order(first.book, order(qty=100, identifier="second"), s)
    assert second.simulated_quantity == 0 and quantity(second.book) == 200
    assert first.book.capacity_used[0].quantity == 200
    with pytest.raises(ValueError, match="assumptions changed"):
        simulate_research_order(
            first.book, order(identifier="second"), replace(s, participation=D("0.1"))
        )


def test_buy_and_sell_both_consume_same_capacity(session):
    s = replace(session, session_volume_shares=6000)  # 300-share total capacity
    first = simulate_research_order(book(lots=(lot(200),)), order("sell", 200), s)
    second = simulate_research_order(first.book, order(qty=200, identifier="purchase"), s)
    assert second.simulated_quantity == 100
    assert second.book.capacity_used[0].quantity == 300


def test_next_session_releases_lot_and_uses_new_capacity_bucket(session):
    s = replace(session, session_volume_shares=2000)
    bought = simulate_research_order(book(), order(), s)
    future = replace(
        s, execution_date=NEXT, evidence_date=NEXT, next_session=date(2024, 2, 7), prior20_asof=DAY
    )
    sold = simulate_research_order(
        bought.book, order("sell", signal=DAY, identifier="next"), future
    )
    assert sold.simulated_quantity == 100
    assert len(sold.book.capacity_used) == 2


def test_cost_components_aggregate_and_round_separately(session):
    fees = replace(
        session.fees,
        commission_rate=D("0.000005"),
        minimum_commission_fen=0,
        sell_stamp_rate=D("0.000005"),
        additional_fee_rate=D("0.000005"),
        additional_fee_fixed_fen=2,
    )
    result = simulate_research_order(
        book(lots=(lot(),)), order("sell"), replace(session, fees=fees)
    )
    assert (result.commission_fen, result.stamp_fen, result.additional_fee_fen) == (1, 1, 3)
    assert result.modeled_fee_fen == 5


def test_large_notional_commission_exceeds_minimum(session):
    s = replace(session, participation=D("1"))
    result = simulate_research_order(book(), order(qty=10_000), s)
    assert result.commission_fen == 860 and result.simulated_quantity == 10_000


def test_additional_declared_fee_reduces_affordable_size(session):
    s = replace(session, fees=replace(session.fees, additional_fee_fixed_fen=1))
    result = simulate_research_order(book(cash=100_500), order(), s)
    assert result.status == "blocked"


def test_tiny_odd_exit_cannot_create_negative_cash(session):
    before = book(cash=0, lots=(lot(1),))
    s = replace(session, raw_close_fen=1, low_fen=1, high_fen=2, down_limit_fen=1, up_limit_fen=2)
    # Avoid the lower-limit block to isolate fee coverage with a 2-fen quote.
    s = replace(s, raw_close_fen=2, high_fen=3, up_limit_fen=3)
    result = simulate_research_order(before, order("sell", 1), s)
    assert result.reason == "sell_proceeds_cannot_cover_modeled_fees" and result.book is before


def test_duplicate_successful_order_is_error(session):
    first = simulate_research_order(book(), order(), session)
    with pytest.raises(ValueError, match="duplicate simulated order"):
        simulate_research_order(first.book, order(), session)


def test_session_evidence_cannot_price_another_instrument(session):
    with pytest.raises(ValueError, match="another instrument"):
        simulate_research_order(book(), order(), replace(session, instrument_id="other_stock"))


def test_serialized_transition_keeps_research_boundary_flags(session):
    result = simulate_research_order(book(), order(), session)
    serialized = asdict(result)
    assert serialized["scope"] == "hypothetical_research_transition_only"
    assert serialized["execution_authority"] is False
    assert serialized["performance_eligible"] is False
    with pytest.raises(ValueError):
        replace(result, execution_authority=True)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_signal_must_precede_execution(session, side):
    with pytest.raises(ValueError, match="chronology"):
        simulate_research_order(book(), order(side, signal=DAY), session)


def test_future_capacity_input_and_book_rejected(session):
    with pytest.raises(ValueError, match="future information"):
        simulate_research_order(book(), order(), replace(session, prior20_asof=DAY))
    with pytest.raises(ValueError, match="chronology"):
        simulate_research_order(book(asof=NEXT), order(), session)
    with pytest.raises(ValueError, match="next session"):
        simulate_research_order(book(), order(), replace(session, next_session=DAY))


def test_stale_evidence_and_insufficient_history_block(session):
    assert (
        simulate_research_order(book(), order(), replace(session, evidence_date=PREV)).reason
        == "evidence_date_mismatch"
    )
    assert (
        simulate_research_order(book(), order(), replace(session, prior20_sessions=19)).reason
        == "prior20_coverage_incomplete"
    )


@pytest.mark.parametrize("name", ["rules", "fees"])
def test_rule_or_fee_date_scope_does_not_expand_silently(session, name):
    scoped = replace(getattr(session, name), effective_from=NEXT)
    result = simulate_research_order(book(), order(), replace(session, **{name: scoped}))
    assert result.reason == name + "_outside_scope"


@pytest.mark.parametrize("bad", [True, 0, -1, 100.0, "100", 10**16])
def test_invalid_desired_quantity_rejected(bad):
    with pytest.raises(ValueError):
        order(qty=bad)


@pytest.mark.parametrize("bad", [True, -1, float("nan"), "0", 10**16])
def test_invalid_cash_rejected(bad):
    with pytest.raises(ValueError):
        book(cash=bad)


@pytest.mark.parametrize("bad", [D("NaN"), D("Infinity"), D("-0.1"), D("1.01"), 0.05, True])
def test_invalid_rates_rejected(session, bad):
    with pytest.raises(ValueError):
        replace(session, participation=bad)
    with pytest.raises(ValueError):
        replace(session.fees, commission_rate=bad)


def test_invalid_dates_lots_and_state_rejected(session):
    with pytest.raises(ValueError):
        order(signal=datetime(2024, 2, 2))
    with pytest.raises(ValueError, match="locked"):
        lot(acquired=DAY, sellable=DAY)
    with pytest.raises(ValueError, match="duplicate lot"):
        book(lots=(lot(), lot()))
    with pytest.raises(ValueError, match="future acquisition"):
        book(lots=(lot(acquired=DAY, sellable=NEXT),))
    with pytest.raises(ValueError, match="reversed"):
        replace(session.rules, effective_from=date(2025, 1, 1))
    with pytest.raises(ValueError, match="inconsistent raw bar"):
        simulate_research_order(book(), order(), replace(session, low_fen=1001))
    with pytest.raises(ValueError, match="boolean"):
        replace(session, market_open="yes")


def test_capacity_records_are_unique_dated_and_bound():
    usage = CapacityUsed(CODE, PREV, 100, 100_000, "a" * 64)
    with pytest.raises(ValueError, match="duplicate instrument"):
        ResearchBook(PREV, 0, (), (usage, usage))
    with pytest.raises(ValueError, match="future capacity"):
        ResearchBook(date(2024, 2, 1), 0, (), (usage,))
    with pytest.raises(ValueError, match="bind"):
        replace(usage, session_fingerprint="")


def test_decimal_global_precision_does_not_change_accounting(session):
    before = book()
    expected = simulate_research_order(before, order(qty=2000), session)
    with localcontext() as context:
        context.prec = 4
        actual = simulate_research_order(before, order(qty=2000), session)
    assert actual == expected


def test_cash_sizing_matches_exhaustive_legal_grid(session):
    # Independent exhaustive check across minima and percent-fee transitions.
    fees = replace(
        session.fees,
        commission_rate=D("0.003"),
        minimum_commission_fen=500,
        additional_fee_fixed_fen=7,
    )
    s = replace(session, fees=fees)
    for cash in (0, 100_499, 100_507, 200_500, 300_907, 1_001_000, 5_005_000):
        legal = [0]
        for q in range(100, 2100, 100):
            commission = max(500, (q * 1000 * 3 + 500) // 1000)
            if q * 1000 + commission + 7 <= cash:
                legal.append(q)
        result = simulate_research_order(book(cash=cash), order(qty=2000), s)
        assert result.simulated_quantity == max(legal)
        assert_identity(book(cash=cash), result, "buy")
