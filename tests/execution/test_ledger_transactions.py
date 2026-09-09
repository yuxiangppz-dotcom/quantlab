"""Strong exception safety for the execution ledger (v0.2.1 Part C).

Every mutation path must behave like a transaction: any Exception OR
BaseException (including KeyboardInterrupt) at any point leaves cash, lots,
orders, reservations, events, event ids, fill ids, and request ids exactly
as they were before the call. The v0.2 implementation mutated live state
and only validated on the happy path, so a mid-batch failure (or an
invariant failure after mutation) left the ledger inconsistent.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantlab.execution.constraints import (
    AShareConstraintEngine,
    FeeScheduleEvidence,
    SuspensionEvidence,
    SuspensionState,
)
from quantlab.execution.ledger import (
    ExecutionLedger,
    FillRecorded,
    LedgerAccountingError,
    OrderCanceled,
    OrderIntended,
    OrderSubmitted,
)
from quantlab.execution.models import (
    AccountSnapshot,
    OrderIntent,
    OrderRequest,
    OrderType,
    PriceBasis,
    Side,
    TimeInForce,
)
from quantlab.execution.rules import (
    InstrumentIdentity,
    PITIdentityBook,
    TradingCalendar,
    default_a_share_rule_book,
)

TZ = ZoneInfo("Asia/Shanghai")
THU = date(2024, 11, 28)
FRI = date(2024, 11, 29)
MON = date(2024, 12, 2)
PRICE = Decimal("10.00")
FEE_CAP = 30_000


def _instant(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(9, 30), TZ) + timedelta(minutes=minute)


def _calendar() -> TradingCalendar:
    return TradingCalendar(
        sessions=(THU, FRI, MON),
        coverage_start=THU,
        coverage_end=MON,
        source_id="txn-calendar",
        source_sha256="a" * 64,
    )


def _identities() -> PITIdentityBook:
    return PITIdentityBook(
        (
            InstrumentIdentity(
                instrument_id="600000.SH",
                exchange="SSE",
                board="MAIN",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="txn-identity",
            ),
        )
    )


def _account(cash_fen: int = 10_000_000) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="txn-account",
        as_of=_instant(FRI, 0),
        cash_fen=cash_fen,
        lots=(),
    )


def _open(day: date) -> SuspensionEvidence:
    return SuspensionEvidence(
        instrument_id="600000.SH",
        trade_date=day,
        state=SuspensionState.VERIFIED_OPEN,
        observed_at=_instant(day),
        source_record_ids=("txn-suspension",),
        coverage_complete=True,
    )


def _fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        schedule_id="synthetic-txn-fee",
        effective_from=date(2020, 1, 1),
        effective_to=date(2027, 12, 31),
        broker_commission_exact=True,
        statutory_fees_exact=True,
        source_sha256="b" * 64,
    )


def _engine() -> AShareConstraintEngine:
    return AShareConstraintEngine(
        calendar=_calendar(), identities=_identities(),
        rules=default_a_share_rule_book(),
    )


def _intent(order_id: str, quantity: int = 300, minute: int = 1) -> OrderIntent:
    from quantlab.execution.models import fingerprint_fee_cap_quote

    return OrderIntent(
        order_id=order_id,
        instruction_id="txn-instruction",
        instrument_id="600000.SH",
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=PRICE,
        intended_trade_date=FRI,
        created_at=_instant(FRI, minute),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="txn-price",
        time_in_force=TimeInForce.DAY,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(_quote()),
    )


def _request(intent: OrderIntent, minute: int) -> OrderRequest:
    return OrderRequest(
        request_id=f"txn-request-{intent.order_id}",
        order_id=intent.order_id,
        instrument_id=intent.instrument_id,
        side=intent.side,
        quantity=intent.quantity,
        order_type=intent.order_type,
        limit_price=intent.limit_price,
        intended_trade_date=intent.intended_trade_date,
        created_at=_instant(intent.intended_trade_date, minute),
        limit_price_basis=intent.limit_price_basis,
        limit_price_source_id=intent.limit_price_source_id,
        time_in_force=intent.time_in_force,
    )


def _intend(ledger: ExecutionLedger, order_id: str, minute: int = 1):
    intent = _intent(order_id, minute=minute)
    ledger.append(
        OrderIntended(f"txn-{order_id}-intended", _instant(FRI, minute), intent)
    )
    return intent


def _assess(ledger: ExecutionLedger, engine, intent, minute: int):
    result = engine.assess(
        intent,
        ledger.snapshot(_instant(FRI, minute)),
        _instant(FRI, minute),
        suspension=_open(FRI),
        fee_schedule=_fee_schedule(),
        daily_bar_available=None,
        availability_fingerprint=ledger.availability_fingerprint(),
    )
    ledger.append(result.event)
    return result


def _submit_event(
    intent: OrderIntent,
    minute: int,
    fee: int = FEE_CAP,
    authority=None,
    ledger=None,
):
    from quantlab.execution.orchestration import materialize_bound_request

    if authority is not None and ledger is not None:
        stored = ledger.order(intent.order_id).authority
        request = materialize_bound_request(
            intent,
            stored,
            fee_quote=_quote(),
            stored_authority=stored,
        )
        request = __import__("dataclasses").replace(
            request, created_at=_instant(FRI, minute)
        )
        return OrderSubmitted(
            f"txn-{intent.order_id}-submitted",
            _instant(FRI, minute),
            request,
            worst_case_fee_fen=fee,
            availability_fingerprint=request.availability_fingerprint,
            fee_quote_fingerprint=request.fee_quote_fingerprint,
            fee_quote=_quote(),
        )
    return OrderSubmitted(
        f"txn-{intent.order_id}-submitted",
        _instant(FRI, minute),
        _request(intent, minute),
        worst_case_fee_fen=fee,
        availability_fingerprint=None,
    )


def _state(ledger: ExecutionLedger) -> tuple:
    """Full white-box state tuple for before/after comparison."""
    return (
        ledger.cash_fen,
        ledger.lots,
        ledger.orders,
        ledger.reservations,
        ledger.events,
        dict(ledger._events_by_id),  # noqa: SLF001
        set(ledger._fill_ids),  # noqa: SLF001
        set(ledger._request_ids),  # noqa: SLF001
        dict(ledger._orders),  # noqa: SLF001
        dict(ledger._reservations),  # noqa: SLF001
    )


def _quote() -> "object":
    from quantlab.execution.planning import FeeCapQuote

    return FeeCapQuote(
        instrument_id="600000.SH",
        account_id="txn-account",
        trade_date=FRI,
        cap_fen=FEE_CAP,
        # the quote's evidence IS the synthetic schedule it derives from
        evidence_id="synthetic-txn-fee",
        source_fingerprint="b" * 64,
        synthetic=True,
    )


def _prepared_batch_ledger(self) -> tuple[ExecutionLedger, list]:
    """Ledger with three intended+validated orders ready for submission."""
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    engine = _engine()
    events = []
    authorities = []
    for index, order_id in enumerate(("txn-a", "txn-b", "txn-c")):
        minute = 1 + index * 10
        intent = _intend(ledger, order_id, minute=minute)
        _assess(ledger, engine, intent, minute=minute + 2)
        authorities.append(ledger.order(order_id).authority)
    # submissions come after every assessment (batch wall-clock order)
    for index, order_id in enumerate(("txn-a", "txn-b", "txn-c")):
        intent = ledger.order(order_id).intent
        events.append(
            _submit_event(
                intent,
                minute=40 + index,
                authority=authorities[index],
                ledger=ledger,
            )
        )
    return ledger, events


def test_batch_middle_keyboardinterrupt_rolls_back_everything() -> None:
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    original = ExecutionLedger._apply_submitted
    calls = {"n": 0}

    def failing(self, event):
        calls["n"] += 1
        original(self, event)
        if calls["n"] == 2:
            raise KeyboardInterrupt

    ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.submit_orders([(event, _quote()) for event in events])
    finally:
        ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]
    assert _state(ledger) == before


@pytest.mark.parametrize("fail_index", [0, 1, 2])
@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_batch_any_position_any_error_rolls_back(fail_index: int, error) -> None:
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    original = ExecutionLedger._apply_submitted
    calls = {"n": 0}

    def failing(self, event):
        if calls["n"] == fail_index:
            calls["n"] += 1
            raise error(f"injected at {fail_index}")
        calls["n"] += 1
        original(self, event)

    ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
    try:
        with pytest.raises((RuntimeError, KeyboardInterrupt)):
            ledger.submit_orders([(event, _quote()) for event in events])
    finally:
        ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]
    assert _state(ledger) == before
    # the batch stays fully retryable after rollback
    ledger.submit_orders([(event, _quote()) for event in events])
    assert all(
        order.status.value == "submitted" for order in ledger.orders
    )


def test_invariant_failure_after_mutation_leaves_state_untouched() -> None:
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    original = ExecutionLedger._check_invariants

    def failing(self):
        raise LedgerAccountingError("injected invariant failure")

    ExecutionLedger._check_invariants = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(LedgerAccountingError):
            ledger.submit_orders([(events[0], _quote())])
    finally:
        ExecutionLedger._check_invariants = original  # type: ignore[method-assign]
    assert _state(ledger) == before


def test_fill_after_reservation_injection_rolls_back() -> None:
    ledger, events = _prepared_batch_ledger(None)
    ledger.submit_orders([(events[0], _quote())])
    before = _state(ledger)
    fill = FillRecorded(
        event_id="txn-fill-1",
        fill_id="txn-fill-1",
        occurred_at=_instant(FRI, 40),
        order_id="txn-a",
        trade_date=FRI,
        quantity=100,
        price=PRICE,
        gross_notional_fen=100_000,
        fee_fen=500,
        buy_lot_sellable_from=MON,
    )
    original = ExecutionLedger._apply_fill

    def failing(self, event):
        original(self, event)
        # inject after the reservation draw-down, before the event commits
        raise KeyboardInterrupt

    ExecutionLedger._apply_fill = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.append(fill)
    finally:
        ExecutionLedger._apply_fill = original  # type: ignore[method-assign]
    assert _state(ledger) == before


def test_single_append_baseexception_rolls_back() -> None:
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    original = ExecutionLedger._apply_submitted

    def failing(self, event):
        raise KeyboardInterrupt

    ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(KeyboardInterrupt):
            ledger.append(events[0])
    finally:
        ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]
    assert _state(ledger) == before


def test_replay_and_idempotence_survive_transactions() -> None:
    ledger, events = _prepared_batch_ledger(None)
    ledger.submit_orders([(event, _quote()) for event in events])
    first = ledger.submit_orders  # keep reference alive
    assert first is not None
    replayed = ExecutionLedger.replay(
        _account(), ledger.events, calendar=_calendar()
    )
    assert replayed.events == ledger.events
    assert replayed.reservations == ledger.reservations
    assert replayed.orders == ledger.orders
    # duplicate submission events stay idempotent no-ops (compare with
    # the ledger-recorded event, which carries the quote binding)
    duplicate = ledger.append(ledger.events[-3])
    assert duplicate is False


def test_cancel_after_batch_failure_releases_nothing_extra() -> None:
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    original = ExecutionLedger._apply_submitted
    calls = {"n": 0}

    def failing(self, event):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("third submission fails")
        original(self, event)

    # fail the THIRD submission inside one atomic bound batch: the first
    # two reservations must roll back with the failed third
    ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            ledger.submit_orders([(event, _quote()) for event in events])
    finally:
        ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]
    # the failed third submission left nothing behind: no request ids,
    # no reservations, and EVERY order back at its validated state —
    # the whole bound batch rolled back as one transaction
    assert ledger._request_ids == set()  # noqa: SLF001
    assert ledger.order("txn-c").status.value == "validated"
    assert ledger.order("txn-c").request_id is None
    assert ledger.order("txn-a").status.value == "validated"
    assert ledger.order("txn-b").status.value == "validated"
    assert ledger.reservations == ()
    assert ledger.reserved_cash_fen() == 0
    assert _state(ledger) == before  # zero residue versus the pre-batch state


# ---------------------------------------- event-index commit boundary ----
# The two index writes that commit an event (ordered list append and
# event-id map insert) must sit INSIDE the same snapshot/restore boundary
# as apply and invariant validation: a BaseException at either write
# leaves no applied mutation and no half-indexed event behind.


class _PoisonEventList(list):
    """Ordered event log whose append raises (first index write)."""

    def __init__(self, items, error: BaseException) -> None:
        super().__init__(items)
        self._error = error

    def append(self, _item) -> None:
        raise self._error


class _PoisonEventIndex(dict):
    """Event-id index whose insert raises (second index write)."""

    def __init__(self, items, error: BaseException) -> None:
        super().__init__(items)
        self._error = error

    def __setitem__(self, _key, _value) -> None:
        raise self._error


def _direct_mutation_event(mutation: str, ledger: ExecutionLedger):
    """A representative direct-append event for each mutation family."""
    if mutation == "submission":
        # a validated order's bound submission appended DIRECTLY (not via
        # submit_orders) on the still-unmutated pre-batch state
        intent = ledger.order("txn-b").intent
        return _submit_event(
            intent, minute=41, authority=object(), ledger=ledger
        )
    if mutation == "fill":
        ledger.submit_orders([( _events_for(ledger)[0], _quote())])
        return FillRecorded(
            event_id="txn-inject-fill",
            fill_id="txn-inject-fill",
            occurred_at=_instant(FRI, 45),
            order_id="txn-a",
            trade_date=FRI,
            quantity=100,
            price=PRICE,
            gross_notional_fen=100_000,
            fee_fen=500,
            buy_lot_sellable_from=MON,
        )
    if mutation == "terminal":
        ledger.submit_orders([(_events_for(ledger)[0], _quote())])
        return OrderCanceled(
            "txn-inject-cancel", _instant(FRI, 45), "txn-a", "injected"
        )
    raise AssertionError(f"unknown mutation {mutation!r}")


def _events_for(ledger: ExecutionLedger) -> list:
    """Bound submission events for the ledger's validated orders."""
    events = []
    for index, order_id in enumerate(("txn-a", "txn-b", "txn-c")):
        intent = ledger.order(order_id).intent
        events.append(
            _submit_event(
                intent, minute=40 + index, authority=object(), ledger=ledger
            )
        )
    return events


