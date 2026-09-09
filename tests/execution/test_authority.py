"""Part C: immutable assessment authority on the production order path.

Every VALIDATED order must persist WHICH assessment authorized it
(event id, intent fingerprint, decision fingerprint, availability
fingerprint, full dimension results, fee-schedule binding) and the
submission must reference that stored authority. v0.2.1 accepted
caller-supplied fingerprints and bare-integer fee caps, so a submission
could present a fresh fingerprint instead of the stored authority.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from quantlab.execution.constraints import (
    AShareConstraintEngine,
    FeeScheduleEvidence,
    SuspensionEvidence,
    SuspensionState,
)
from quantlab.execution.ledger import (
    ExecutionLedger,
    LedgerTransitionError,
    OrderIntended,
    OrderSubmitted,
)
from quantlab.execution.models import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    OrderIntent,
    OrderType,
    PriceBasis,
    Side,
    TimeInForce,
)
from quantlab.execution.planning import (
    AvailabilityState,
    ExecutionStateView,
    FeeCapQuote,
    fingerprint_fee_cap_quote,
    fingerprint_order_intent,
)
from quantlab.execution.rules import (
    InstrumentIdentity,
    PITIdentityBook,
    TradingCalendar,
    default_a_share_rule_book,
)

THU = date(2024, 11, 28)
FRI = date(2024, 11, 29)
MON = date(2024, 12, 2)


def _instant(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(9, 30), EXCHANGE_TIMEZONE) + timedelta(
        minutes=minute
    )


def _calendar() -> TradingCalendar:
    return TradingCalendar(
        sessions=(THU, FRI, MON), coverage_start=THU, coverage_end=MON,
        source_id="authority-synthetic", source_sha256="2" * 64,
    )


def _account(cash: int = 5_000_000) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="authority-account",
        as_of=_instant(FRI),
        cash_fen=cash,
        lots=(),
    )


def _identities() -> PITIdentityBook:
    return PITIdentityBook((
        InstrumentIdentity(
            instrument_id="600000.SH",
            exchange="SSE",
            board="MAIN",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            source_record_id="authority-identity",
        ),
    ))


def _engine(calendar: TradingCalendar) -> AShareConstraintEngine:
    return AShareConstraintEngine(
        calendar=calendar, identities=_identities(),
        rules=default_a_share_rule_book(),
    )


def _open(day: date) -> SuspensionEvidence:
    return SuspensionEvidence(
        instrument_id="600000.SH",
        trade_date=day,
        state=SuspensionState.VERIFIED_OPEN,
        observed_at=_instant(day),
        source_record_ids=("authority-suspension",),
        coverage_complete=True,
    )


def _fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        schedule_id="authority-fee-schedule",
        effective_from=date(2020, 1, 1),
        effective_to=date(2027, 12, 31),
        broker_commission_exact=True,
        statutory_fees_exact=True,
        source_sha256="9" * 64,
    )


def _intent(
    order_id: str = "authority-order",
    minute: int = 1,
    *,
    side: Side = Side.BUY,
    quantity: int = 100,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instruction_id="authority-instruction",
        instrument_id="600000.SH",
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        intended_trade_date=MON,
        created_at=_instant(FRI, minute),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="authority-price",
        fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
    )


def _fee_quote() -> FeeCapQuote:
    return FeeCapQuote(
        instrument_id="600000.SH",
        account_id="authority-account",
        trade_date=MON,
        cap_fen=30_000,
        # the quote's evidence IS the synthetic schedule it derives from
        evidence_id="authority-fee-schedule",
        source_fingerprint="9" * 64,
        synthetic=True,
    )


def _state_view(account: AccountSnapshot) -> ExecutionStateView:
    return ExecutionStateView(
        account=account,
        state=AvailabilityState(
            account_id=account.account_id,
            as_of=account.as_of,
            trade_date=MON,
            settled_cash_fen=account.cash_fen,
            lots=account.lots,
            reservations=(),
            available_cash_fen=account.cash_fen,
            available_sellable_shares={},
        ),
    )


def _assess(ledger, engine, intent, minute: int):
    # the suspension evidence observes MON; assess on MON so the
    # observation is never from the future
    return engine.assess(
        intent,
        ledger.snapshot(_instant(MON, minute + 1)),
        _instant(MON, minute + 1),
        suspension=_open(MON),
        fee_schedule=_fee_schedule(),
        daily_bar_available=None,
        availability_fingerprint=ledger.availability_fingerprint(),
    )


def _submit_validated_order(order_id: str = "authority-order"):
    """Happy path: intent -> assessment -> stored authority."""
    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent(order_id)
    ledger.append(OrderIntended(f"ev-{order_id}-intent", intent.created_at, intent))
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    return ledger, engine, intent, result


def test_production_assessment_without_authority_is_rejected() -> None:
    """A bare low-level assessment may exist, but its order can never be
    submitted: the submission boundary demands the stored authority."""
    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent()
    ledger.append(OrderIntended("ev-intent", intent.created_at, intent))
    result = _assess(ledger, engine, intent, minute=2)
    event = replace(result.event, authority=None)
    ledger.append(event)  # bare assessment accepted as a non-production fixture
    assert ledger.order(intent.order_id).authority is None
    from quantlab.execution.orchestration import materialize_bound_request

    with pytest.raises(Exception, match="assessment authority"):
        materialize_bound_request(
            intent,
            replace(
                result.event.authority,
                assessment_event_id="ev-intent",
            ),
            fee_quote=_fee_quote(),
            stored_authority=ledger.order(intent.order_id).authority,
        )


def test_authority_with_tampered_intent_fingerprint_is_rejected() -> None:
    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent()
    ledger.append(OrderIntended("ev-intent", intent.created_at, intent))
    result = _assess(ledger, engine, intent, minute=2)
    tampered = replace(
        result.event,
        authority=replace(
            result.event.authority,
            intent_fingerprint="sha256:" + "0" * 64,
        ),
    )
    with pytest.raises(LedgerTransitionError, match="intent fingerprint"):
        ledger.append(tampered)


def test_authority_with_tampered_decision_fingerprint_is_rejected() -> None:
    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent()
    ledger.append(OrderIntended("ev-intent", intent.created_at, intent))
    result = _assess(ledger, engine, intent, minute=2)
    tampered = replace(
        result.event,
        authority=replace(
            result.event.authority,
            decision_fingerprint="sha256:" + "1" * 64,
        ),
    )
    with pytest.raises(LedgerTransitionError, match="decision fingerprint"):
        ledger.append(tampered)


def test_submission_must_reference_the_stored_authority() -> None:
    ledger, engine, intent, result = _submit_validated_order()
    authority = ledger.order(intent.order_id).authority
    assert authority is not None
    assert authority.assessment_event_id
    assert authority.intent_fingerprint == fingerprint_order_intent(intent)
    assert authority.availability_fingerprint
    # a submission presenting a DIFFERENT assessment event id fails
    from quantlab.execution.orchestration import materialize_bound_request

    forged_authority = replace(
        authority, assessment_event_id="ev-forged-assessment"
    )
    with pytest.raises(Exception, match="stored in"):
        materialize_bound_request(
            intent,
            forged_authority,
            fee_quote=_fee_quote(),
            stored_authority=ledger.order(intent.order_id).authority,
        )


def test_foreign_reservation_makes_the_old_authority_stale() -> None:
    ledger, engine, intent, result = _submit_validated_order()
    from quantlab.execution.orchestration import materialize_bound_request

    request = materialize_bound_request(
        intent,
        ledger.order(intent.order_id).authority,
        fee_quote=_fee_quote(),
        stored_authority=ledger.order(intent.order_id).authority,
    )
    # a foreign reservation changes the availability state
    ledger._reservations["foreign-order"] = __import__(
        "quantlab.execution.ledger", fromlist=["ActiveReservation"]
    ).ActiveReservation(
        order_id="foreign-order",
        instrument_id="600000.SH",
        limit_price_fen=1_000,
        fee_cap_fen=30_000,
        fee_used_fen=0,
        reserved_cash_fen=4_000_000,
        reserved_shares=0,
    )
    event = OrderSubmitted(
        f"ev-{intent.order_id}-submitted",
        request.created_at,
        request,
        worst_case_fee_fen=_fee_quote().cap_fen,
        availability_fingerprint=request.availability_fingerprint,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
        fee_quote=_fee_quote(),
    )
    with pytest.raises(LedgerTransitionError, match="availability state"):
        ledger.append(event)


def test_bare_integer_fee_submission_cannot_bypass_the_typed_quote() -> None:
    """append(OrderSubmitted(..., worst_case_fee_fen=int)) is not an
    authorized entry point: the event must embed the typed quote."""
    ledger, engine, intent, result = _submit_validated_order()
    from quantlab.execution.orchestration import materialize_bound_request

    request = materialize_bound_request(
        intent,
        ledger.order(intent.order_id).authority,
        fee_quote=_fee_quote(),
        stored_authority=ledger.order(intent.order_id).authority,
    )
    with pytest.raises(Exception, match="typed FeeCapQuote"):
        ledger.append(
            OrderSubmitted(
                f"ev-{intent.order_id}-submitted",
                request.created_at,
                request,
                worst_case_fee_fen=30_000,
                availability_fingerprint=request.availability_fingerprint,
                fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
            )
        )


def test_bound_batch_commits_two_orders_on_one_prebatch_state() -> None:
    """Two orders bound to the SAME pre-batch availability state commit
    atomically: the first member's reservation cannot stale the second."""
    calendar = _calendar()
    ledger = ExecutionLedger(_account(cash=5_000_000), calendar=calendar)
    engine = _engine(calendar)
    from quantlab.execution.orchestration import materialize_bound_request

    base = ledger.availability_fingerprint()
    intents = []
    for index in range(2):
        order_id = f"authority-batch-{index}"
        intent = _intent(order_id, minute=1 + index * 10)
        ledger.append(
            OrderIntended(
                f"ev-{order_id}-intent", intent.created_at, intent
            )
        )
        intents.append(intent)
    # all assessments happen AFTER every intent (wall-clock order)
    submissions = []
    for index, intent in enumerate(intents):
        result = _assess(ledger, engine, intent, minute=2 + index * 10)
        ledger.append(result.event)
        stored = ledger.order(intent.order_id).authority
        assert stored.availability_fingerprint == base
        request = materialize_bound_request(
            intent, stored, fee_quote=_fee_quote(), stored_authority=stored
        )
        # submissions come after every assessment (batch wall-clock order)
        from dataclasses import replace as _replace

        request = _replace(request, created_at=_instant(MON, 20 + index))
        submissions.append(
            (
                OrderSubmitted(
                    f"ev-{intent.order_id}-submitted",
                    request.created_at,
                    request,
                    worst_case_fee_fen=_fee_quote().cap_fen,
                    availability_fingerprint=(
                        request.availability_fingerprint
                    ),
                    fee_quote_fingerprint=fingerprint_fee_cap_quote(
                        _fee_quote()
                    ),
                    fee_quote=_fee_quote(),
                ),
                _fee_quote(),
            )
        )
    ledger.submit_orders(submissions)
    assert ledger.order("authority-batch-0").status.value == "submitted"
    assert ledger.order("authority-batch-1").status.value == "submitted"


