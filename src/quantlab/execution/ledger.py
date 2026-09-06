"""Append-only, deterministic order and cash/share ledger.

Cash and shares are reserved atomically at submission time: a buy reserves
its worst-case cash need (gross limit notional plus an explicit, externally
provided fee cap) and a sell reserves sellable shares. Partial fills draw
down the reservation; cancel/expire releases what is left. Availability is
always re-derived from settled state minus active reservations, and the
invariants (available cash never negative, reservations never exceed
settled cash or sellable shares) are checked after every event.
"""

from __future__ import annotations

import hashlib
import json
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
    TimeInForce,
    derive_order_status,
    exchange_date,
    require_aware,
    require_decimal,
    require_identifier,
    require_int,
)
from quantlab.execution.rules import TradingCalendar


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
    account_fingerprint: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.order_id, "order_id")
        require_aware(self.occurred_at, "occurred_at")
        if self.account_fingerprint is not None:
            require_identifier(
                self.account_fingerprint, "account_fingerprint"
            )
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
    """Broker submission with the explicit worst-case fee cap for the cash
    reservation of a buy order.

    The fee cap is caller-supplied and must be verifiable (an exact,
    effective-dated, account-specific schedule). Production data lacking a
    real fee table keeps this unknown; tests may use evidence explicitly
    marked as synthetic. There is no default zero-fee or generic-bps path.
    """

    event_id: str
    occurred_at: datetime
    request: OrderRequest
    worst_case_fee_fen: int = 0

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_aware(self.occurred_at, "occurred_at")
        require_int(self.worst_case_fee_fen, "worst_case_fee_fen")
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


@dataclass(frozen=True)
class ActiveReservation:
    """One live reservation against the account's settled state."""

    order_id: str
    instrument_id: str
    reserved_cash_fen: int
    reserved_shares: int


@dataclass(frozen=True)
class AccountAvailability:
    """Account state split into settled, reserved, and available parts."""

    instrument_id: str
    trade_date: date
    settled_cash_fen: int
    reserved_cash_fen: int
    available_cash_fen: int
    total_shares: int
    sellable_shares: int
    reserved_sellable_shares: int
    available_sellable_shares: int


