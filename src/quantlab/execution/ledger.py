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
    AssessmentAuthority,
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
    fingerprint_decisions,
    fingerprint_fee_cap_quote,
    fingerprint_order_intent,
    require_aware,
    require_decimal,
    require_identifier,
    require_int,
)
from quantlab.execution.planning import (
    AvailabilityReservation,
    AvailabilityState,
    ExecutionStateView,
    FeeCapQuote,
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
    availability_fingerprint: str | None = None
    authority: AssessmentAuthority | None = None

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_identifier(self.order_id, "order_id")
        require_aware(self.occurred_at, "occurred_at")
        if self.account_fingerprint is not None:
            require_identifier(self.account_fingerprint, "account_fingerprint")
        if self.availability_fingerprint is not None:
            require_identifier(
                self.availability_fingerprint,
                "availability_fingerprint",
            )
        dimensions = [decision.dimension for decision in self.decisions]
        if len(dimensions) != len(set(dimensions)):
            raise ExecutionValidationError("duplicate constraint dimension")
        if set(dimensions) != set(ConstraintDimension):
            missing = sorted(d.value for d in set(ConstraintDimension) - set(dimensions))
            extra = sorted(d.value for d in set(dimensions) - set(ConstraintDimension))
            raise ExecutionValidationError(
                f"constraint assessment must cover all dimensions; missing={missing} extra={extra}"
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
    availability_fingerprint: str | None = None
    fee_quote_fingerprint: str | None = None
    fee_quote: FeeCapQuote | None = None

    def __post_init__(self) -> None:
        require_identifier(self.event_id, "event_id")
        require_aware(self.occurred_at, "occurred_at")
        require_int(self.worst_case_fee_fen, "worst_case_fee_fen")
        if self.availability_fingerprint is not None:
            require_identifier(
                self.availability_fingerprint,
                "availability_fingerprint",
            )
        if self.fee_quote_fingerprint is not None:
            if len(self.fee_quote_fingerprint) != 64 or any(
                char not in "0123456789abcdef" for char in self.fee_quote_fingerprint
            ):
                raise ExecutionValidationError("fee_quote_fingerprint must be SHA-256")
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
class ManualFillImported:
    """User-imported broker fill fact, not a QuantLab order or simulated fill."""

    event_id: str
    fill_id: str
    occurred_at: datetime
    account_id: str
    instrument_id: str
    side: Side
    trade_date: date
    quantity: int
    price: Decimal
    gross_notional_fen: int
    fee_fen: int
    buy_lot_sellable_from: date | None
    source_sha256: str
    source_row_sha256: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.event_id, "event_id"),
            (self.fill_id, "fill_id"),
            (self.account_id, "account_id"),
            (self.instrument_id, "instrument_id"),
        ):
            require_identifier(value, field)
        require_aware(self.occurred_at, "occurred_at")
        if not isinstance(self.side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        require_int(self.quantity, "quantity", minimum=1)
        require_decimal(self.price, "price", positive=True)
        require_int(self.gross_notional_fen, "gross_notional_fen", minimum=1)
        require_int(self.fee_fen, "fee_fen")
        if exchange_date(self.occurred_at) < self.trade_date:
            raise ExecutionValidationError("fill reported before its exchange trade_date")
        exact_fen = self.price * self.quantity * 100
        if exact_fen != exact_fen.to_integral_value():
            raise ExecutionValidationError(
                "fill price and quantity produce fractional fen; explicit rounding is required"
            )
        if int(exact_fen) != self.gross_notional_fen:
            raise ExecutionValidationError(
                "gross_notional_fen disagrees with Decimal price * quantity"
            )
        for digest, field in (
            (self.source_sha256, "source_sha256"),
            (self.source_row_sha256, "source_row_sha256"),
        ):
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ExecutionValidationError(f"{field} must be lowercase SHA-256")


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
    | ManualFillImported
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
    authority: AssessmentAuthority | None = None

    @property
    def remaining_quantity(self) -> int:
        return self.intent.quantity - self.filled_quantity


@dataclass(frozen=True)
class ActiveReservation:
    """One live reservation against the account's settled state.

    For a buy, the reservation independently tracks the remaining
    worst-case limit notional of the unfilled shares and the remaining fee
    capacity of the order-lifetime cumulative fee cap; the identity
    ``reserved_cash_fen == remaining_quantity * limit_price_fen +
    (fee_cap_fen - fee_used_fen)`` holds after every event, so price
    improvement releases the excess instead of hoarding it.
    """

    order_id: str
    instrument_id: str
    reserved_cash_fen: int
    reserved_shares: int
    limit_price_fen: int = 0
    fee_cap_fen: int = 0
    fee_used_fen: int = 0


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


_OPEN_ORDER_STATUSES = frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED})