def test_foreign_drift_before_the_batch_fails_every_member() -> None:
    """A foreign reservation between assessment and submission invalidates
    the whole batch: every member binds the now-stale state."""
    calendar = _calendar()
    ledger = ExecutionLedger(_account(cash=5_000_000), calendar=calendar)
    engine = _engine(calendar)
    from quantlab.execution.orchestration import materialize_bound_request

    stale_base = ledger.availability_fingerprint()
    intents = []
    for index in range(2):
        order_id = f"authority-stale-{index}"
        intent = _intent(order_id, minute=1 + index * 10)
        ledger.append(
            OrderIntended(
                f"ev-{order_id}-intent", intent.created_at, intent
            )
        )
        intents.append(intent)
    # all assessments happen AFTER every intent (wall-clock order)
    submissions = []
    for index, intent in enumerate(intents):
        result = _assess(ledger, engine, intent, minute=2 + index * 10)
        ledger.append(result.event)
        stored = ledger.order(intent.order_id).authority
        request = materialize_bound_request(
            intent, stored, fee_quote=_fee_quote(), stored_authority=stored
        )
        from dataclasses import replace as _replace

        request = _replace(request, created_at=_instant(MON, 20 + index))
        submissions.append(
            (
                OrderSubmitted(
                    f"ev-{intent.order_id}-submitted",
                    request.created_at,
                    request,
                    worst_case_fee_fen=_fee_quote().cap_fen,
                    availability_fingerprint=request.availability_fingerprint,
                    fee_quote_fingerprint=fingerprint_fee_cap_quote(
                        _fee_quote()
                    ),
                    fee_quote=_fee_quote(),
                ),
                _fee_quote(),
            )
        )
    # foreign drift: a reservation on an unrelated order drains cash
    ledger._reservations["foreign-order"] = __import__(
        "quantlab.execution.ledger", fromlist=["ActiveReservation"]
    ).ActiveReservation(
        order_id="foreign-order",
        instrument_id="600000.SH",
        limit_price_fen=1_000,
        fee_cap_fen=30_000,
        fee_used_fen=0,
        reserved_cash_fen=4_000_000,
        reserved_shares=0,
    )
    assert ledger.availability_fingerprint() != stale_base
    from quantlab.execution.ledger import LedgerTransitionError

    with pytest.raises(LedgerTransitionError, match="pre-batch state"):
        ledger.submit_orders(submissions)
    for index in range(2):
        assert (
            ledger.order(f"authority-stale-{index}").status.value
            == "validated"
        )
        assert f"authority-stale-{index}" not in ledger._reservations