def account_state_fingerprint(snapshot: AccountSnapshot) -> str:
    """Deterministic fingerprint of the settled account state.

    Constraint assessments bind this fingerprint, so a stale assessment
    (taken before another order's fill or reservation changed cash/lots)
    is rejected instead of silently gating on outdated facts.
    """
    payload = {
        "account_id": snapshot.account_id,
        "cash_fen": snapshot.cash_fen,
        "lots": [
            {
                "lot_id": lot.lot_id,
                "instrument_id": lot.instrument_id,
                "quantity": lot.quantity,
                "acquired_trade_date": lot.acquired_trade_date.isoformat(),
                "sellable_from": lot.sellable_from.isoformat(),
            }
            for lot in sorted(
                snapshot.lots,
                key=lambda lot: (
                    lot.instrument_id,
                    lot.sellable_from,
                    lot.acquired_trade_date,
                    lot.lot_id,
                ),
            )
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ExecutionLedger:
    """Append-only event ledger with atomic validation-before-mutation."""

    def __init__(
        self,
        initial: AccountSnapshot,
        *,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._initial = initial
        self._cash_fen = initial.cash_fen
        self._lots = list(initial.lots)
        self.calendar = calendar
        self._orders: dict[str, OrderLedgerState] = {}
        self._events: list[LedgerEvent] = []
        self._events_by_id: dict[str, LedgerEvent] = {}
        self._fill_ids: set[str] = set()
        self._request_ids: set[str] = set()
        self._reservations: dict[str, ActiveReservation] = {}

    @classmethod
    def replay(
        cls,
        initial: AccountSnapshot,
        events: tuple[LedgerEvent, ...] | list[LedgerEvent],
        *,
        calendar: TradingCalendar | None = None,
    ) -> ExecutionLedger:
        ledger = cls(initial, calendar=calendar)
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

    @property
    def reservations(self) -> tuple[ActiveReservation, ...]:
        return tuple(
            self._reservations[key] for key in sorted(self._reservations)
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

    def reserved_cash_fen(self) -> int:
        return sum(
            reservation.reserved_cash_fen
            for reservation in self._reservations.values()
        )

    def reserved_sellable_shares(self, instrument_id: str) -> int:
        return sum(
            reservation.reserved_shares
            for reservation in self._reservations.values()
            if reservation.instrument_id == instrument_id
        )

    def availability(self, instrument_id: str, trade_date: date) -> AccountAvailability:
        settled_reserved = self.reserved_cash_fen()
        sellable = self.sellable_quantity(instrument_id, trade_date)
        reserved_shares = self.reserved_sellable_shares(instrument_id)
        available_cash = self._cash_fen - settled_reserved
        available_shares = sellable - reserved_shares
        if available_cash < 0 or available_shares < 0:
            raise LedgerAccountingError(
                "availability invariant violated: available cash/shares went negative"
            )
        return AccountAvailability(
            instrument_id=instrument_id,
            trade_date=trade_date,
            settled_cash_fen=self._cash_fen,
            reserved_cash_fen=settled_reserved,
            available_cash_fen=available_cash,
            total_shares=self.position_quantity(instrument_id),
            sellable_shares=sellable,
            reserved_sellable_shares=reserved_shares,
            available_sellable_shares=available_shares,
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

        self._check_invariants()
        self._events.append(event)
        self._events_by_id[event.event_id] = event
        return True

    def _check_invariants(self) -> None:
        """Absolute integer invariants re-checked after every event."""
        reserved_cash = self.reserved_cash_fen()
        if reserved_cash > self._cash_fen:
            raise LedgerAccountingError(
                f"reserved cash {reserved_cash} exceeds settled cash {self._cash_fen}"
            )
        for reservation in self._reservations.values():
            sellable = self.sellable_quantity(
                reservation.instrument_id, date.max
            )
            reserved = self.reserved_sellable_shares(reservation.instrument_id)
            if reserved > sellable:
                raise LedgerAccountingError(
                    f"reserved sellable shares {reserved} exceed sellable "
                    f"{sellable} for {reservation.instrument_id}"
                )
            availability = self.availability(
                reservation.instrument_id,
                self._orders[reservation.order_id].intent.intended_trade_date,
            )
            if (
                availability.settled_cash_fen
                != availability.available_cash_fen + availability.reserved_cash_fen
                or availability.sellable_shares
                != availability.available_sellable_shares
                + availability.reserved_sellable_shares
            ):
                raise LedgerAccountingError(
                    "settled/available/reserved identity violated for "
                    + reservation.instrument_id
                )

    def submit_orders(
        self,
        submissions: list[tuple[OrderSubmitted, int]],
    ) -> None:
        """Atomically reserve and submit a batch (all-or-nothing).

        Every reservation is computed and validated against the current
        availability BEFORE any event mutates the ledger, so a batch that
        overdraws cash or shares fails without leaving a partial
        reservation. The caller passes ``(event, worst_case_fee_fen)``; the
        fee cap is copied into the event so replay stays deterministic.
        """
        staged: list[OrderSubmitted] = []
        staged_cash = 0
        staged_shares: dict[str, int] = {}
        for event, worst_case_fee_fen in submissions:
            event = replace(event, worst_case_fee_fen=worst_case_fee_fen)
            state = self.order(event.request.order_id)
            if state.status is not OrderStatus.VALIDATED:
                raise LedgerTransitionError(
                    f"cannot submit order {event.request.order_id} "
                    f"from {state.status.value}"
                )
            intent = state.intent
            if intent.side is Side.BUY:
                if worst_case_fee_fen <= 0:
                    raise LedgerAccountingError(
                        "buy submission requires an explicit worst-case fee "
                        "cap for its cash reservation; production data "
                        "without a real fee table stays unknown instead of "
                        "assuming zero fees"
                    )
                need = int(intent.limit_price * intent.quantity * 100) + (
                    worst_case_fee_fen
                )
                available_now = self.availability(
                    intent.instrument_id,
                    intent.intended_trade_date,
                ).available_cash_fen - staged_cash
                if need > available_now:
                    raise LedgerAccountingError(
                        f"buy reservation for {intent.instrument_id} needs "
                        f"{need} fen, available {available_now} fen "
                        "after earlier staged reservations"
                    )
                staged_cash += need
            else:
                available = (
                    self.availability(
                        intent.instrument_id, intent.intended_trade_date
                    ).available_sellable_shares
                    - staged_shares.get(intent.instrument_id, 0)
                )
                if intent.quantity > available:
                    raise LedgerAccountingError(
                        f"sell reservation for {intent.instrument_id} needs "
                        f"{intent.quantity} sellable shares, available {available}"
                    )
                staged_shares[intent.instrument_id] = (
                    staged_shares.get(intent.instrument_id, 0) + intent.quantity
                )
            staged.append(event)
        for event in staged:
            self.append(event)

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
        if event.account_fingerprint is not None:
            current = account_state_fingerprint(self.snapshot(event.occurred_at))
            if current != event.account_fingerprint:
                raise LedgerTransitionError(
                    "stale constraint assessment rejected: account state "
                    f"fingerprint drifted (assessment bound "
                    f"{event.account_fingerprint}, current {current})"
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
        if request.intended_trade_date != intent.intended_trade_date:
            raise LedgerTransitionError(
                "request intended trade date differs from validated intent"
            )
        if request.time_in_force is not TimeInForce.DAY:
            raise LedgerTransitionError("only DAY orders may be submitted")
        if intent.time_in_force is not TimeInForce.DAY:
            raise LedgerTransitionError("only DAY orders may be submitted")
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

        # -- atomic reservation ------------------------------------------
        if intent.side is Side.BUY:
            if event.worst_case_fee_fen <= 0:
                raise LedgerAccountingError(
                    "buy submission requires an explicit worst-case fee cap; "
                    "production without a real fee table stays unknown and "
                    "cannot reserve with an assumed zero fee"
                )
            need = int(intent.limit_price * intent.quantity * 100) + (
                event.worst_case_fee_fen
            )
            availability = self.availability(
                intent.instrument_id, intent.intended_trade_date
            )
            if need > availability.available_cash_fen:
                raise LedgerAccountingError(
                    f"buy reservation needs {need} fen, available "
                    f"{availability.available_cash_fen} fen"
                )
            self._reservations[request.order_id] = ActiveReservation(
                order_id=request.order_id,
                instrument_id=intent.instrument_id,
                reserved_cash_fen=need,
                reserved_shares=0,
            )
        else:
            availability = self.availability(
                intent.instrument_id, intent.intended_trade_date
            )
            if intent.quantity > availability.available_sellable_shares:
                raise LedgerAccountingError(
                    f"sell reservation needs {intent.quantity} sellable "
                    f"shares, available "
                    f"{availability.available_sellable_shares}"
                )
            self._reservations[request.order_id] = ActiveReservation(
                order_id=request.order_id,
                instrument_id=intent.instrument_id,
                reserved_cash_fen=0,
                reserved_shares=intent.quantity,
            )

        self._request_ids.add(request.request_id)
        self._orders[request.order_id] = replace(
            state,
            status=OrderStatus.SUBMITTED,
            request_id=request.request_id,
        )

    def _release_reservation(self, order_id: str, *, cash_used: int = 0,
                             shares_used: int = 0) -> None:
        reservation = self._reservations.pop(order_id, None)
        if reservation is None:
            return
        remaining_cash = reservation.reserved_cash_fen - cash_used
        remaining_shares = reservation.reserved_shares - shares_used
        if remaining_cash > 0 or remaining_shares > 0:
            self._reservations[order_id] = ActiveReservation(
                order_id=order_id,
                instrument_id=reservation.instrument_id,
                reserved_cash_fen=max(0, remaining_cash),
                reserved_shares=max(0, remaining_shares),
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
        # DAY order: the economic trade date is bound to the intent. A late
        # broker report may arrive later in wall-clock time, but its trade
        # date can never drift.
        if event.trade_date != state.intent.intended_trade_date:
            raise LedgerAccountingError(
                f"DAY fill trade_date {event.trade_date} does not match the "
                f"intended trade date {state.intent.intended_trade_date}"
            )

        if state.intent.side is Side.BUY:
            expected = (
                self.calendar.next_session(event.trade_date)
                if self.calendar is not None
                else None
            )
            if self.calendar is not None:
                if expected is None:
                    raise LedgerAccountingError(
                        "calendar cannot derive the next session after "
                        f"{event.trade_date}; T+1 is fail-closed on "
                        "incomplete calendar coverage"
                    )
                if event.buy_lot_sellable_from != expected:
                    raise LedgerAccountingError(
                        f"buy lot sellable_from {event.buy_lot_sellable_from} "
                        f"must be exactly the next session {expected}"
                    )
            elif (
                event.buy_lot_sellable_from is None
                or event.buy_lot_sellable_from <= event.trade_date
            ):
                raise LedgerAccountingError(
                    "buy fill requires a sellable_from date after trade_date (T+1)"
                )
            debit = event.gross_notional_fen + event.fee_fen
            reservation = self._reservations.get(event.order_id)
            reserved = reservation.reserved_cash_fen if reservation else 0
            if debit > reserved:
                raise LedgerAccountingError(
                    f"fill draws {debit} fen but only {reserved} fen is "
                    "reserved for this order; the worst case was "
                    "under-reserved"
                )
            self._cash_fen -= debit
            self._release_reservation(event.order_id, cash_used=debit)
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
            self._release_reservation(
                event.order_id, cash_used=0, shares_used=event.quantity
            )

        self._fill_ids.add(event.fill_id)
        next_status = (
            OrderStatus.FILLED
            if new_filled == state.intent.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        if next_status is OrderStatus.FILLED:
            # a fully filled order keeps no reservation: the worst case was
            # an upper bound, not a target
            self._reservations.pop(event.order_id, None)
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
        # cancel/expire releases everything still reserved; cash and shares
        # actually consumed by partial fills were already drawn down
        self._reservations.pop(order_id, None)
        self._orders[order_id] = replace(state, status=terminal)
