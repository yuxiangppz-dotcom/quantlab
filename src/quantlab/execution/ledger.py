"""Append-only, deterministic order and cash/share ledger."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal

from quantlab.execution.models import (
    AccountSnapshot,
    ConstraintDecision,
    ConstraintDimension,
    ExecutionValidationError,
    OrderIntent,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionLot,
    PriceBasis,
    Side,
    derive_order_status,
    exchange_date,
    require_aware,
    require_decimal,
    require_identifier,
    require_int,
)


class LedgerError(RuntimeError):
    """Base class for deterministic ledger failures."""


class LedgerTransitionError(LedgerError):
    """An event is illegal from the order's current state."""


class LedgerAccountingError(LedgerError):
    """An event would violate cash, share, or fill accounting."""


@dataclass(frozen=True)
class OrderIntended:
    event_id: str
    occurred_at: datetime
    intent: OrderIntent

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_aware(self.occurred_at, "occurred_at")
        if self.occurred_at != self.intent.created_at:
            raise ExecutionValidationError("intent event time must equal intent.created_at")


@dataclass(frozen=True)
class ConstraintsAssessed:
    event_id: str
    occurred_at: datetime
    order_id: str
    decisions: tuple[ConstraintDecision, ...]

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.order_id, "order_id")
        require_aware(self.occurred_at, "occurred_at")
        dimensions = [decision.dimension for decision in self.decisions]
        if len(dimensions) != len(set(dimensions)):
            raise ExecutionValidationError("duplicate constraint dimension")
        if set(dimensions) != set(ConstraintDimension):
            missing = sorted(d.value for d in set(ConstraintDimension) - set(dimensions))
            extra = sorted(d.value for d in set(dimensions) - set(ConstraintDimension))
            raise ExecutionValidationError(
                f"constraint assessment must cover all dimensions; "
                f"missing={missing} extra={extra}"
            )
        if any(decision.assessed_at > self.occurred_at for decision in self.decisions):
            raise ExecutionValidationError("decision timestamp exceeds assessment event")


@dataclass(frozen=True)
class OrderSubmitted:
    event_id: str
    occurred_at: datetime
    request: OrderRequest

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_aware(self.occurred_at, "occurred_at")
        if self.occurred_at != self.request.created_at:
            raise ExecutionValidationError("submit event time must equal request.created_at")


@dataclass(frozen=True)
class FillRecorded:
    """Broker-reported fill with independently explicit exact money fields.

    ``gross_notional_fen`` and ``fee_fen`` drive cash accounting. ``price``
    is retained as Decimal evidence; the ledger does not invent a rounding
    rule or infer a fill from daily OHLCV.
    """

    event_id: str
    fill_id: str
    occurred_at: datetime
    order_id: str
    trade_date: date
    quantity: int
    price: Decimal
    gross_notional_fen: int
    fee_fen: int
    buy_lot_sellable_from: date | None = None

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.fill_id, "fill_id")
        require_identifier(self.order_id, "order_id")
        require_aware(self.occurred_at, "occurred_at")
        require_int(self.quantity, "quantity", minimum=1)
        require_decimal(self.price, "price", positive=True)
        require_int(self.gross_notional_fen, "gross_notional_fen", minimum=1)
        require_int(self.fee_fen, "fee_fen")
        if exchange_date(self.occurred_at) < self.trade_date:
            raise ExecutionValidationError("fill reported before its exchange trade_date")
        exact_fen = self.price * self.quantity * 100
        if exact_fen != exact_fen.to_integral_value():
            raise ExecutionValidationError(
                "fill price and quantity produce fractional fen; explicit "
                "rounding policy is required"
            )
        if int(exact_fen) != self.gross_notional_fen:
            raise ExecutionValidationError(
                "gross_notional_fen disagrees with Decimal price * quantity"
            )


@dataclass(frozen=True)
class OrderCanceled:
    event_id: str
    occurred_at: datetime
    order_id: str
    reason: str

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.order_id, "order_id")
        require_identifier(self.reason, "reason")
        require_aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True)
class OrderExpired:
    event_id: str
    occurred_at: datetime
    order_id: str
    reason: str

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.order_id, "order_id")
        require_identifier(self.reason, "reason")
        require_aware(self.occurred_at, "occurred_at")


LedgerEvent = (
    OrderIntended
    | ConstraintsAssessed
    | OrderSubmitted
    | FillRecorded
    | OrderCanceled
    | OrderExpired
)


@dataclass(frozen=True)
class OrderLedgerState:
    intent: OrderIntent
    status: OrderStatus
    filled_quantity: int = 0
    gross_notional_fen: int = 0
    fee_fen: int = 0
    request_id: str | None = None

    @property
    def remaining_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity


class ExecutionLedger:
    """Append-only event ledger with atomic validation-before-mutation."""

    def __init__(self, initial: AccountSnapshot) -> None:
        self._initial = initial
        self._cash_fen = initial.cash_fen
        self._lots = list(initial.lots)
        self._orders: dict[str, OrderLedgerState] = {}
        self._events: list[LedgerEvent] = []
        self._events_by_id: dict[str, LedgerEvent] = {}
        self._fill_ids: set[str] = set()
        self._request_ids: set[str] = set()

    @classmethod
    def replay(
        cls,
        initial: AccountSnapshot,
        events: tuple[LedgerEvent, ...] | list[LedgerEvent],
    ) -> ExecutionLedger:
        ledger = cls(initial)
        for event in events:
            ledger.append(event)
        return ledger

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    @property
    def cash_fen(self) -> int:
        return self._cash_fen

    @property
    def orders(self) -> tuple[OrderLedgerState, ...]:
        return tuple(self._orders[key] for key in sorted(self._orders))

    @property
    def lots(self) -> tuple[PositionLot, ...]:
        return tuple(
            sorted(
                self._lots,
                key=lambda lot: (
                    lot.instrument_id,
                    lot.sellable_from,
                    lot.acquired_trade_date,
                    lot.lot_id,
                ),
            )
        )

    def order(self, order_id: str) -> OrderLedgerState:
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise LedgerTransitionError(f"unknown order_id: {order_id}") from exc

    def position_quantity(self, instrument_id: str) -> int:
        return sum(lot.quantity for lot in self._lots if lot.instrument_id == instrument_id)

    def sellable_quantity(self, instrument_id: str, trade_date: date) -> int:
        return sum(
            lot.quantity
            for lot in self._lots
            if lot.instrument_id == instrument_id and lot.sellable_from <= trade_date
        )

    def snapshot(self, as_of: datetime) -> AccountSnapshot:
        require_aware(as_of, "as_of")
        if self._events and as_of < self._events[-1].occurred_at:
            raise LedgerTransitionError("cannot snapshot before the latest ledger event")
        return AccountSnapshot(
            account_id=self._initial.account_id,
            as_of=as_of,
            cash_fen=self._cash_fen,
            lots=self.lots,
        )

    def append(self, event: LedgerEvent) -> bool:
        """Append one event; exact duplicate event ids are idempotent."""
        previous = self._events_by_id.get(event.event_id)
        if previous is not None:
            if previous == event:
                return False
            raise LedgerTransitionError(
                f"event_id {event.event_id!r} reused with different payload"
            )
        if event.occurred_at < self._initial.as_of:
            raise LedgerTransitionError("event precedes initial account snapshot")
        if self._events and event.occurred_at < self._events[-1].occurred_at:
            raise LedgerTransitionError("events must be appended in timestamp order")

        if isinstance(event, OrderIntended):
            self._apply_intended(event)
        elif isinstance(event, ConstraintsAssessed):
            self._apply_assessment(event)
        elif isinstance(event, OrderSubmitted):
            self._apply_submitted(event)
        elif isinstance(event, FillRecorded):
            self._apply_fill(event)
        elif isinstance(event, OrderCanceled):
            self._apply_terminal(event.order_id, OrderStatus.CANCELED)
        elif isinstance(event, OrderExpired):
            self._apply_terminal(event.order_id, OrderStatus.EXPIRED)
        else:  # pragma: no cover - closed union and defensive runtime guard
            raise TypeError(f"unsupported ledger event: {type(event).__name__}")

        self._events.append(event)
        self._events_by_id[event.event_id] = event
        return True

    def _apply_intended(self, event: OrderIntended) -> None:
        order_id = event.intent.order_id
        if order_id in self._orders:
            raise LedgerTransitionError(f"duplicate order_id: {order_id}")
        self._orders[order_id] = OrderLedgerState(
            intent=event.intent,
            status=OrderStatus.INTENDED,
        )

    def _apply_assessment(self, event: ConstraintsAssessed) -> None:
        state = self.order(event.order_id)
        if state.status is not OrderStatus.INTENDED:
            raise LedgerTransitionError(
                f"cannot assess order {event.order_id} from {state.status.value}"
            )
        next_status = derive_order_status(state.intent.side, event.decisions)
        self._orders[event.order_id] = replace(state, status=next_status)

    def _apply_submitted(self, event: OrderSubmitted) -> None:
        request = event.request
        state = self.order(request.order_id)
        if state.status is not OrderStatus.VALIDATED:
            raise LedgerTransitionError(
                f"cannot submit order {request.order_id} from {state.status.value}"
            )
        if request.request_id in self._request_ids:
            raise LedgerTransitionError(f"duplicate request_id: {request.request_id}")
        intent = state.intent
        if (
            intent.order_type is OrderType.LIMIT
            and intent.limit_price_basis is not PriceBasis.RAW
        ):
            raise LedgerTransitionError(
                "only raw unadjusted prices may reach order submission"
            )
        request_terms = (
            request.instrument_id,
            request.side,
            request.quantity,
            request.order_type,
            request.limit_price,
            request.session,
            request.limit_price_basis,
            request.limit_price_source_id,
        )
        intent_terms = (
            intent.instrument_id,
            intent.side,
            intent.quantity,
            intent.order_type,
            intent.limit_price,
            intent.session,
            intent.limit_price_basis,
            intent.limit_price_source_id,
        )
        if request_terms != intent_terms:
            raise LedgerTransitionError("order request terms differ from validated intent")
        self._request_ids.add(request.request_id)
        self._orders[request.order_id] = replace(
            state,
            status=OrderStatus.SUBMITTED,
            request_id=request.request_id,
        )

    def _apply_fill(self, event: FillRecorded) -> None:
        state = self.order(event.order_id)
        if state.status not in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
            raise LedgerTransitionError(
                f"cannot fill order {event.order_id} from {state.status.value}"
            )
        if event.fill_id in self._fill_ids:
            raise LedgerTransitionError(f"duplicate fill_id: {event.fill_id}")
        new_filled = state.filled_quantity + event.quantity
        if new_filled > state.intent.quantity:
            raise LedgerAccountingError(
                f"overfill for {event.order_id}: {new_filled} > {state.intent.quantity}"
            )

        if state.intent.side is Side.BUY:
            if (
                event.buy_lot_sellable_from is None
                or event.buy_lot_sellable_from <= event.trade_date
            ):
                raise LedgerAccountingError(
                    "buy fill requires a sellable_from date after trade_date (T+1)"
                )
            debit = event.gross_notional_fen + event.fee_fen
            if debit > self._cash_fen:
                raise LedgerAccountingError(
                    f"insufficient cash: need {debit} fen, have {self._cash_fen} fen"
                )
            self._cash_fen -= debit
            self._lots.append(
                PositionLot(
                    lot_id=event.fill_id,
                    instrument_id=state.intent.instrument_id,
                    quantity=event.quantity,
                    acquired_trade_date=event.trade_date,
                    sellable_from=event.buy_lot_sellable_from,
                )
            )
        else:
            if event.buy_lot_sellable_from is not None:
                raise LedgerAccountingError(
                    "sell fill cannot carry buy_lot_sellable_from"
                )
            if event.fee_fen > event.gross_notional_fen:
                raise LedgerAccountingError("sell fee exceeds gross proceeds")
            self._consume_sellable_lots(
                state.intent.instrument_id,
                event.trade_date,
                event.quantity,
            )
            self._cash_fen += event.gross_notional_fen - event.fee_fen

        self._fill_ids.add(event.fill_id)
        next_status = (
            OrderStatus.FILLED
            if new_filled == state.intent.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        self._orders[event.order_id] = replace(
            state,
            status=next_status,
            filled_quantity=new_filled,
            gross_notional_fen=state.gross_notional_fen + event.gross_notional_fen,
            fee_fen=state.fee_fen + event.fee_fen,
        )

    def _consume_sellable_lots(
        self,
        instrument_id: str,
        trade_date: date,
        quantity: int,
    ) -> None:
        available = self.sellable_quantity(instrument_id, trade_date)
        if available < quantity:
            raise LedgerAccountingError(
                f"oversell or T+1 violation for {instrument_id}: "
                f"requested {quantity}, sellable {available}"
            )
        remaining = quantity
        new_lots: list[PositionLot] = []
        for lot in sorted(
            self._lots,
            key=lambda item: (
                item.sellable_from,
                item.acquired_trade_date,
                item.lot_id,
            ),
        ):
            if (
                remaining
                and lot.instrument_id == instrument_id
                and lot.sellable_from <= trade_date
            ):
                consumed = min(remaining, lot.quantity)
                remaining -= consumed
                if consumed < lot.quantity:
                    new_lots.append(replace(lot, quantity=lot.quantity - consumed))
            else:
                new_lots.append(lot)
        self._lots = new_lots

    def _apply_terminal(self, order_id: str, terminal: OrderStatus) -> None:
        state = self.order(order_id)
        if state.status not in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
            raise LedgerTransitionError(
                f"cannot mark order {order_id} {terminal.value} from {state.status.value}"
            )
        self._orders[order_id] = replace(state, status=terminal)