def test_friday_cannot_submit_a_monday_day_request() -> None:
    """DAY commit boundary: the Shanghai-local submission date must equal
    the intended trade date; a Friday-created Monday intent may exist as a
    future intention but Friday can never submit its Monday request."""
    from quantlab.execution.orchestration import materialize_bound_request

    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent("authority-monday")
    ledger.append(
        OrderIntended("ev-authority-monday-intent", intent.created_at, intent)
    )
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    stored = ledger.order("authority-monday").authority
    request = materialize_bound_request(
        intent, stored, fee_quote=_fee_quote(), stored_authority=stored
    )
    # the request is dated TUESDAY while the order is intended for Monday:
    # the submission boundary must refuse any non-intended submission date
    tue = date(2024, 12, 3)
    from dataclasses import replace as _replace

    late_request = _replace(request, created_at=_instant(tue))
    event = OrderSubmitted(
        "ev-authority-monday-submitted",
        late_request.created_at,
        late_request,
        worst_case_fee_fen=_fee_quote().cap_fen,
        availability_fingerprint=request.availability_fingerprint,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
        fee_quote=_fee_quote(),
    )
    with pytest.raises(LedgerTransitionError, match="intended trade date"):
        ledger.append(event)


def test_sell_order_lifetime_fee_budget_is_enforced() -> None:
    """Sell orders do not freeze cash but still carry the order-lifetime
    cumulative fee cap: exceeding it rejects the fill."""
    lot = __import__(
        "quantlab.execution.models", fromlist=["PositionLot"]
    ).PositionLot(
        lot_id="authority-sell-lot",
        instrument_id="600000.SH",
        quantity=100,
        acquired_trade_date=THU,
        sellable_from=FRI,
    )
    account = replace(_account(), lots=(lot,))
    calendar = _calendar()
    ledger = ExecutionLedger(account, calendar=calendar)
    engine = _engine(calendar)
    from quantlab.execution.orchestration import materialize_bound_request

    intent = _intent("authority-sell", side=Side.SELL, quantity=100)
    small_cap = replace(_fee_quote(), cap_fen=1_000)
    intent = replace(
        intent,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(small_cap),
    )
    ledger.append(
        OrderIntended("ev-authority-sell-intent", intent.created_at, intent)
    )
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    stored = ledger.order("authority-sell").authority
    request = materialize_bound_request(
        intent, stored, fee_quote=small_cap, stored_authority=stored
    )
    ledger.append(
        OrderSubmitted(
            "ev-authority-sell-submitted",
            request.created_at,
            request,
            worst_case_fee_fen=small_cap.cap_fen,
            availability_fingerprint=request.availability_fingerprint,
            fee_quote_fingerprint=fingerprint_fee_cap_quote(small_cap),
            fee_quote=small_cap,
        )
    )
    assert ledger.reserved_cash_fen() == 0
    assert ledger.reserved_sellable_shares("600000.SH") == 100
    first = __import__(
        "quantlab.execution.ledger", fromlist=["FillRecorded"]
    ).FillRecorded(
        event_id="ev-sell-fill-1",
        fill_id="authority-sell-f1",
        occurred_at=_instant(MON, 20),
        order_id="authority-sell",
        trade_date=MON,
        quantity=40,
        price=Decimal("10.00"),
        gross_notional_fen=40_000,
        fee_fen=800,
    )
    ledger.append(first)
    second = __import__(
        "quantlab.execution.ledger", fromlist=["FillRecorded"]
    ).FillRecorded(
        event_id="ev-sell-fill-2",
        fill_id="authority-sell-f2",
        occurred_at=_instant(MON, 21),
        order_id="authority-sell",
        trade_date=MON,
        quantity=60,
        price=Decimal("10.00"),
        gross_notional_fen=60_000,
        fee_fen=300,
    )
    with pytest.raises(Exception, match="fee cap"):
        ledger.append(second)
    assert ledger.order("authority-sell").fee_fen == 800


