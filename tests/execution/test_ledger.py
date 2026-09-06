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
)

FRIDAY = date(2026, 1, 9)
MONDAY = date(2026, 1, 12)
START = datetime(2026, 1, 9, 9, 0, tzinfo=EXCHANGE_TIMEZONE)


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


def _validated_order(
    ledger: ExecutionLedger,
    order_id: str,
    *,
    side: Side = Side.BUY,
    quantity: int = 100,
    start: datetime = START,
    trade_date: date = FRIDAY,
) -> tuple[OrderIntent, datetime]:
    intent = _intent(
        order_id,
        side=side,
        quantity=quantity,
        created_at=start,
        trade_date=trade_date,
    )
    ledger.append(OrderIntended(f"event-{order_id}-intent", start, intent))
    assessed_at = start + timedelta(minutes=1)
    ledger.append(
        ConstraintsAssessed(
            f"event-{order_id}-assessment",
            assessed_at,
            order_id,
            _decisions(assessed_at, side=side),
        )
    )
    submitted_at = start + timedelta(minutes=2)
    request = OrderRequest(
        request_id=f"request-{order_id}",
        order_id=order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        created_at=submitted_at,
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
    )
    ledger.append(OrderSubmitted(f"event-{order_id}-submit", submitted_at, request))
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


def test_unknown_constraint_can_never_be_submitted() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-unknown")
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
    assert ledger.order(intent.order_id).status is OrderStatus.UNKNOWN
    request = OrderRequest(
        request_id="request-1",
        order_id=intent.order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        created_at=START + timedelta(minutes=2),
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
    )
    with pytest.raises(LedgerTransitionError, match="from unknown"):
        ledger.append(OrderSubmitted("event-submit", request.created_at, request))


def test_rejection_takes_precedence_and_is_terminal() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-rejected")
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
                    ConstraintDimension.ORDER_ADMISSIBILITY:
                    ConstraintStatus.REJECTED,
                    ConstraintDimension.FILLABILITY: ConstraintStatus.UNKNOWN,
                },
            ),
        )
    )
    assert ledger.order(intent.order_id).status is OrderStatus.REJECTED
    with pytest.raises(LedgerTransitionError, match="cannot assess"):
        ledger.append(
            ConstraintsAssessed(
                "event-reassess",
                assessed_at + timedelta(minutes=1),
                intent.order_id,
                _decisions(assessed_at + timedelta(minutes=1)),
            )
        )


def test_not_applicable_mandatory_dimension_is_fail_closed() -> None:
    ledger = ExecutionLedger(_initial())
    intent = _intent("order-na")
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
                    ConstraintDimension.FEE_DETERMINABILITY:
                    ConstraintStatus.NOT_APPLICABLE
                },
            ),
        )
    )
    assert ledger.order(intent.order_id).status is OrderStatus.UNKNOWN


def test_partial_fill_duplicate_idempotence_and_overfill() -> None:
    ledger = ExecutionLedger(_initial())
    _, submitted_at = _validated_order(ledger, "order-buy", quantity=100)
    first = _fill(
        "order-buy",
        "fill-1",
        submitted_at + timedelta(minutes=1),
        quantity=40,
    )
    assert ledger.append(first) is True
    cash_after_first = ledger.cash_fen
    assert ledger.append(first) is False
    assert ledger.cash_fen == cash_after_first
    assert ledger.order("order-buy").filled_quantity == 40

    duplicate_fill = replace(
        _fill(
            "order-buy",
            "fill-1",
            submitted_at + timedelta(minutes=2),
            quantity=40,
        ),
        event_id="event-duplicate-fill-id",
    )
    with pytest.raises(LedgerTransitionError, match="duplicate fill_id"):
        ledger.append(duplicate_fill)

    overfill = _fill(
        "order-buy",
        "fill-over",
        submitted_at + timedelta(minutes=2),
        quantity=61,
    )
    with pytest.raises(LedgerAccountingError, match="overfill"):
        ledger.append(overfill)
    assert ledger.cash_fen == cash_after_first
    assert ledger.position_quantity("000001.SZ") == 40


def test_insufficient_cash_fill_is_atomic() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=50_000))
    _, submitted_at = _validated_order(ledger, "order-buy")
    fill = _fill(
        "order-buy",
        "fill-buy",
        submitted_at + timedelta(minutes=1),
        quantity=100,
    )
    with pytest.raises(LedgerAccountingError, match="insufficient cash"):
        ledger.append(fill)
    assert ledger.cash_fen == 50_000
    assert ledger.position_quantity("000001.SZ") == 0
    assert ledger.order("order-buy").status is OrderStatus.SUBMITTED