@pytest.mark.parametrize("mutation", ["submission", "fill", "terminal"])
def test_event_list_append_injection_restores_exact_state(mutation) -> None:
    """BaseException at self._events.append(event) must restore everything."""
    ledger, _ = _prepared_batch_ledger(None)
    event = _direct_mutation_event(mutation, ledger)
    before = _state(ledger)
    ledger._events = _PoisonEventList(  # noqa: SLF001
        ledger._events, KeyboardInterrupt()
    )
    with pytest.raises(KeyboardInterrupt):
        ledger.append(event)
    assert _state(ledger) == before


@pytest.mark.parametrize("mutation", ["submission", "fill", "terminal"])
def test_event_index_insert_injection_restores_exact_state(mutation) -> None:
    """BaseException at the event-id insert (after the ordered append
    succeeded) must restore everything, including the ordered log."""
    ledger, _ = _prepared_batch_ledger(None)
    event = _direct_mutation_event(mutation, ledger)
    before = _state(ledger)
    ledger._events_by_id = _PoisonEventIndex(  # noqa: SLF001
        ledger._events_by_id, KeyboardInterrupt()
    )
    with pytest.raises(KeyboardInterrupt):
        ledger.append(event)
    assert _state(ledger) == before


def test_batch_event_index_injection_rolls_back_and_stays_retryable() -> None:
    """Nested boundaries: an index-write failure inside a bound batch is
    restored by BOTH the append boundary and the batch boundary, and the
    restored containers stay healthy for a retry."""
    ledger, events = _prepared_batch_ledger(None)
    before = _state(ledger)
    ledger._events_by_id = _PoisonEventIndex(  # noqa: SLF001
        ledger._events_by_id, KeyboardInterrupt()
    )
    with pytest.raises(KeyboardInterrupt):
        ledger.submit_orders([(event, _quote()) for event in events])
    assert _state(ledger) == before
    # the rollback restored plain containers: the same batch commits
    ledger.submit_orders([(event, _quote()) for event in events])
    assert all(order.status.value == "submitted" for order in ledger.orders)
    assert len(ledger.events) == len(before[4]) + 3


def test_event_index_boundary_preserves_duplicate_semantics() -> None:
    """Moving the index writes inside the boundary must not weaken the
    exact-duplicate idempotency or different-payload rejection."""
    ledger, events = _prepared_batch_ledger(None)
    ledger.submit_orders([(events[0], _quote())])
    committed = ledger.events[-1]
    # exact duplicate: idempotent no-op
    assert ledger.append(committed) is False
    # same event_id with a different payload: rejected, nothing changes
    before = _state(ledger)
    with pytest.raises(Exception, match="reused with different payload"):
        ledger.append(
            replace(
                committed,
                worst_case_fee_fen=committed.worst_case_fee_fen + 1,
            )
        )
    assert _state(ledger) == before