def test_happy_path_authority_chain_end_to_end() -> None:
    ledger, engine, intent, result = _submit_validated_order()
    from quantlab.execution.orchestration import materialize_bound_request

    authority = ledger.order(intent.order_id).authority
    assert authority.decision_fingerprint
    assert authority.fee_schedule_evidence_id == "authority-fee-schedule"
    assert authority.fee_schedule_source_fingerprint == "9" * 64
    assert authority.plan_id is None and authority.leg_id is None
    request = materialize_bound_request(
        intent,
        authority,
        fee_quote=_fee_quote(),
        stored_authority=ledger.order(intent.order_id).authority,
    )
    assert request.assessment_event_id == authority.assessment_event_id
    assert (
        request.assessment_decision_fingerprint
        == authority.decision_fingerprint
    )
    assert request.availability_fingerprint == authority.availability_fingerprint
    assert request.fee_quote_fingerprint == fingerprint_fee_cap_quote(
        _fee_quote()
    )
    assert request.time_in_force is TimeInForce.DAY
    ledger.append(
        OrderSubmitted(
            f"ev-{intent.order_id}-submitted",
            request.created_at,
            request,
            worst_case_fee_fen=_fee_quote().cap_fen,
            availability_fingerprint=request.availability_fingerprint,
            fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
            fee_quote=_fee_quote(),
        )
    )
    assert ledger.order(intent.order_id).status.value == "submitted"
    reservation = ledger._reservations[intent.order_id]
    assert reservation.reserved_cash_fen == 130_000


