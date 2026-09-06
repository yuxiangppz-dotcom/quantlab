"""Strong exception safety for the execution ledger (v0.2.1 Part C).

Every mutation path must behave like a transaction: any Exception OR
BaseException (including KeyboardInterrupt) at any point leaves cash, lots,
orders, reservations, events, event ids, fill ids, and request ids exactly
as they were before the call. The v0.2 implementation mutated live state
and only validated on the happy path, so a mid-batch failure (or an
invariant failure after mutation) left the ledger inconsistent.
"""

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


def _submit_event(intent: OrderIntent, minute: int, fee: int = FEE_CAP):
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
        evidence_id="txn-fee-quote",
        source_fingerprint="6" * 64,
        synthetic=True,
    )


def _prepared_batch_ledger(self) -> tuple[ExecutionLedger, list]:
    """Ledger with three intended+validated orders ready for submission."""
    ledger = ExecutionLedger(_account(), calendar=_calendar())
    engine = _engine()
    events = []
    for index, order_id in enumerate(("txn-a", "txn-b", "txn-c")):
        minute = 1 + index * 10
        intent = _intend(ledger, order_id, minute=minute)
        _assess(ledger, engine, intent, minute=minute + 2)
        # submissions come after every assessment (batch wall-clock order)
        events.append(_submit_event(intent, minute=40 + index))
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

    # fail the THIRD submission after the first two succeeded, through the
    # public single-append path, mixing ledger APIs like a real caller
    ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
    try:
        ledger.append(events[0])
        ledger.append(events[1])
        with pytest.raises(RuntimeError):
            ledger.append(events[2])
    finally:
        ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]
    # the failed third submission left nothing behind: no request id,
    # no reservation, and the order back at its validated state
    assert ledger._request_ids == set(  # noqa: SLF001
        event.request.request_id for event in events[:2]
    )
    assert ledger.order("txn-c").status.value == "validated"
    assert ledger.order("txn-c").request_id is None
    assert all(
        reservation.order_id in {"txn-a", "txn-b"}
        for reservation in ledger.reservations
    )
    assert ledger.order("txn-a").status.value == "submitted"
    # canceling the two live orders releases their reservations exactly
    ledger.append(
        OrderCanceled(
            "txn-cancel-a", _instant(FRI, 50), "txn-a", "txn cancel"
        )
    )
    ledger.append(
        OrderCanceled(
            "txn-cancel-b", _instant(FRI, 51), "txn-b", "txn cancel"
        )
    )
    assert ledger.reserved_cash_fen() == 0
    assert _state(ledger) != before  # the two good submissions did commit