def test_buy_cash_and_t_plus_one_lot_across_weekend() -> None:
    ledger = ExecutionLedger(_initial(cash_fen=250_000))
    _, submitted_at = _validated_order(ledger, "order-buy")
    buy_fill = _fill(
        "order-buy",
        "fill-buy",
        submitted_at + timedelta(minutes=1),
        quantity=100,
    )
    ledger.append(buy_fill)
    assert ledger.cash_fen == 149_500
    assert ledger.sellable_quantity("000001.SZ", FRIDAY) == 0
    assert ledger.sellable_quantity("000001.SZ", MONDAY) == 100

    sell_start = submitted_at + timedelta(minutes=2)
    _, sell_submitted = _validated_order(
        ledger,
        "order-sell",
        side=Side.SELL,
        start=sell_start,
    )
    friday_sell = _fill(
        "order-sell",
        "fill-sell",
        sell_submitted + timedelta(minutes=1),
        quantity=100,
        sellable_from=None,
    )
    with pytest.raises(LedgerAccountingError, match=r"T\+1"):
        ledger.append(friday_sell)
    assert ledger.cash_fen == 149_500

    monday_time = datetime(2026, 1, 12, 9, 31, tzinfo=EXCHANGE_TIMEZONE)
    monday_sell = _fill(
        "order-sell",
        "fill-sell",
        monday_time,
        quantity=100,
        trade_date=MONDAY,
        sellable_from=None,
    )
    ledger.append(monday_sell)
    assert ledger.position_quantity("000001.SZ") == 0
    assert ledger.cash_fen == 249_000


def test_oversell_is_rejected_without_partial_mutation() -> None:
    lot = PositionLot("lot-1", "000001.SZ", 50, date(2026, 1, 8), FRIDAY)
    ledger = ExecutionLedger(_initial(cash_fen=0, lots=(lot,)))
    _, submitted_at = _validated_order(
        ledger,
        "order-sell",
        side=Side.SELL,
        quantity=100,
    )
    fill = _fill(
        "order-sell",
        "fill-sell",
        submitted_at + timedelta(minutes=1),
        quantity=100,
        sellable_from=None,
    )
    with pytest.raises(LedgerAccountingError, match="sellable 50"):
        ledger.append(fill)
    assert ledger.cash_fen == 0
    assert ledger.position_quantity("000001.SZ") == 50
    assert ledger.order("order-sell").filled_quantity == 0


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
        "request-bad",
        intent.order_id,
        intent.instrument_id,
        intent.side,
        200,
        intent.order_type,
        intent.limit_price,
        START + timedelta(minutes=2),
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
    request = OrderRequest(
        request_id="request-adjusted",
        order_id=intent.order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        created_at=START + timedelta(minutes=2),
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
    )
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
    _, submitted_at = _validated_order(ledger, f"order-{expected.value}")
    ledger.append(
        terminal_event(
            f"event-{expected.value}",
            submitted_at + timedelta(minutes=1),
            f"order-{expected.value}",
            "synthetic terminal event",
        )
    )
    assert ledger.order(f"order-{expected.value}").status is expected


def test_fill_notional_must_exactly_match_decimal_price() -> None:
    with pytest.raises(ExecutionValidationError, match="disagrees"):
        FillRecorded(
            event_id="event-fill",
            fill_id="fill-1",
            occurred_at=START,
            order_id="order-1",
            trade_date=FRIDAY,
            quantity=100,
            price=Decimal("10.00"),
            gross_notional_fen=99_999,
            fee_fen=0,
            buy_lot_sellable_from=MONDAY,
        )
    with pytest.raises(ExecutionValidationError, match="fractional fen"):
        FillRecorded(
            event_id="event-fill",
            fill_id="fill-1",
            occurred_at=START,
            order_id="order-1",
            trade_date=FRIDAY,
            quantity=1,
            price=Decimal("10.001"),
            gross_notional_fen=1_000,
            fee_fen=0,
            buy_lot_sellable_from=MONDAY,
        )


def test_replay_is_deterministic() -> None:
    ledger = ExecutionLedger(_initial())
    _, submitted_at = _validated_order(ledger, "order-buy")
    ledger.append(
        _fill(
            "order-buy",
            "fill-buy",
            submitted_at + timedelta(minutes=1),
            quantity=100,
        )
    )

    replayed = ExecutionLedger.replay(_initial(), ledger.events)

    assert replayed.events == ledger.events
    assert replayed.orders == ledger.orders
    assert replayed.lots == ledger.lots
    assert replayed.cash_fen == ledger.cash_fen