def test_blocked_authority_cannot_materialize_a_request() -> None:
    from quantlab.execution.orchestration import materialize_bound_request

    ledger, engine, intent, result = _submit_validated_order()
    authority = replace(
        ledger.order(intent.order_id).authority,
        decision_fingerprint="sha256:" + "3" * 64,
    )
    # an authority whose decision fingerprint does not match the stored
    # one cannot materialize (the ledger would reject it)
    with pytest.raises(Exception, match="stored in"):
        materialize_bound_request(
            intent,
            authority,
            fee_quote=_fee_quote(),
            stored_authority=ledger.order(intent.order_id).authority,
        )


# ----------------------- fee quote to assessment-authority binding -------
# The typed quote a submission reserves cash from must derive from the
# EXACT fee schedule the stored assessment authorized, and the intent and
# request must both carry that quote's canonical fingerprint (a missing
# None fingerprint is not fee lineage).


def _submit_with_quote(quote: FeeCapQuote, *, intent: OrderIntent | None = None):
    """Assess through the real engine with _fee_schedule(), then submit
    ``quote`` (with fully consistent fingerprints) via direct append."""
    from quantlab.execution.orchestration import materialize_bound_request

    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = intent or _intent("authority-binding")
    ledger.append(
        OrderIntended("ev-binding-intent", intent.created_at, intent)
    )
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    stored = ledger.order(intent.order_id).authority
    request = materialize_bound_request(
        intent, stored, fee_quote=quote, stored_authority=stored
    )
    event = OrderSubmitted(
        f"ev-{intent.order_id}-submitted",
        request.created_at,
        request,
        worst_case_fee_fen=quote.cap_fen,
        availability_fingerprint=request.availability_fingerprint,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(quote),
        fee_quote=quote,
    )
    ledger.append(event)
    return ledger, intent


