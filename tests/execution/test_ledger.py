"""Reservation, DAY-binding, and T+1 ledger semantics (v0.2)."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from quantlab.execution import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    ConstraintDecision,
    ConstraintDimension,
    ConstraintsAssessed,
    ConstraintStatus,
    ExecutionLedger,
    ExecutionValidationError,
    FillRecorded,
    LedgerAccountingError,
    LedgerTransitionError,
    OrderCanceled,
    OrderExpired,
    OrderIntended,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderSubmitted,
    OrderType,
    PositionLot,
    PriceBasis,
    Side,
    TimeInForce,
    TradingCalendar,
    account_state_fingerprint,
)

FRIDAY = date(2026, 1, 9)
MONDAY = date(2026, 1, 12)
TUESDAY = date(2026, 1, 13)
START = datetime(2026, 1, 9, 9, 0, tzinfo=EXCHANGE_TIMEZONE)
FEE_CAP_FEN = 500


def _calendar(
    sessions: tuple[date, ...] = (FRIDAY, MONDAY, TUESDAY),
    coverage_end: date = TUESDAY,
) -> TradingCalendar:
    return TradingCalendar(
        sessions=sessions,
        coverage_start=FRIDAY,
        coverage_end=coverage_end,
        source_id="synthetic-calendar",
        source_sha256="c" * 64,
    )


def _initial(*, cash_fen: int = 500_000, lots=()) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="account-1",
        as_of=START,
        cash_fen=cash_fen,
        lots=tuple(lots),
    )


def _intent(
    order_id: str,
    *,
    side: Side = Side.BUY,
    quantity: int = 100,
    created_at: datetime = START,
    trade_date: date = FRIDAY,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instruction_id="rebalance-1",
        instrument_id="000001.SZ",
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        intended_trade_date=trade_date,
        created_at=created_at,
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="raw-bar-close",
        time_in_force=TimeInForce.DAY,
    )


def _decisions(
    assessed_at: datetime,
    *,
    override: dict[ConstraintDimension, ConstraintStatus] | None = None,
    side: Side = Side.BUY,
) -> tuple[ConstraintDecision, ...]:
    override = override or {}
    decisions = []
    for index, dimension in enumerate(ConstraintDimension):
        default = (
            ConstraintStatus.NOT_APPLICABLE
            if dimension is ConstraintDimension.POSITION_SELLABILITY and side is Side.BUY
            else ConstraintStatus.ALLOWED
        )
        decisions.append(
            ConstraintDecision(
                decision_id=f"decision-{index}-{assessed_at.timestamp()}",
                dimension=dimension,
                status=override.get(dimension, default),
                reason_code="synthetic_test",
                message="",
                assessed_at=assessed_at,
                rule_ids=(f"rule-{dimension.value}",),
            )
        )
    return tuple(decisions)


def _request(order_id: str, intent: OrderIntent, created_at: datetime) -> OrderRequest:
    return OrderRequest(
        request_id=f"request-{order_id}",
        order_id=order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        intended_trade_date=intent.intended_trade_date,
        created_at=created_at,
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
        time_in_force=TimeInForce.DAY,
    )


def _validated_order(
    ledger: ExecutionLedger,
    order_id: str,
    *,
    side: Side = Side.BUY,
    quantity: int = 100,
    start: datetime = START,
    trade_date: date = FRIDAY,
    fee_cap_fen: int = FEE_CAP_FEN,
    instrument_id: str = "000001.SZ",
) -> tuple[OrderIntent, datetime]:
    intent = _intent(
        order_id,
        side=side,
        quantity=quantity,
        created_at=start,
        trade_date=trade_date,
    )
    intent = replace(intent, instrument_id=instrument_id)
    ledger.append(OrderIntended(f"event-{order_id}-intent", start, intent))
    assessed_at = start + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            f"event-{order_id}-assessment",
            assessed_at,
            order_id,
            _decisions(assessed_at, side=side),
            account_fingerprint=account_state_fingerprint(
                ledger.snapshot(assessed_at)
            ),
        )
    )
    submitted_at = start + timedelta(minutes=2)
    request = _request(order_id, intent, submitted_at)
    ledger.append(
        OrderSubmitted(
            f"event-{order_id}-submit",
            submitted_at,
            request,
            worst_case_fee_fen=fee_cap_fen if side is Side.BUY else 0,
        )
    )
    return intent, submitted_at


def _fill(
    order_id: str,
    fill_id: str,
    occurred_at: datetime,
    *,
    quantity: int,
    trade_date: date = FRIDAY,
    sellable_from: date | None = MONDAY,
) -> FillRecorded:
    return FillRecorded(
        event_id=f"event-{fill_id}",
        fill_id=fill_id,
        occurred_at=occurred_at,
        order_id=order_id,
        trade_date=trade_date,
        quantity=quantity,
        price=Decimal("10.00"),
        gross_notional_fen=quantity * 1_000,
        fee_fen=500,
        buy_lot_sellable_from=sellable_from,
    )


# ------------------------------------------------- submission eligibility --


def test_fillability_unknown_does_not_block_submission_eligibility() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-fill-unknown")
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            intent.order_id,
            _decisions(
                assessed_at,
                override={ConstraintDimension.FILLABILITY: ConstraintStatus.UNKNOWN},
            ),
        )
    )
    # fill probability/queue position is auditable uncertainty, not a gate
    assert ledger.order(intent.order_id).status is OrderStatus.VALIDATED


def test_fee_access_and_admissibility_unknown_still_block() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-fee-unknown")
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            intent.order_id,
            _decisions(
                assessed_at,
                override={
                    ConstraintDimension.FEE_DETERMINABILITY: ConstraintStatus.UNKNOWN
                },
            ),
        )
    )
    assert ledger.order(intent.order_id).status is OrderStatus.UNKNOWN

    started2 = START + timedelta(minutes=10)
    intent2 = _intent("order-access-unknown", created_at=started2)
    ledger.append(OrderIntended("event-intent-2", started2, intent2))
    assessed2 = started2 + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment-2",
            assessed2,
            intent2.order_id,
            _decisions(
                assessed2,
                override={
                    ConstraintDimension.MARKET_ACCESSIBILITY: ConstraintStatus.UNKNOWN
                },
            ),
        )
    )
    assert ledger.order(intent2.order_id).status is OrderStatus.UNKNOWN

    started3 = START + timedelta(minutes=20)
    intent3 = _intent("order-admissibility-unknown", created_at=started3)
    ledger.append(OrderIntended("event-intent-3", started3, intent3))
    assessed3 = started3 + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment-3",
            assessed3,
            intent3.order_id,
            _decisions(
                assessed3,
                override={
                    ConstraintDimension.ORDER_ADMISSIBILITY: ConstraintStatus.UNKNOWN
                },
            ),
        )
    )
    assert ledger.order(intent3.order_id).status is OrderStatus.UNKNOWN


# ---------------------------------------------------------- reservations --


def test_buy_submission_reserves_worst_case_cash_atomically() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=100_500))
    intent, _ = _validated_order(ledger, "order-buy", fee_cap_fen=500)
    availability = ledger.availability("000001.SZ", FRIDAY)
    assert availability.settled_cash_fen == 100_500
    assert availability.reserved_cash_fen == 100_000 + 500
    assert availability.available_cash_fen == 0
    assert ledger.cash_fen == 100_500  # reservation does not move money


def test_buy_without_fee_cap_cannot_reserve_or_submit() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-nofee")
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            intent.order_id,
            _decisions(assessed_at),
        )
    )
    request = _request("order-nofee", intent, START + timedelta(minutes=2))
    with pytest.raises(LedgerAccountingError, match="fee cap"):
        ledger.append(
            OrderSubmitted("event-submit", request.created_at, request)
        )
    assert ledger.order("order-nofee").status is OrderStatus.VALIDATED
    assert ledger.reserved_cash_fen() == 0


def test_two_buys_contending_for_cash_second_fails_atomically() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=150_000))
    _validated_order(ledger, "order-a")  # reserves 100_500
    first_available = ledger.availability("000001.SZ", FRIDAY)
    assert first_available.available_cash_fen == 49_500

    started_b = START + timedelta(minutes=10)
    intent_b = _intent("order-b", created_at=started_b)
    ledger.append(OrderIntended("event-intent-b", started_b, intent_b))
    assessed_at = started_b + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment-b",
            assessed_at,
            "order-b",
            _decisions(assessed_at),
        )
    )
    request_b = _request("order-b", intent_b, assessed_at + timedelta(minutes=1))
    with pytest.raises(LedgerAccountingError, match="available"):
        ledger.append(
            OrderSubmitted("event-submit-b", request_b.created_at, request_b,
                           worst_case_fee_fen=500)
        )
    # first reservation untouched; second left nothing behind
    assert ledger.availability(
        "000001.SZ", FRIDAY
    ) == first_available
    assert ledger.order("order-b").status is OrderStatus.VALIDATED


def test_batch_reservation_is_all_or_nothing() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=150_000))
    submissions = []
    for index, order_id in enumerate(("order-a", "order-b", "order-c")):
        started = START + timedelta(minutes=10 * index)
        intent = _intent(order_id, created_at=started)
        ledger.append(OrderIntended(f"event-{order_id}-intent", started, intent))
        assessed_at = started + timedelta(minutes=1)
        ledger.append(
            ConstraintsAssessed(
                f"event-{order_id}-assessment",
                assessed_at,
                order_id,
                _decisions(assessed_at),
            )
        )
        submitted = started + timedelta(minutes=2)
        submissions.append(
            (
                OrderSubmitted(
                    f"event-{order_id}-submit",
                    submitted,
                    _request(order_id, intent, submitted),
                ),
                500,
            )
        )
    with pytest.raises(LedgerAccountingError):
        ledger.submit_orders(submissions)
    # nothing from the batch may be reserved or submitted
    assert ledger.reserved_cash_fen() == 0
    for order_id in ("order-a", "order-b", "order-c"):
        assert ledger.order(order_id).status is OrderStatus.VALIDATED
    assert all(
        event.__class__ is not OrderSubmitted for event in ledger.events
    )


def test_two_sells_contending_for_same_sellable_lot() -> None:
    lot = PositionLot("lot-1", "000001.SZ", 100, date(2026, 1, 8), FRIDAY)
    ledger = ExecutionLedger(_initial(cash_fen=0, lots=(lot,)))
    _validated_order(ledger, "sell-a", side=Side.SELL, quantity=100)
    started_b = START + timedelta(minutes=10)
    intent_b = _intent(
        "sell-b", side=Side.SELL, quantity=50, created_at=started_b
    )
    ledger.append(OrderIntended("event-sell-b-intent", started_b, intent_b))
    assessed_at = started_b + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-sell-b-assessment",
            assessed_at,
            "sell-b",
            _decisions(assessed_at, side=Side.SELL),
        )
    )
    request_b = _request("sell-b", intent_b, assessed_at + timedelta(minutes=1))
    with pytest.raises(LedgerAccountingError, match="sellable"):
        ledger.append(OrderSubmitted("event-sell-b-submit", request_b.created_at, request_b))
    assert ledger.reserved_sellable_shares("000001.SZ") == 100
    assert ledger.order("sell-b").status is OrderStatus.UNKNOWN or (
        ledger.order("sell-b").status is OrderStatus.VALIDATED
    )


def test_partial_fill_draws_down_reservation() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=500_000))
    _, submitted_at = _validated_order(ledger, "order-buy", quantity=100)
    before = ledger.availability("000001.SZ", FRIDAY)
    assert before.reserved_cash_fen == 100_500
    ledger.append(
        _fill("order-buy", "fill-1", submitted_at + timedelta(minutes=1), quantity=40)
    )
    after = ledger.availability("000001.SZ", FRIDAY)
    # 40 shares * 1000 fen + 500 fee drawn from the reservation
    assert after.reserved_cash_fen == 100_500 - 40_500
    assert after.available_cash_fen == 459_500 - 60_000
    assert ledger.order("order-buy").status is OrderStatus.PARTIALLY_FILLED
    ledger.append(
        OrderCanceled(
            "event-cancel",
            submitted_at + timedelta(minutes=2),
            "order-buy",
            "synthetic cancel",
        )
    )
    after_cancel = ledger.availability("000001.SZ", FRIDAY)
    assert after_cancel.reserved_cash_fen == 0
    assert after_cancel.available_cash_fen == 500_000 - 40_500


def test_cancel_and_expire_release_remaining_reservation() -> None:
    for terminal in (OrderCanceled, OrderExpired):
        ledger = ExecutionLedger(_initial(cash_fen=500_000))
        order_id = f"order-{terminal.__name__}"
        _, submitted_at = _validated_order(ledger, order_id)
        assert ledger.reserved_cash_fen() == 100_500
        ledger.append(
            terminal(
                f"event-{order_id}",
                submitted_at + timedelta(minutes=1),
                order_id,
                "synthetic terminal",
            )
        )
        assert ledger.reserved_cash_fen() == 0
        assert (
            ledger.availability("000001.SZ", FRIDAY).available_cash_fen == 500_000
        )


def test_event_replay_matches_fresh_run_including_reservations() -> None:
    lot = PositionLot("lot-1", "000001.SZ", 100, date(2026, 1, 8), FRIDAY)
    ledger = ExecutionLedger(_initial(cash_fen=500_000, lots=(lot,)))
    _, submitted_at = _validated_order(ledger, "order-buy")
    ledger.append(
        _fill("order-buy", "fill-1", submitted_at + timedelta(minutes=1), quantity=40)
    )
    _validated_order(
        ledger, "order-sell", side=Side.SELL, start=submitted_at + timedelta(minutes=2)
    )
    replayed = ExecutionLedger.replay(_initial(cash_fen=500_000, lots=(lot,)), ledger.events)
    assert replayed.events == ledger.events
    assert replayed.orders == ledger.orders
    assert replayed.lots == ledger.lots
    assert replayed.cash_fen == ledger.cash_fen
    assert replayed.reservations == ledger.reservations


def test_reused_event_id_with_different_payload_fails_hard() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-1")
    event = OrderIntended("event-shared", START, intent)
    ledger.append(event)
    clone = replace(event, intent=_intent("order-2"))
    with pytest.raises(LedgerTransitionError, match="reused"):
        ledger.append(clone)


# ------------------------------------------------------- DAY + T+1 binds --


def test_request_trade_date_must_match_intent() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-1", trade_date=FRIDAY)
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            "order-1",
            _decisions(assessed_at),
        )
    )
    request = replace(
        _request("order-1", intent, START + timedelta(minutes=2)),
        intended_trade_date=MONDAY,
    )
    with pytest.raises(LedgerTransitionError, match="trade date"):
        ledger.append(OrderSubmitted("event-submit", request.created_at, request))


def test_non_day_time_in_force_is_rejected_at_construction() -> None:
    intent = _intent("order-gtc")
    with pytest.raises(ExecutionValidationError, match="time_in_force"):
        replace(intent, time_in_force=None)
    with pytest.raises(ExecutionValidationError, match="time_in_force"):
        OrderRequest(
            request_id="request-gtc",
            order_id="order-gtc",
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=100,
            order_type=OrderType.LIMIT,
            limit_price=intent.limit_price,
            intended_trade_date=FRIDAY,
            created_at=START,
            limit_price_basis=PriceBasis.RAW,
            limit_price_source_id="raw",
            time_in_force=None,  # type: ignore[arg-type]
        )


def test_day_fill_on_wrong_trade_date_is_rejected_even_if_late_report(
    tmp_path=None,
) -> None:
    ledger = ExecutionLedger(_initial())
    _, submitted_at = _validated_order(ledger, "order-buy", trade_date=FRIDAY)
    late = _fill(
        "order-buy",
        "fill-late",
        datetime(2026, 1, 12, 20, 0, tzinfo=EXCHANGE_TIMEZONE),  # reported Monday
        quantity=100,
        trade_date=MONDAY,  # but claiming Monday's session
    )
    with pytest.raises(LedgerAccountingError, match="trade_date"):
        ledger.append(late)


def test_friday_buy_fill_sellable_exactly_next_monday() -> None:
    calendar = _calendar()
    ledger = ExecutionLedger(_initial(), calendar=calendar)
    _, submitted_at = _validated_order(ledger, "order-buy", trade_date=FRIDAY)
    wrong = _fill(
        "order-buy",
        "fill-wrong",
        submitted_at + timedelta(minutes=1),
        quantity=100,
        sellable_from=TUESDAY,
    )
    with pytest.raises(LedgerAccountingError, match="next session"):
        ledger.append(wrong)
    right = _fill(
        "order-buy",
        "fill-right",
        submitted_at + timedelta(minutes=1),
        quantity=100,
        sellable_from=MONDAY,
    )
    ledger.append(right)
    assert ledger.sellable_quantity("000001.SZ", MONDAY) == 100


def test_holiday_crossing_sellable_follows_calendar() -> None:
    # Thursday session, then Monday (Friday is a holiday)
    thu = date(2026, 1, 8)
    mon = date(2026, 1, 12)
    calendar = TradingCalendar(
        sessions=(thu, mon),
        coverage_start=thu,
        coverage_end=mon,
        source_id="synthetic-calendar-holiday",
        source_sha256="c" * 64,
    )
    started = datetime(2026, 1, 8, 9, 30, tzinfo=EXCHANGE_TIMEZONE)
    initial = AccountSnapshot(
        account_id="account-1", as_of=started, cash_fen=500_000, lots=()
    )
    ledger = ExecutionLedger(initial, calendar=calendar)
    intent = _intent("order-holiday", trade_date=thu, created_at=started)
    ledger.append(OrderIntended("event-intent", started, intent))
    assessed = started + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed,
            "order-holiday",
            _decisions(assessed),
            account_fingerprint=account_state_fingerprint(
                ledger.snapshot(assessed)
            ),
        )
    )
    submitted = started + timedelta(minutes=2)
    request = _request("order-holiday", intent, submitted)
    ledger.append(
        OrderSubmitted(
            "event-submit", submitted, request, worst_case_fee_fen=500
        )
    )
    fill = FillRecorded(
        event_id="event-fill",
        fill_id="fill-1",
        occurred_at=submitted + timedelta(minutes=1),
        order_id="order-holiday",
        trade_date=thu,
        quantity=100,
        price=Decimal("10.00"),
        gross_notional_fen=100_000,
        fee_fen=500,
        buy_lot_sellable_from=mon,  # holiday skipped: Monday is next session
    )
    ledger.append(fill)
    assert ledger.sellable_quantity("000001.SZ", thu) == 0
    assert ledger.sellable_quantity("000001.SZ", mon) == 100


def test_missing_next_session_is_fail_closed() -> None:
    calendar = _calendar(sessions=(FRIDAY,), coverage_end=FRIDAY)
    ledger = ExecutionLedger(_initial(), calendar=calendar)
    _, submitted_at = _validated_order(ledger, "order-buy", trade_date=FRIDAY)
    fill = _fill(
        "order-buy",
        "fill-1",
        submitted_at + timedelta(minutes=1),
        quantity=100,
        sellable_from=MONDAY,
    )
    with pytest.raises(LedgerAccountingError, match="fail-closed|next session"):
        ledger.append(fill)
    assert ledger.position_quantity("000001.SZ") == 0


# ------------------------------------------------------------ stale state --


def test_stale_account_fingerprint_rejects_assessment() -> None:
    ledger = ExecutionLedger(_initial())
    intent_b = _intent("order-b")
    ledger.append(OrderIntended("event-b-intent", START, intent_b))

    fingerprint = account_state_fingerprint(ledger.snapshot(START))
    # a fill on order A changes the settled state...
    _, _ = _validated_order(ledger, "order-a")
    ledger.append(
        _fill("order-a", "fill-a", START + timedelta(minutes=5), quantity=100)
    )
    # ...order B's assessment, taken before the fill, is now stale and must
    # be rejected instead of gating on outdated facts
    with pytest.raises(LedgerTransitionError, match="stale constraint assessment"):
        ledger.append(
            ConstraintsAssessed(
                "event-b-assessment",
                START + timedelta(minutes=6),
                "order-b",
                _decisions(START + timedelta(minutes=6)),
                account_fingerprint=fingerprint,
            )
        )
    assert ledger.order("order-b").status is OrderStatus.INTENDED


def test_fresh_assessment_fingerprint_is_accepted() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-1")
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            "order-1",
            _decisions(assessed_at),
            account_fingerprint=account_state_fingerprint(
                ledger.snapshot(assessed_at)
            ),
        )
    )
    assert ledger.order("order-1").status is OrderStatus.VALIDATED


# ------------------------------------------------- legacy behavior kept --


def test_partial_fill_duplicate_idempotence_and_overfill() -> None:
    ledger = ExecutionLedger(_initial())
    _, submitted_at = _validated_order(ledger, "order-buy", quantity=100)
    first = _fill(
        "order-buy", "fill-1", submitted_at + timedelta(minutes=1), quantity=40
    )
    assert ledger.append(first) is True
    cash_after_first = ledger.cash_fen
    assert ledger.append(first) is False
    assert ledger.cash_fen == cash_after_first
    assert ledger.order("order-buy").filled_quantity == 40

    duplicate_fill = replace(
        _fill(
            "order-buy", "fill-1", submitted_at + timedelta(minutes=2), quantity=40
        ),
        event_id="event-duplicate-fill-id",
    )
    with pytest.raises(LedgerTransitionError, match="duplicate fill_id"):
        ledger.append(duplicate_fill)

    overfill = _fill(
        "order-buy", "fill-over", submitted_at + timedelta(minutes=2), quantity=61
    )
    with pytest.raises(LedgerAccountingError, match="overfill"):
        ledger.append(overfill)
    assert ledger.cash_fen == cash_after_first
    assert ledger.position_quantity("000001.SZ") == 40


def test_illegal_transition_and_request_drift_fail() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-1")
    ledger.append(OrderIntended("event-intent", START, intent))
    with pytest.raises(LedgerTransitionError, match="cannot mark"):
        ledger.append(
            OrderCanceled(
                "event-cancel",
                START + timedelta(minutes=1),
                intent.order_id,
                "not submitted",
            )
        )

    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            intent.order_id,
            _decisions(assessed_at),
        )
    )
    bad_request = OrderRequest(
        request_id="request-bad",
        order_id=intent.order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=200,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        intended_trade_date=intent.intended_trade_date,
        created_at=START + timedelta(minutes=2),
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
    )
    with pytest.raises(LedgerTransitionError, match="differ"):
        ledger.append(OrderSubmitted("event-submit", bad_request.created_at, bad_request))
    assert ledger.order(intent.order_id).status is OrderStatus.VALIDATED


def test_adjusted_price_cannot_bypass_constraint_engine_into_submission() -> None:
    ledger = ExecutionLedger(_initial())
    intent = replace(_intent("order-adjusted"), limit_price_basis=PriceBasis.ADJUSTED)
    ledger.append(OrderIntended("event-intent", START, intent))
    assessed_at = START + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            "event-assessment",
            assessed_at,
            intent.order_id,
            _decisions(assessed_at),
        )
    )
    assert ledger.order(intent.order_id).status is OrderStatus.VALIDATED
    request = _request("order-adjusted", intent, START + timedelta(minutes=2))
    with pytest.raises(LedgerTransitionError, match="raw unadjusted"):
        ledger.append(OrderSubmitted("event-submit", request.created_at, request))


@pytest.mark.parametrize(
    ("terminal_event", "expected"),
    [
        (OrderCanceled, OrderStatus.CANCELED),
        (OrderExpired, OrderStatus.EXPIRED),
    ],
)
def test_submitted_order_can_reach_unfilled_terminal_state(
    terminal_event,
    expected: OrderStatus,
) -> None:
    ledger = ExecutionLedger(_initial())
    order_id = f"order-{expected.value}"
    _, submitted_at = _validated_order(ledger, order_id)
    ledger.append(
        terminal_event(
            f"event-{expected.value}",
            submitted_at + timedelta(minutes=1),
            order_id,
            "synthetic terminal event",
        )
    )
    assert ledger.order(order_id).status is expected
    assert ledger.reserved_cash_fen() == 0


def test_replay_is_deterministic() -> None:
    ledger = ExecutionLedger(_initial())
    _, submitted_at = _validated_order(ledger, "order-buy")
    ledger.append(
        _fill("order-buy", "fill-buy", submitted_at + timedelta(minutes=1), quantity=100)
    )
    replayed = ExecutionLedger.replay(_initial(), ledger.events)
    assert replayed.events == ledger.events
    assert replayed.orders == ledger.orders
    assert replayed.lots == ledger.lots
    assert replayed.cash_fen == ledger.cash_fen
