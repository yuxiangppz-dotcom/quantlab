"""Part B: canonical availability fingerprints and collision coverage.

The v0.2.1 execution-state fingerprint mixed resource state with order
lifecycle state, accepted caller-supplied values, and derived leg/plan/
quote identities from partial payloads. v0.2.2 requires:

- an availability fingerprint computed from CANONICAL bytes (never a
  caller-declared string), insensitive to pure lifecycle transitions but
  sensitive to every resource axis;
- instruction/leg/plan/fee-quote identities that change whenever any
  economic term or evidence input changes (collision tests).
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from quantlab.execution.ledger import (
    ConstraintsAssessed,
    ExecutionLedger,
    OrderIntended,
)
from quantlab.execution.models import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    ExecutionValidationError,
    OrderIntent,
    PositionLot,
    PriceBasis,
    Side,
)
from quantlab.execution.planning import (
    AvailabilityReservation,
    AvailabilityState,
    ExecutionStateView,
    FeeCapQuote,
    OrderPriceEvidence,
    fingerprint_fee_cap_quote,
    fingerprint_order_price_evidence,
    fingerprint_rebalance_instruction,
)
from quantlab.execution.rules import PITIdentityBook, TradingCalendar

THU = date(2024, 11, 28)
FRI = date(2024, 11, 29)
MON = date(2024, 12, 2)


def _instant(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(9, 30), EXCHANGE_TIMEZONE) + timedelta(
        minutes=minute
    )


def _account(cash: int = 5_000_000) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="acct",
        as_of=_instant(FRI),
        cash_fen=cash,
        lots=(
            PositionLot("lot-1", "600000.SH", 500, THU, MON),
        ),
    )


def _state(**overrides) -> AvailabilityState:
    kwargs = dict(
        account_id="acct",
        as_of=_instant(FRI),
        trade_date=MON,
        settled_cash_fen=5_000_000,
        lots=(
            PositionLot("lot-1", "600000.SH", 500, THU, MON),
        ),
        reservations=(
            AvailabilityReservation(
                order_id="ord-1",
                instrument_id="600000.SH",
                limit_price_fen=1_000,
                fee_cap_fen=30_000,
                fee_used_fen=0,
                reserved_cash_fen=330_000,
                reserved_shares=0,
            ),
        ),
        available_cash_fen=4_670_000,
        available_sellable_shares={},
    )
    kwargs.update(overrides)
    return AvailabilityState(**kwargs)


def _view(state: AvailabilityState) -> ExecutionStateView:
    return ExecutionStateView(
        account=_account(cash=state.settled_cash_fen), state=state
    )


# ---------------------------------------------------------------------------
# availability fingerprint: canonical derivation and collision axes
# ---------------------------------------------------------------------------


def test_availability_fingerprint_is_derived_from_payload() -> None:
    state = _state()
    other = _state()
    assert state.fingerprint == other.fingerprint
    assert state.fingerprint.startswith("sha256:")


def test_availability_fingerprint_moves_on_every_resource_axis() -> None:
    base = _state().fingerprint
    variants = [
        _state(settled_cash_fen=5_000_001).fingerprint,
        _state(trade_date=THU).fingerprint,
        _state(as_of=_instant(FRI, 1)).fingerprint,
        _state(lots=()).fingerprint,
        _state(lots=(
            PositionLot("lot-1", "600000.SH", 501, THU, MON),
        )).fingerprint,
        _state(lots=(
            PositionLot("lot-1", "600000.SH", 500, THU, date(2024, 12, 3)),
        )).fingerprint,
        _state(reservations=()).fingerprint,
        _state(reservations=(
            AvailabilityReservation(
                order_id="ord-1",
                instrument_id="600000.SH",
                limit_price_fen=1_000,
                fee_cap_fen=30_000,
                fee_used_fen=1,
                reserved_cash_fen=330_000,
                reserved_shares=0,
            ),
        )).fingerprint,
        _state(reservations=(
            AvailabilityReservation(
                order_id="ord-1",
                instrument_id="600000.SH",
                limit_price_fen=1_005,
                fee_cap_fen=30_000,
                fee_used_fen=0,
                reserved_cash_fen=330_000,
                reserved_shares=0,
            ),
        )).fingerprint,
        _state(available_cash_fen=4_670_001).fingerprint,
        _state(available_sellable_shares={"600000.SH": 500}).fingerprint,
    ]
    for variant in variants:
        assert variant != base, "a resource axis failed to move the fingerprint"


def test_pure_lifecycle_transition_does_not_move_the_availability_state() -> None:
    """INTENDED -> VALIDATED is a lifecycle transition, not a resource
    change: the same pre-batch availability state must survive it."""
    from quantlab.execution.models import OrderType

    calendar = TradingCalendar(
        sessions=(THU, FRI, MON), coverage_start=THU, coverage_end=MON,
        source_id="synthetic", source_sha256="1" * 64,
    )
    ledger = ExecutionLedger(_account(), calendar=calendar)
    intent = OrderIntent(
        order_id="ord-lifecycle",
        instruction_id="instr",
        instrument_id="600000.SH",
        side=Side.BUY,
        quantity=100,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        intended_trade_date=MON,
        created_at=_instant(FRI, 1),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="synthetic-raw",
    )
    ledger.append(OrderIntended("ev-intent", _instant(FRI, 1), intent))
    before = ledger.availability_fingerprint()
    # appending a SECOND intent is a pure lifecycle event: no cash, lot,
    # or reservation moves, so the availability state must not move
    intent_two = replace(
        intent, order_id="ord-lifecycle-2", created_at=_instant(FRI, 2)
    )
    ledger.append(OrderIntended("ev-intent-2", _instant(FRI, 2), intent_two))
    assert ledger.availability_fingerprint() == before


def test_ledger_recomputes_availability_fingerprint_on_accept() -> None:
    """Even if a caller fabricates a state fingerprint, the ledger
    re-derives the canonical value when accepting an event."""
    from quantlab.execution.constraints import ConstraintDecision
    from quantlab.execution.models import (
        ConstraintDimension,
        ConstraintStatus,
        OrderType,
    )

    calendar = TradingCalendar(
        sessions=(THU, FRI, MON), coverage_start=THU, coverage_end=MON,
        source_id="synthetic", source_sha256="1" * 64,
    )
    ledger = ExecutionLedger(_account(), calendar=calendar)
    fabricated = "sha256:" + "f" * 64
    intent = OrderIntent(
        order_id="ord-fab",
        instruction_id="instr",
        instrument_id="600000.SH",
        side=Side.BUY,
        quantity=100,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        intended_trade_date=MON,
        created_at=_instant(FRI, 1),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="synthetic-raw",
    )
    ledger.append(OrderIntended("ev-fab-intent", _instant(FRI, 1), intent))
    decisions = tuple(
        ConstraintDecision(
            decision_id=f"d-{dimension.value}",
            dimension=dimension,
            status=ConstraintStatus.ALLOWED,
            reason_code="x",
            message="m",
            assessed_at=_instant(FRI, 2),
        )
        for dimension in ConstraintDimension
    )
    with pytest.raises(Exception) as excinfo:
        ledger.append(
            ConstraintsAssessed(
                event_id="ev-fab-assess",
                occurred_at=_instant(FRI, 2),
                order_id="ord-fab",
                decisions=decisions,
                availability_fingerprint=fabricated,
            )
        )
    assert "drifted" in str(excinfo.value)


def test_view_fingerprint_comes_from_its_own_state() -> None:
    view = _view(_state())
    assert view.fingerprint == _state().fingerprint
    assert view.availability_fingerprint == view.fingerprint


# ---------------------------------------------------------------------------
# canonical identity collisions
# ---------------------------------------------------------------------------


def _instruction(**overrides):
    from quantlab.execution.models import (
        InstructionSourceMetadata,
        PositionTarget,
        PriceBasis,
        RebalanceInstruction,
    )

    metadata = overrides.pop(
        "source_metadata",
        InstructionSourceMetadata(
            target_as_of=FRI,
            target_fingerprint="a" * 64,
            planning_input_fingerprint="f" * 64,
            planner_version="test-planner",
            planning_nav_fen=10_000_000,
            minimum_cash_fen=0,
            planning_price_basis=PriceBasis.RAW,
            planning_price_policy="raw_close",
            share_rounding_policy="board_lot_floor_from_zero",
            cash_policy="integer_fen",
            planning_price_source_ids=("raw-close",),
        ),
    )
    kwargs = dict(
        instruction_id="instr-1",
        portfolio_id="port-1",
        signal_as_of=_instant(FRI, 360),
        execution_date=MON,
        targets=(PositionTarget("600000.SH", 100),),
        source_fingerprint="a" * 64,
        source_metadata=metadata,
    )
    kwargs.update(overrides)
    return RebalanceInstruction(**kwargs)


def test_instruction_fingerprint_binds_signal_cutoff_and_metadata() -> None:
    base = fingerprint_rebalance_instruction(_instruction())
    moved_cutoff = fingerprint_rebalance_instruction(
        _instruction(signal_as_of=_instant(FRI, 361))
    )
    moved_metadata = fingerprint_rebalance_instruction(
        _instruction(
            source_metadata=replace(
                _instruction().source_metadata,
                planning_input_fingerprint="0" * 64,
            )
        )
    )
    assert len({base, moved_cutoff, moved_metadata}) == 3


def test_fee_quote_fingerprint_is_a_full_canonical_hash() -> None:
    quote = FeeCapQuote(
        instrument_id="600000.SH",
        account_id="acct",
        trade_date=MON,
        cap_fen=30_000,
        evidence_id="fee-ev-1",
        source_fingerprint="b" * 64,
        synthetic=True,
    )
    fp = fingerprint_fee_cap_quote(quote)
    assert fp != quote.source_fingerprint
    variants = [
        replace(quote, cap_fen=30_001),
        replace(quote, evidence_id="fee-ev-2"),
        replace(quote, source_fingerprint="c" * 64),
        replace(quote, synthetic=False),
        replace(quote, trade_date=FRI),
        replace(quote, account_id="acct-2"),
    ]
    for variant in variants:
        assert fingerprint_fee_cap_quote(variant) != fp


def test_price_evidence_fingerprint_collisions() -> None:
    evidence = OrderPriceEvidence(
        instrument_id="600000.SH",
        price=Decimal("10.00"),
        price_date=FRI,
        available_at=_instant(FRI, 360),
        basis=PriceBasis.RAW,
        source_id="raw-price",
        source_fingerprint="d" * 64,
    )
    fp = fingerprint_order_price_evidence(evidence)
    variants = [
        replace(evidence, price=Decimal("10.01")),
        replace(evidence, price_date=THU),
        replace(evidence, source_id="raw-price-2"),
        replace(evidence, source_fingerprint="e" * 64),
    ]
    for variant in variants:
        assert fingerprint_order_price_evidence(variant) != fp


def test_fee_quote_synthetic_must_be_strict_bool() -> None:
    with pytest.raises(ExecutionValidationError):
        FeeCapQuote(
            instrument_id="600000.SH",
            account_id="acct",
            trade_date=MON,
            cap_fen=30_000,
            evidence_id="fee-ev-1",
            source_fingerprint="b" * 64,
            synthetic="true",  # type: ignore[arg-type]
        )


def test_foreign_price_object_gives_structured_block_not_attribute_error() -> None:
    """A handoff planning price (or any foreign object) must produce a
    structured blocked leg, not an AttributeError."""
    from quantlab.execution.planning import (
        OrderPlanLegStatus,
        build_order_plan,
    )
    from quantlab.execution.rules import default_a_share_rule_book

    calendar = TradingCalendar(
        sessions=(THU, FRI, MON), coverage_start=THU, coverage_end=MON,
        source_id="synthetic", source_sha256="1" * 64,
    )
    instruction = _instruction()
    class ForeignPrice:
        pass

    from quantlab.execution.rules import InstrumentIdentity

    identities = PITIdentityBook((
        InstrumentIdentity(
            instrument_id="600000.SH",
            exchange="SSE",
            board="MAIN",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            source_record_id="fp-identity",
        ),
    ))

    plan = build_order_plan(
        instruction,
        _account(cash=5_000_000),
        created_at=_instant(FRI, 361),
        order_prices={"600000.SH": ForeignPrice()},
        fee_caps={},
        calendar=calendar,
        identities=identities,
        rules=default_a_share_rule_book(),
        execution_state=_view(_state(reservations=(), available_cash_fen=5_000_000)),
    )
    leg = plan.legs[0]
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "order_price_evidence_invalid"