def test_submission_quote_must_match_the_assessed_fee_schedule_id() -> None:
    """A quote whose evidence id names a DIFFERENT schedule than the one
    the stored assessment authorized cannot reserve cash."""
    foreign_schedule_quote = replace(
        _fee_quote(), evidence_id="authority-other-schedule"
    )
    intent = _intent("authority-binding")
    intent = replace(
        intent,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(foreign_schedule_quote),
    )
    with pytest.raises(Exception, match="fee schedule"):
        _submit_with_quote(foreign_schedule_quote, intent=intent)


def test_submission_quote_must_match_the_assessed_fee_schedule_source() -> None:
    """A quote whose source fingerprint differs from the authorized
    schedule's bytes is contradictory fee evidence."""
    foreign_source_quote = replace(_fee_quote(), source_fingerprint="5" * 64)
    intent = _intent("authority-binding")
    intent = replace(
        intent,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(foreign_source_quote),
    )
    with pytest.raises(Exception, match="fee schedule"):
        _submit_with_quote(foreign_source_quote, intent=intent)


def test_submission_rejects_a_missing_intent_fee_quote_fingerprint() -> None:
    """None == None on intent and request is NOT fee lineage: the quote
    must be canonically bound end to end or the submission fails closed."""
    from quantlab.execution.orchestration import materialize_bound_request

    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = replace(_intent("authority-nolineage"), fee_quote_fingerprint=None)
    ledger.append(
        OrderIntended("ev-nolineage-intent", intent.created_at, intent)
    )
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    stored = ledger.order(intent.order_id).authority
    request = replace(
        materialize_bound_request(
            intent, stored, fee_quote=_fee_quote(), stored_authority=stored
        ),
        fee_quote_fingerprint=None,
    )
    with pytest.raises(Exception, match="fingerprint"):
        ledger.append(
            OrderSubmitted(
                "ev-nolineage-submitted",
                request.created_at,
                request,
                worst_case_fee_fen=_fee_quote().cap_fen,
                availability_fingerprint=request.availability_fingerprint,
                fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
                fee_quote=_fee_quote(),
            )
        )
    assert ledger.order(intent.order_id).status.value == "validated"


def test_submission_rejects_a_missing_request_fee_quote_fingerprint() -> None:
    """A tampered request without its quote fingerprint cannot pass the
    boundary even when every other lineage field is intact."""
    from quantlab.execution.orchestration import materialize_bound_request

    calendar = _calendar()
    ledger = ExecutionLedger(_account(), calendar=calendar)
    engine = _engine(calendar)
    intent = _intent("authority-norequest")
    ledger.append(
        OrderIntended("ev-norequest-intent", intent.created_at, intent)
    )
    result = _assess(ledger, engine, intent, minute=2)
    ledger.append(result.event)
    stored = ledger.order(intent.order_id).authority
    request = replace(
        materialize_bound_request(
            intent, stored, fee_quote=_fee_quote(), stored_authority=stored
        ),
        fee_quote_fingerprint=None,
    )
    with pytest.raises(Exception, match="fingerprint"):
        ledger.append(
            OrderSubmitted(
                "ev-norequest-submitted",
                request.created_at,
                request,
                worst_case_fee_fen=_fee_quote().cap_fen,
                availability_fingerprint=request.availability_fingerprint,
                fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
                fee_quote=_fee_quote(),
            )
        )


def test_fully_matching_quote_authority_chain_reserves_cash() -> None:
    """Happy path: quote evidence equals the assessed schedule and the
    intent, request, and event all carry the quote's canonical
    fingerprint; the reservation is created exactly."""
    ledger, intent = _submit_with_quote(_fee_quote())
    assert ledger.order(intent.order_id).status.value == "submitted"
    authority = ledger.order(intent.order_id).authority
    quote = _fee_quote()
    assert quote.evidence_id == authority.fee_schedule_evidence_id
    assert quote.source_fingerprint == authority.fee_schedule_source_fingerprint
    reservation = ledger._reservations[intent.order_id]  # noqa: SLF001
    assert reservation.reserved_cash_fen == 100 * 1000 + quote.cap_fen