def _fingerprint_lots(lots) -> list[dict[str, str]]:
    return [
        {
            "lot_id": lot.lot_id,
            "instrument_id": lot.instrument_id,
            "quantity": str(lot.quantity),
            "acquired_trade_date": lot.acquired_trade_date.isoformat(),
            "sellable_from": lot.sellable_from.isoformat(),
        }
        for lot in sorted(
            lots,
            key=lambda lot: (
                lot.instrument_id,
                lot.sellable_from,
                lot.acquired_trade_date,
                lot.lot_id,
            ),
        )
    ]


@dataclass(frozen=True)
class _LedgerSnapshot:
    """Byte-for-byte capture of every mutable ledger container."""

    cash_fen: int
    lots: list
    orders: dict
    events: list
    events_by_id: dict
    fill_ids: set
    request_ids: set
    reservations: dict


class ExecutionLedger:
    """Append-only event ledger with transactional strong exception safety.

    Every public mutation (``append``, ``submit_orders``) first captures a
    snapshot of all mutable state, applies its changes, re-checks the
    invariants, and only then commits. Any ``Exception`` or
    ``BaseException`` — injected, accidental, or a ``KeyboardInterrupt`` —
    restores the exact prior containers (cash, lots, orders, reservations,
    events, event ids, fill ids, request ids), so a failed call leaves no
    partial reservation, no half-applied event, and no orphaned id.
    """

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
        # explicit non-production fixture switch (append_legacy_low_level)
        self._legacy_fixture_mode = False
        self._manual_import_mode = False
        # active bound-batch base (submit_orders only); None outside a batch
        self._batch_base_fingerprint: str | None = None

    @classmethod
    def replay(
        cls,
        initial: AccountSnapshot,
        events: tuple[LedgerEvent, ...] | list[LedgerEvent],
        *,
        calendar: TradingCalendar | None = None,
        legacy_fixture_entry: bool = False,
    ) -> ExecutionLedger:
        """Rebuild a ledger from its event history (deterministic).

        ``legacy_fixture_entry`` mirrors ``append_legacy_low_level`` for
        fixture-built histories and must never be used on the production
        path.
        """
        ledger = cls(initial, calendar=calendar)
        ledger._legacy_fixture_mode = legacy_fixture_entry
        for event in events:
            # fixture histories carry bound batches: replay them with the
            # same batch-base semantics the original submission used
            if isinstance(event, OrderSubmitted):
                if ledger._batch_base_fingerprint is None:
                    ledger._batch_base_fingerprint = event.availability_fingerprint
            else:
                ledger._batch_base_fingerprint = None
            if isinstance(event, ManualFillImported):
                ledger.append_manual_imports([event])
            else:
                ledger.append(event)
        ledger._legacy_fixture_mode = False
        ledger._batch_base_fingerprint = None
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
        return tuple(self._reservations[key] for key in sorted(self._reservations))

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
        return sum(reservation.reserved_cash_fen for reservation in self._reservations.values())

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

    def availability_state(self, as_of: datetime | None = None) -> AvailabilityState:
        """Canonical resource state, derived from the ledger itself.

        Binds account id, the Shanghai-local as_of instant and the trade
        date it maps to, settled cash, every position lot, every active
        reservation WITH its full economic terms (limit price, fee cap,
        fee already used, reserved cash, reserved shares), and the
        derived available cash / per-instrument available sellable
        shares. Lifecycle-only transitions (INTENDED -> VALIDATED) do not
        appear here, so they can never invalidate a batch's own
        authorization; every resource mutation does.
        """
        moment = as_of if as_of is not None else self._initial.as_of
        reservations = tuple(
            AvailabilityReservation(
                order_id=reservation.order_id,
                instrument_id=reservation.instrument_id,
                limit_price_fen=reservation.limit_price_fen,
                fee_cap_fen=reservation.fee_cap_fen,
                fee_used_fen=reservation.fee_used_fen,
                reserved_cash_fen=reservation.reserved_cash_fen,
                reserved_shares=reservation.reserved_shares,
            )
            for reservation in sorted(self._reservations.values(), key=lambda item: item.order_id)
        )
        available: dict[str, int] = {}
        for instrument_id in {lot.instrument_id for lot in self._lots}:
            availability = self.availability(instrument_id, exchange_date(moment))
            available[instrument_id] = availability.available_sellable_shares
        return AvailabilityState(
            account_id=self._initial.account_id,
            as_of=moment,
            trade_date=exchange_date(moment),
            settled_cash_fen=self._cash_fen,
            lots=tuple(self._lots),
            reservations=reservations,
            available_cash_fen=self._cash_fen - self.reserved_cash_fen(),
            available_sellable_shares=available,
        )

    def availability_fingerprint(self, as_of: datetime | None = None) -> str:
        """Canonical availability fingerprint (derived, never declared)."""
        return self.availability_state(as_of).fingerprint

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

    def execution_state_view(self, as_of: datetime) -> ExecutionStateView:
        """Reservation-aware planning view at ``as_of``.

        Buys must be funded from available (unreserved) cash and sells from
        available (unreserved) sellable shares; the view carries the
        canonical availability state, whose fingerprint is derived from its
        own payload so plans and orders bind the exact state they were
        derived from.
        """
        settled = self.snapshot(as_of)
        state = self.availability_state(as_of)
        return ExecutionStateView(account=settled, state=state)

    def _begin(self) -> _LedgerSnapshot:
        return _LedgerSnapshot(
            cash_fen=self._cash_fen,
            lots=list(self._lots),
            orders=dict(self._orders),
            events=list(self._events),
            events_by_id=dict(self._events_by_id),
            fill_ids=set(self._fill_ids),
            request_ids=set(self._request_ids),
            reservations=dict(self._reservations),
        )

    def _restore(self, snapshot: _LedgerSnapshot) -> None:
        self._cash_fen = snapshot.cash_fen
        self._lots = snapshot.lots
        self._orders = snapshot.orders
        self._events = snapshot.events
        self._events_by_id = snapshot.events_by_id
        self._fill_ids = snapshot.fill_ids
        self._request_ids = snapshot.request_ids
        self._reservations = snapshot.reservations

    def append_legacy_low_level(self, event: LedgerEvent) -> bool:
        """Explicit NON-PRODUCTION entry point for low-level fixtures.

        Skips ONLY the assessment-authority and typed-fee-quote
        verification at the submission boundary; every other transition
        rule, reservation rule, DAY/trade-date binding, limit-price
        protection, and invariant still applies inside the same
        transaction. The readiness production path must never call this
        method: it uses ``submit_orders`` with stored assessment
        authorities and typed fee quotes.
        """
        previous = self._legacy_fixture_mode
        self._legacy_fixture_mode = True
        try:
            return self.append(event)
        finally:
            self._legacy_fixture_mode = previous

    def append(self, event: LedgerEvent) -> bool:
        """Append one event; exact duplicate event ids are idempotent.

        The event either fully applies (including the post-apply invariant
        check) or the ledger is restored to its exact prior state.
        """
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

        snapshot = self._begin()
        try:
            if isinstance(event, OrderIntended):
                self._apply_intended(event)
            elif isinstance(event, ConstraintsAssessed):
                self._apply_assessment(event)
            elif isinstance(event, OrderSubmitted):
                self._apply_submitted(event)
            elif isinstance(event, FillRecorded):
                self._apply_fill(event)
            elif isinstance(event, ManualFillImported):
                if not self._manual_import_mode:
                    raise LedgerTransitionError(
                        "manual fills require the explicit manual-import entry point"
                    )
                self._apply_manual_fill(event)
            elif isinstance(event, OrderCanceled):
                self._apply_terminal(event.order_id, OrderStatus.CANCELED)
            elif isinstance(event, OrderExpired):
                self._apply_terminal(event.order_id, OrderStatus.EXPIRED)
            else:  # pragma: no cover - closed union and defensive runtime guard
                raise TypeError(f"unsupported ledger event: {type(event).__name__}")
            self._check_invariants()
            # both event-index commits sit inside the same snapshot/restore
            # boundary as apply and invariant validation: a BaseException
            # at either write restores the ordered log AND the id index
            # together with every applied mutation
            self._events.append(event)
            self._events_by_id[event.event_id] = event
        except BaseException:
            self._restore(snapshot)
            raise
        return True

    def append_manual_imports(self, events: list[ManualFillImported]) -> None:
        """Atomically post a validated batch of external user-reported fills."""
        if any(not isinstance(event, ManualFillImported) for event in events):
            raise TypeError("manual import batch accepts only ManualFillImported events")
        snapshot = self._begin()
        previous = self._manual_import_mode
        self._manual_import_mode = True
        try:
            for event in events:
                self.append(event)
        except BaseException:
            self._restore(snapshot)
            raise
        finally:
            self._manual_import_mode = previous

    def _check_invariants(self) -> None:
        """Absolute integer invariants re-checked after every event."""
        reserved_cash = self.reserved_cash_fen()
        if reserved_cash > self._cash_fen:
            raise LedgerAccountingError(
                f"reserved cash {reserved_cash} exceeds settled cash {self._cash_fen}"
            )
        for reservation in self._reservations.values():
            sellable = self.sellable_quantity(reservation.instrument_id, date.max)
            reserved = self.reserved_sellable_shares(reservation.instrument_id)
            if reserved > sellable:
                raise LedgerAccountingError(
                    f"reserved sellable shares {reserved} exceed sellable "
                    f"{sellable} for {reservation.instrument_id}"
                )
            order_state = self._orders.get(reservation.order_id)
            if order_state is None:
                raise LedgerAccountingError(f"reservation for unknown order {reservation.order_id}")
            remaining = order_state.remaining_quantity
            is_buy = order_state.intent.side is Side.BUY
            if not is_buy and reservation.reserved_cash_fen:
                raise LedgerAccountingError(
                    f"sell reservation carries a buy-style cash identity: {reservation.order_id}"
                )
            if is_buy and reservation.limit_price_fen:
                needed = remaining * reservation.limit_price_fen + (
                    reservation.fee_cap_fen - reservation.fee_used_fen
                )
                if reservation.reserved_cash_fen != needed:
                    raise LedgerAccountingError(
                        f"buy reservation identity violated for "
                        f"{reservation.order_id}: reserved "
                        f"{reservation.reserved_cash_fen} != worst-case "
                        f"remaining need {needed} "
                        f"({remaining} unfilled shares + "
                        f"{reservation.fee_cap_fen - reservation.fee_used_fen} "
                        "fen fee capacity)"
                    )
                if remaining <= 0:
                    raise LedgerAccountingError(
                        f"buy reservation survives a fully filled order: {reservation.order_id}"
                    )
            else:
                if reservation.reserved_shares != remaining:
                    raise LedgerAccountingError(
                        f"sell reservation identity violated for "
                        f"{reservation.order_id}: reserved "
                        f"{reservation.reserved_shares} shares != "
                        f"{remaining} unfilled shares"
                    )
            availability = self.availability(
                reservation.instrument_id,
                self._orders[reservation.order_id].intent.intended_trade_date,
            )
            if (
                availability.settled_cash_fen
                != availability.available_cash_fen + availability.reserved_cash_fen
                or availability.sellable_shares
                != availability.available_sellable_shares + availability.reserved_sellable_shares
            ):
                raise LedgerAccountingError(
                    "settled/available/reserved identity violated for " + reservation.instrument_id
                )

    def submit_orders(
        self,
        submissions: list[tuple[OrderSubmitted, FeeCapQuote]],
    ) -> None:
        """Atomically reserve and submit a batch (all-or-nothing).

        Every reservation is computed and validated against the current
        availability BEFORE any event mutates the ledger, so a batch that
        overdraws cash or shares fails without leaving a partial
        reservation, and the whole batch is one transaction (strong
        exception safety, including KeyboardInterrupt). The caller passes
        ``(event, fee_quote)``; the fee quote is TYPED provenance - the
        submission boundary never degrades it to a bare integer - and each
        event records the quote's cap and evidence fingerprint so replay
        stays deterministic.
        """
        staged: list[OrderSubmitted] = []
        staged_cash = 0
        staged_shares: dict[str, int] = {}
        pre_batch_state = self.availability_fingerprint()
        # every member binds the SAME explicit pre-batch state; a member's
        # own earlier reservation in this batch cannot make it stale
        self._batch_base_fingerprint = pre_batch_state
        batch_snapshot = self._begin()
        try:
            for event, fee_quote in submissions:
                if not isinstance(fee_quote, FeeCapQuote):
                    raise LedgerAccountingError(
                        "submission fee cap must be a typed FeeCapQuote "
                        "bound to account, instrument, trade date, and "
                        "schedule evidence; a bare integer is not accepted"
                    )
                if (
                    event.availability_fingerprint is not None
                    and event.availability_fingerprint != pre_batch_state
                ):
                    raise LedgerTransitionError(
                        "batch submission rejected: assessment execution state "
                        f"{event.availability_fingerprint} is not the current "
                        f"pre-batch state {pre_batch_state}"
                    )
                state = self.order(event.request.order_id)
                if state.status is not OrderStatus.VALIDATED:
                    raise LedgerTransitionError(
                        f"cannot submit order {event.request.order_id} from {state.status.value}"
                    )
                intent = state.intent
                if (
                    fee_quote.instrument_id != intent.instrument_id
                    or fee_quote.account_id != self._initial.account_id
                    or fee_quote.trade_date != intent.intended_trade_date
                ):
                    raise LedgerAccountingError(
                        f"fee quote does not match the submission: quote is "
                        f"for {fee_quote.instrument_id}/"
                        f"{fee_quote.account_id}/{fee_quote.trade_date}, "
                        f"order is {intent.instrument_id}/"
                        f"{self._initial.account_id}/"
                        f"{intent.intended_trade_date}"
                    )
                # the pair-supplied quote is embedded as the typed fee
                # authority; the lineage fingerprint stays the canonical
                # FULL-quote hash (never the bare source SHA)
                event = replace(
                    event,
                    worst_case_fee_fen=fee_quote.cap_fen,
                    fee_quote_fingerprint=(
                        event.fee_quote_fingerprint or fingerprint_fee_cap_quote(fee_quote)
                    ),
                    fee_quote=event.fee_quote or fee_quote,
                )
                if intent.side is Side.BUY:
                    if fee_quote.cap_fen <= 0:
                        raise LedgerAccountingError(
                            "buy submission requires an explicit worst-case fee "
                            "cap for its cash reservation; production data "
                            "without a real fee table stays unknown instead of "
                            "assuming zero fees"
                        )
                    need = int(intent.limit_price * intent.quantity * 100) + (fee_quote.cap_fen)
                    available_now = (
                        self.availability(
                            intent.instrument_id,
                            intent.intended_trade_date,
                        ).available_cash_fen
                        - staged_cash
                    )
                    if need > available_now:
                        raise LedgerAccountingError(
                            f"buy reservation for {intent.instrument_id} needs "
                            f"{need} fen, available {available_now} fen "
                            "after earlier staged reservations"
                        )
                    staged_cash += need
                else:
                    available = self.availability(
                        intent.instrument_id, intent.intended_trade_date
                    ).available_sellable_shares - staged_shares.get(intent.instrument_id, 0)
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
        except BaseException:
            self._restore(batch_snapshot)
            raise
        finally:
            self._batch_base_fingerprint = None

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
            current_account = account_state_fingerprint(self.snapshot(event.occurred_at))
            if current_account != event.account_fingerprint:
                raise LedgerTransitionError(
                    "stale constraint assessment rejected: account state "
                    f"fingerprint drifted (assessment bound "
                    f"{event.account_fingerprint}, current {current_account})"
                )
        authority = event.authority
        if authority is None:
            # Low-level fixtures may append a bare assessment (the order
            # can never reach SUBMITTED without an authority), but the
            # availability binding is still verified.
            if event.availability_fingerprint is not None:
                current_state = self.availability_fingerprint()
                if current_state != event.availability_fingerprint:
                    raise LedgerTransitionError(
                        "stale constraint assessment rejected: availability "
                        f"state drifted (assessment bound "
                        f"{event.availability_fingerprint}, current "
                        f"{current_state})"
                    )
            next_status = derive_order_status(state.intent.side, event.decisions)
            self._orders[event.order_id] = replace(state, status=next_status)
            return
        # the authority must bind THIS ledger's intent, decisions, and state
        if authority.order_id != event.order_id:
            raise LedgerTransitionError(
                f"assessment authority is bound to {authority.order_id}, not {event.order_id}"
            )
        intent_fingerprint = fingerprint_order_intent(state.intent)
        if authority.intent_fingerprint != intent_fingerprint:
            raise LedgerTransitionError(
                "assessment authority intent fingerprint does not match the "
                f"stored intent (authority {authority.intent_fingerprint}, "
                f"stored {intent_fingerprint})"
            )
        decision_fingerprint = fingerprint_decisions(event.decisions)
        if authority.decision_fingerprint != decision_fingerprint:
            raise LedgerTransitionError(
                "assessment authority decision fingerprint does not match "
                f"the assessed decisions (authority "
                f"{authority.decision_fingerprint}, computed "
                f"{decision_fingerprint})"
            )
        if authority.assessment_event_id != event.event_id:
            raise LedgerTransitionError(
                "assessment authority references event "
                f"{authority.assessment_event_id}, not {event.event_id}"
            )
        if event.availability_fingerprint is not None:
            current_state = self.availability_fingerprint()
            if current_state != event.availability_fingerprint:
                raise LedgerTransitionError(
                    "stale constraint assessment rejected: availability "
                    f"state drifted (assessment bound "
                    f"{event.availability_fingerprint}, current "
                    f"{current_state})"
                )
        if (
            authority.availability_fingerprint is not None
            and event.availability_fingerprint is not None
            and authority.availability_fingerprint != event.availability_fingerprint
        ):
            raise LedgerTransitionError(
                "assessment authority availability fingerprint differs from "
                "the assessment event binding"
            )
        next_status = derive_order_status(state.intent.side, event.decisions)
        self._orders[event.order_id] = replace(state, status=next_status, authority=authority)

    def _apply_submitted(self, event: OrderSubmitted) -> None:
        request = event.request
        state = self.order(request.order_id)
        if state.status is not OrderStatus.VALIDATED:
            raise LedgerTransitionError(
                f"cannot submit order {request.order_id} from {state.status.value}"
            )
        if not self._legacy_fixture_mode:
            # -- typed fee quote: the submission boundary never accepts a
            # bare integer as fee authority ----------------------------
            quote = event.fee_quote
            if not isinstance(quote, FeeCapQuote):
                raise LedgerAccountingError(
                    "submission requires an embedded typed FeeCapQuote "
                    "bound to account, instrument, trade date, and "
                    "schedule evidence; a bare integer is not accepted"
                )
            quote_fingerprint = fingerprint_fee_cap_quote(quote)
            if event.fee_quote_fingerprint != quote_fingerprint:
                raise LedgerAccountingError(
                    "submission fee_quote_fingerprint does not match the embedded typed quote"
                )
            if event.worst_case_fee_fen != quote.cap_fen:
                raise LedgerAccountingError(
                    "submission worst_case_fee_fen does not match the typed fee quote cap"
                )
            # -- immutable assessment authority -------------------------
            authority = state.authority
            if authority is None:
                raise LedgerTransitionError(
                    f"order {request.order_id} has no stored assessment "
                    "authority; it was assessed outside the production path"
                )
            if (
                request.assessment_event_id is None
                or request.assessment_event_id != authority.assessment_event_id
            ):
                raise LedgerTransitionError(
                    "submission references assessment event "
                    f"{request.assessment_event_id!r}, but the stored "
                    f"authority is {authority.assessment_event_id!r}"
                )
            if (
                request.assessment_decision_fingerprint is None
                or request.assessment_decision_fingerprint != authority.decision_fingerprint
            ):
                raise LedgerTransitionError(
                    "submission decision fingerprint does not match the stored assessment authority"
                )
            if (
                request.availability_fingerprint is None
                or request.availability_fingerprint != authority.availability_fingerprint
            ):
                raise LedgerTransitionError(
                    "submission availability fingerprint does not match "
                    "the stored assessment authority"
                )
            if (
                request.plan_id != authority.plan_id
                or request.leg_id != authority.leg_id
                or request.instruction_id != authority.instruction_id
            ):
                raise LedgerTransitionError(
                    "submission plan/leg/instruction lineage differs from "
                    "the stored assessment authority"
                )
            if (
                request.limit_price_source_fingerprint is not None
                and request.limit_price_source_fingerprint
                != state.intent.limit_price_source_fingerprint
            ):
                raise LedgerTransitionError(
                    "submission price evidence fingerprint differs from the validated intent"
                )
            if request.fee_quote_fingerprint != state.intent.fee_quote_fingerprint:
                raise LedgerTransitionError(
                    "submission fee quote fingerprint differs from the validated intent"
                )
            # -- fee quote to assessment-authority binding: the typed quote
            # must derive from the EXACT fee schedule the stored
            # assessment authorized, and the intent and request must both
            # carry the embedded quote's canonical fingerprint. None ==
            # None is not fee lineage, and no fee is inferred or defaulted
            # from incomplete evidence.
            if (
                quote.evidence_id != authority.fee_schedule_evidence_id
                or quote.source_fingerprint != authority.fee_schedule_source_fingerprint
            ):
                raise LedgerAccountingError(
                    "submission fee quote does not derive from the fee "
                    "schedule authorized by the stored assessment: quote "
                    f"evidence {quote.evidence_id!r}/"
                    f"{quote.source_fingerprint!r} vs authority "
                    f"{authority.fee_schedule_evidence_id!r}/"
                    f"{authority.fee_schedule_source_fingerprint!r}"
                )
            if (
                state.intent.fee_quote_fingerprint is None
                or state.intent.fee_quote_fingerprint != quote_fingerprint
            ):
                raise LedgerAccountingError(
                    "submission intent lacks the typed fee quote lineage "
                    "fingerprint or it differs from the embedded quote's "
                    "canonical fingerprint; a missing fingerprint is not "
                    "fee lineage"
                )
            if (
                request.fee_quote_fingerprint is None
                or request.fee_quote_fingerprint != quote_fingerprint
            ):
                raise LedgerAccountingError(
                    "submission request lacks the typed fee quote lineage "
                    "fingerprint or it differs from the embedded quote's "
                    "canonical fingerprint; a missing fingerprint is not "
                    "fee lineage"
                )
        if request.availability_fingerprint is not None and request.availability_fingerprint != (
            self._batch_base_fingerprint or self.availability_fingerprint()
        ):
            raise LedgerTransitionError(
                "stale submission rejected: availability state drifted "
                f"since assessment (submission bound "
                f"{request.availability_fingerprint}, current "
                f"{self.availability_fingerprint()})"
            )
        if request.request_id in self._request_ids:
            raise LedgerTransitionError(f"duplicate request_id: {request.request_id}")
        intent = state.intent
        if intent.order_type is OrderType.LIMIT and intent.limit_price_basis is not PriceBasis.RAW:
            raise LedgerTransitionError("only raw unadjusted prices may reach order submission")
        if request.intended_trade_date != intent.intended_trade_date:
            raise LedgerTransitionError("request intended trade date differs from validated intent")
        # DAY commit boundary: the Shanghai-local submission date must be
        # exactly the intended trade date. A Friday-created intent for
        # Monday may exist as a future intention, but Friday can never
        # submit its Monday DAY request; late broker reports arrive later,
        # yet their fills still bind the submitted DAY trade date.
        if exchange_date(event.occurred_at) != request.intended_trade_date:
            raise LedgerTransitionError(
                "a DAY order is submitted on its intended trade date: "
                f"submission date {exchange_date(event.occurred_at)} != "
                f"intended {request.intended_trade_date}"
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
            need = int(intent.limit_price * intent.quantity * 100) + (event.worst_case_fee_fen)
            availability = self.availability(intent.instrument_id, intent.intended_trade_date)
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
                limit_price_fen=int(intent.limit_price * 100),
                fee_cap_fen=event.worst_case_fee_fen,
                fee_used_fen=0,
            )
        else:
            availability = self.availability(intent.instrument_id, intent.intended_trade_date)
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
                limit_price_fen=int(intent.limit_price * 100),
                fee_cap_fen=event.worst_case_fee_fen,
                fee_used_fen=0,
            )

        self._request_ids.add(request.request_id)
        self._orders[request.order_id] = replace(
            state,
            status=OrderStatus.SUBMITTED,
            request_id=request.request_id,
        )

    def _release_reservation(
        self, order_id: str, *, cash_used: int = 0, shares_used: int = 0
    ) -> None:
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
        # Limit protection BEFORE any mutation: a buy never fills above its
        # limit and a sell never fills below it.
        intent = state.intent
        if intent.order_type is OrderType.LIMIT and intent.limit_price is not None:
            if intent.side is Side.BUY and event.price > intent.limit_price:
                raise LedgerAccountingError(
                    f"buy fill price {event.price} exceeds the limit "
                    f"{intent.limit_price} for {event.order_id}"
                )
            if intent.side is Side.SELL and event.price < intent.limit_price:
                raise LedgerAccountingError(
                    f"sell fill price {event.price} is below the limit "
                    f"{intent.limit_price} for {event.order_id}"
                )

        reservation = self._reservations.get(event.order_id)
        if reservation is not None:
            # cumulative order-lifetime fee cap applies to buys AND sells
            if (
                reservation.fee_cap_fen
                and reservation.fee_used_fen + event.fee_fen > reservation.fee_cap_fen
            ):
                raise LedgerAccountingError(
                    f"fill fee {event.fee_fen} fen exceeds the order "
                    f"fee cap: cumulative "
                    f"{reservation.fee_used_fen + event.fee_fen} > "
                    f"{reservation.fee_cap_fen} for {event.order_id}"
                )

        if intent.side is Side.BUY:
            expected = (
                self.calendar.next_session(event.trade_date) if self.calendar is not None else None
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
            reservation = self._reservations.get(event.order_id)
            if reservation is not None:
                # cumulative order-lifetime fee cap
                if (
                    reservation.fee_cap_fen
                    and reservation.fee_used_fen + event.fee_fen > reservation.fee_cap_fen
                ):
                    raise LedgerAccountingError(
                        f"fill fee {event.fee_fen} fen exceeds the order "
                        f"fee cap: cumulative "
                        f"{reservation.fee_used_fen + event.fee_fen} > "
                        f"{reservation.fee_cap_fen} for {event.order_id}"
                    )
            debit = event.gross_notional_fen + event.fee_fen
            reserved = reservation.reserved_cash_fen if reservation else 0
            if debit > reserved:
                raise LedgerAccountingError(
                    f"fill draws {debit} fen but only {reserved} fen is "
                    "reserved for this order; the worst case was "
                    "under-reserved"
                )
            self._cash_fen -= debit
            if reservation is not None:
                unfilled = intent.quantity - new_filled
                fee_used = reservation.fee_used_fen + event.fee_fen
                if unfilled > 0:
                    # exact remaining need: worst-case notional of the
                    # unfilled shares plus the remaining fee capacity;
                    # price improvement releases the excess automatically
                    needed = unfilled * reservation.limit_price_fen + (
                        reservation.fee_cap_fen - fee_used
                    )
                    self._reservations[event.order_id] = ActiveReservation(
                        order_id=reservation.order_id,
                        instrument_id=reservation.instrument_id,
                        reserved_cash_fen=needed,
                        reserved_shares=0,
                        limit_price_fen=reservation.limit_price_fen,
                        fee_cap_fen=reservation.fee_cap_fen,
                        fee_used_fen=fee_used,
                    )
                else:
                    # fully filled: no residual reservation survives
                    self._reservations.pop(event.order_id, None)
            self._lots.append(
                PositionLot(
                    lot_id=event.fill_id,
                    instrument_id=intent.instrument_id,
                    quantity=event.quantity,
                    acquired_trade_date=event.trade_date,
                    sellable_from=event.buy_lot_sellable_from,
                )
            )
        else:
            if event.buy_lot_sellable_from is not None:
                raise LedgerAccountingError("sell fill cannot carry buy_lot_sellable_from")
            if event.fee_fen > event.gross_notional_fen:
                raise LedgerAccountingError("sell fee exceeds gross proceeds")
            self._consume_sellable_lots(
                intent.instrument_id,
                event.trade_date,
                event.quantity,
            )
            self._cash_fen += event.gross_notional_fen - event.fee_fen
            sell_reservation = self._reservations.get(event.order_id)
            self._release_reservation(event.order_id, cash_used=0, shares_used=event.quantity)
            # a sell reserves shares only, but it still carries the
            # order-lifetime cumulative fee budget: accumulate fee usage
            # for as long as the order is not fully filled
            if sell_reservation is not None:
                unfilled = intent.quantity - new_filled
                if unfilled > 0:
                    self._reservations[event.order_id] = ActiveReservation(
                        order_id=sell_reservation.order_id,
                        instrument_id=sell_reservation.instrument_id,
                        reserved_cash_fen=0,
                        reserved_shares=unfilled,
                        limit_price_fen=sell_reservation.limit_price_fen,
                        fee_cap_fen=sell_reservation.fee_cap_fen,
                        fee_used_fen=(sell_reservation.fee_used_fen + event.fee_fen),
                    )

        self._fill_ids.add(event.fill_id)
        next_status = (
            OrderStatus.FILLED if new_filled == intent.quantity else OrderStatus.PARTIALLY_FILLED
        )
        self._orders[event.order_id] = replace(
            state,
            status=next_status,
            filled_quantity=new_filled,
            gross_notional_fen=state.gross_notional_fen + event.gross_notional_fen,
            fee_fen=state.fee_fen + event.fee_fen,
        )

    def _apply_manual_fill(self, event: ManualFillImported) -> None:
        if event.account_id != self._initial.account_id:
            raise LedgerTransitionError("manual fill account does not match ledger account")
        if event.fill_id in self._fill_ids:
            raise LedgerTransitionError(f"duplicate fill_id: {event.fill_id}")
        if event.side is Side.BUY:
            expected = self.calendar.next_session(event.trade_date) if self.calendar else None
            if self.calendar is not None and expected is None:
                raise LedgerAccountingError(
                    "calendar cannot derive the next session for an imported buy fill"
                )
            if event.buy_lot_sellable_from != expected:
                raise LedgerAccountingError(
                    "imported buy sellable_from must equal the next verified session"
                )
            debit = event.gross_notional_fen + event.fee_fen
            if debit > self._cash_fen:
                raise LedgerAccountingError(
                    f"manual buy draws {debit} fen but cash is {self._cash_fen} fen"
                )
            self._cash_fen -= debit
            self._lots.append(
                PositionLot(
                    lot_id=event.fill_id,
                    instrument_id=event.instrument_id,
                    quantity=event.quantity,
                    acquired_trade_date=event.trade_date,
                    sellable_from=event.buy_lot_sellable_from,
                )
            )
        else:
            if event.buy_lot_sellable_from is not None:
                raise LedgerAccountingError("imported sell cannot carry buy sellable_from")
            if event.fee_fen > event.gross_notional_fen:
                raise LedgerAccountingError("sell fee exceeds gross proceeds")
            self._consume_sellable_lots(event.instrument_id, event.trade_date, event.quantity)
            self._cash_fen += event.gross_notional_fen - event.fee_fen
        self._fill_ids.add(event.fill_id)

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
            if remaining and lot.instrument_id == instrument_id and lot.sellable_from <= trade_date:
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
