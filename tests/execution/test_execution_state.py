"""Reservation-aware execution-state fingerprint and TOCTOU regression tests.

These tests prove the v0.2 defect: the settled account fingerprint cannot
see reservations, so an assessment taken before another order's reservation
silently stays fresh. v0.2.1 binds every assessment and submission to a
unique, unreconstructable-from-outside execution-state fingerprint and the
planner to available (unreserved) cash and sellable shares.
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
    LedgerTransitionError,
    OrderIntended,
    OrderSubmitted,
    account_state_fingerprint,
)
from quantlab.execution.models import (
    AccountSnapshot,
    OrderIntent,
    OrderType,
    PositionLot,
    PriceBasis,
    Side,
    TimeInForce,
)
from quantlab.execution.planning import (
    FeeCapQuote,
    OrderPriceEvidence,
    build_order_plan,
    fingerprint_fee_cap_quote,
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
        source_id="test-state-calendar",
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
                source_record_id="state-identity",
            ),
        )
    )


def _account(cash_fen: int = 1_000_000, lots: tuple[PositionLot, ...] = ()) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="state-account",
        as_of=_instant(FRI, 0),
        cash_fen=cash_fen,
        lots=lots,
    )


def _open(day: date) -> SuspensionEvidence:
    return SuspensionEvidence(
        instrument_id="600000.SH",
        trade_date=day,
        state=SuspensionState.VERIFIED_OPEN,
        observed_at=_instant(day),
        source_record_ids=("state-suspension",),
        coverage_complete=True,
    )


def _fee_schedule() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        schedule_id="synthetic-state-fee-schedule",
        effective_from=date(2020, 1, 1),
        effective_to=date(2027, 12, 31),
        broker_commission_exact=True,
        statutory_fees_exact=True,
        source_sha256="b" * 64,
    )


def _intent(order_id: str, quantity: int = 300, minute: int = 1) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instruction_id="state-instruction",
        instrument_id="600000.SH",
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=PRICE,
        intended_trade_date=FRI,
        created_at=_instant(FRI, minute),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="state-price-source",
        time_in_force=TimeInForce.DAY,
        fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
    )


def _fee_quote():
    from quantlab.execution.planning import FeeCapQuote

    return FeeCapQuote(
        instrument_id="600000.SH",
        account_id="state-account",
        trade_date=FRI,
        cap_fen=FEE_CAP,
        evidence_id="state-fee-quote",
        source_fingerprint="f" * 64,
        synthetic=True,
    )


def _engine() -> AShareConstraintEngine:
    calendar = _calendar()
    return AShareConstraintEngine(
        calendar=calendar, identities=_identities(), rules=default_a_share_rule_book()
    )


def _submit(
    ledger: ExecutionLedger,
    engine: AShareConstraintEngine,
    order_id: str,
    quantity: int = 300,
    minute: int = 1,
) -> None:
    """Intend, assess, and submit one buy through the real engine."""
    intent = _intent(order_id, quantity=quantity, minute=minute)
    ledger.append(
        OrderIntended(f"{order_id}-intended", _instant(FRI, minute), intent)
    )
    assess_at = _instant(FRI, minute + 2)
    result = engine.assess(
        intent,
        ledger.snapshot(assess_at),
        assess_at,
        suspension=_open(FRI),
        fee_schedule=_fee_schedule(),
        daily_bar_available=None,
        availability_fingerprint=ledger.availability_fingerprint(),
    )
    ledger.append(result.event)
    from quantlab.execution.orchestration import materialize_bound_request

    authority = ledger.order(order_id).authority
    fee_quote = _fee_quote()
    request = materialize_bound_request(
        intent, authority, fee_quote=fee_quote, stored_authority=authority
    )
    ledger.append(
        OrderSubmitted(
            f"{order_id}-submitted",
            request.created_at,
            request,
            worst_case_fee_fen=fee_quote.cap_fen,
            availability_fingerprint=request.availability_fingerprint,
            fee_quote_fingerprint=fingerprint_fee_cap_quote(fee_quote),
            fee_quote=fee_quote,
        )
    )


def test_settled_fingerprint_is_blind_to_reservations() -> None:
    """The v0.2 defect: reservations do not move the settled fingerprint."""
    ledger = ExecutionLedger(_account(cash_fen=500_000), calendar=_calendar())
    before = account_state_fingerprint(ledger.snapshot(_instant(FRI, 9)))
    _submit(ledger, _engine(), "state-buy-a")
    after = account_state_fingerprint(ledger.snapshot(_instant(FRI, 9)))
    assert before == after, "settled fingerprint unexpectedly moved"
    assert ledger.reserved_cash_fen() == 330_000


def test_availability_fingerprint_moves_on_reservation() -> None:
    ledger = ExecutionLedger(_account(cash_fen=500_000), calendar=_calendar())
    before = ledger.availability_fingerprint()
    _submit(ledger, _engine(), "state-buy-a")
    after = ledger.availability_fingerprint()
    assert before != after


def test_assessment_before_foreign_reservation_is_stale() -> None:
    """An assessment bound pre-reservation must be rejected post-reservation."""
    ledger = ExecutionLedger(_account(cash_fen=500_000), calendar=_calendar())
    engine = _engine()
    ledger.append(
        OrderIntended("state-buy-a-intent", _instant(FRI, 1), _intent("state-buy-a"))
    )
    assess_at = _instant(FRI, 20)
    result = engine.assess(
        _intent("state-buy-a"),
        ledger.snapshot(assess_at),
        assess_at,
        suspension=_open(FRI),
        fee_schedule=_fee_schedule(),
        daily_bar_available=None,
        availability_fingerprint=ledger.availability_fingerprint(),
    )
    # a different order reserves cash before this assessment is appended
    _submit(ledger, engine, "state-other", minute=6)
    with pytest.raises(LedgerTransitionError, match="availability state"):
        ledger.append(result.event)


def test_submission_rechecks_fingerprint_before_reservation() -> None:
    ledger = ExecutionLedger(_account(cash_fen=500_000), calendar=_calendar())
    engine = _engine()
    intent = _intent("state-buy-late")
    ledger.append(
        OrderIntended("state-buy-late-intent", _instant(FRI, 1), intent)
    )
    assess_at = _instant(FRI, 3)
    result = engine.assess(
        intent,
        ledger.snapshot(assess_at),
        assess_at,
        suspension=_open(FRI),
        fee_schedule=_fee_schedule(),
        daily_bar_available=None,
        availability_fingerprint=ledger.availability_fingerprint(),
    )
    ledger.append(result.event)
    stale_state = ledger.availability_fingerprint()
    _submit(ledger, engine, "state-buy-first", minute=6)
    from quantlab.execution.orchestration import materialize_bound_request

    authority = ledger.order("state-buy-late").authority
    request = materialize_bound_request(
        intent,
        authority,
        fee_quote=_fee_quote(),
        stored_authority=authority,
    )
    from dataclasses import replace

    request = replace(request, created_at=_instant(FRI, 10))
    with pytest.raises(LedgerTransitionError, match="availability state"):
        ledger.append(
            OrderSubmitted(
                "state-buy-late-submitted",
                request.created_at,
                request,
                worst_case_fee_fen=FEE_CAP,
                availability_fingerprint=stale_state,
                fee_quote_fingerprint=fingerprint_fee_cap_quote(_fee_quote()),
                fee_quote=_fee_quote(),
            )
        )


def test_planner_funds_buys_from_available_cash_only() -> None:
    """Settled cash hides a reservation; planning must use available cash."""
    calendar = _calendar()
    identities = _identities()
    instruction_fingerprint = "c" * 64
    from quantlab.execution.models import (
        InstructionSourceMetadata,
        PositionTarget,
        RebalanceInstruction,
    )

    def _instruction() -> RebalanceInstruction:
        return RebalanceInstruction(
            instruction_id="state-instruction",
            portfolio_id="state-portfolio",
            signal_as_of=_instant(FRI, 375),
            execution_date=MON,
            targets=(PositionTarget("600000.SH", 400),),
            source_fingerprint=instruction_fingerprint,
            source_metadata=InstructionSourceMetadata(
                target_as_of=FRI,
                target_fingerprint=instruction_fingerprint,
                planning_input_fingerprint="d" * 64,
                planner_version="target_weight_to_share_target_v0_1",
                planning_nav_fen=2_000_000,
                minimum_cash_fen=0,
                planning_price_basis=PriceBasis.RAW,
                planning_price_policy="test",
                share_rounding_policy="test",
                cash_policy="test",
                planning_price_source_ids=("state-price-source",),
            ),
        )

    account = _account(cash_fen=1_000_000)
    prices = {
        "600000.SH": OrderPriceEvidence(
            instrument_id="600000.SH",
            price=PRICE,
            price_date=FRI,
            available_at=_instant(FRI, 0),
            basis=PriceBasis.RAW,
            source_id="state-price-source",
            source_fingerprint="e" * 64,
        )
    }
    fee_caps = {
        "600000.SH": FeeCapQuote(
            instrument_id="600000.SH",
            account_id="state-account",
            trade_date=MON,
            cap_fen=FEE_CAP,
            evidence_id="state-fee-quote",
            source_fingerprint="f" * 64,
            synthetic=True,
        )
    }
    # settled cash 1_000_000 >= worst case 430_000, but only 100_000 is
    # unreserved; planning on settled cash would wrongly clear the plan
    from quantlab.execution.planning import (
        AvailabilityState,
        ExecutionStateView,
    )

    view = ExecutionStateView(
        account=account,
        state=AvailabilityState(
            account_id=account.account_id,
            as_of=account.as_of,
            trade_date=MON,
            settled_cash_fen=account.cash_fen,
            lots=account.lots,
            reservations=(),
            available_cash_fen=100_000,
            available_sellable_shares={},
        ),
    )
    blocked = build_order_plan(
        _instruction(),
        account,
        created_at=_instant(MON, 0),
        order_prices=prices,
        fee_caps=fee_caps,
        calendar=calendar,
        identities=identities,
        rules=default_a_share_rule_book(),
        execution_state=view,
    )
    assert blocked.status.value == "blocked"
    assert "aggregate_buy_worst_case_exceeds_available_cash" in (
        blocked.reason_codes
    )
    assert blocked.availability_fingerprint == view.fingerprint


def test_planner_uses_available_sellable_for_exits() -> None:
    """Reserved sellable shares must not fund a planned exit."""
    from quantlab.execution.models import (
        InstructionSourceMetadata,
        PositionTarget,
        RebalanceInstruction,
    )
    from quantlab.execution.planning import (
        AvailabilityState,
        ExecutionStateView,
    )

    calendar = _calendar()
    identities = _identities()
    lot = PositionLot(
        lot_id="state-lot",
        instrument_id="600000.SH",
        quantity=300,
        acquired_trade_date=THU,
        sellable_from=FRI,
    )
    account = _account(cash_fen=1_000_000, lots=(lot,))
    fingerprint = "9" * 64
    instruction = RebalanceInstruction(
        instruction_id="state-exit-instruction",
        portfolio_id="state-portfolio",
        signal_as_of=_instant(FRI, 375),
        execution_date=MON,
        targets=(PositionTarget("600000.SH", 0),),
        source_fingerprint=fingerprint,
        source_metadata=InstructionSourceMetadata(
            target_as_of=FRI,
            target_fingerprint=fingerprint,
            planning_input_fingerprint="8" * 64,
            planner_version="target_weight_to_share_target_v0_1",
            planning_nav_fen=2_000_000,
            minimum_cash_fen=0,
            planning_price_basis=PriceBasis.RAW,
            planning_price_policy="test",
            share_rounding_policy="test",
            cash_policy="test",
            planning_price_source_ids=("state-price-source",),
        ),
    )
    prices = {
        "600000.SH": OrderPriceEvidence(
            instrument_id="600000.SH",
            price=PRICE,
            price_date=FRI,
            available_at=_instant(FRI, 0),
            basis=PriceBasis.RAW,
            source_id="state-price-source",
            source_fingerprint="e" * 64,
        )
    }
    kwargs = dict(
        created_at=_instant(MON, 0),
        order_prices=prices,
        calendar=calendar,
        identities=identities,
        rules=default_a_share_rule_book(),
    )
    settled_only = build_order_plan(instruction, account, **kwargs)
    assert settled_only.status.value == "submit_ready"
    reserved_view = ExecutionStateView(
        account=account,
        state=AvailabilityState(
            account_id=account.account_id,
            as_of=account.as_of,
            trade_date=MON,
            settled_cash_fen=account.cash_fen,
            lots=account.lots,
            reservations=(),
            available_cash_fen=1_000_000,
            available_sellable_shares={"600000.SH": 0},
        ),
    )
    blocked = build_order_plan(
        instruction, account, execution_state=reserved_view, **kwargs
    )
    sell_legs = [
        leg
        for leg in blocked.legs
        if leg.instrument_id == "600000.SH" and leg.side is Side.SELL
    ]
    assert sell_legs, blocked.legs
    assert sell_legs[0].status.value == "blocked"
    assert (
        sell_legs[0].reason_code == "insufficient_sellable_shares_t_plus_one"
    )
