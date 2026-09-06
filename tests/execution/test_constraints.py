from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from quantlab.execution import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    AShareConstraintEngine,
    ConstraintDimension,
    ConstraintStatus,
    ExecutionLedger,
    FeeScheduleEvidence,
    InstrumentIdentity,
    OrderIntended,
    OrderIntent,
    OrderStatus,
    OrderType,
    PITIdentityBook,
    PositionLot,
    PriceBasis,
    Side,
    SuspensionEvidence,
    SuspensionState,
    TradingCalendar,
    default_a_share_rule_book,
)

FRIDAY = date(2022, 1, 7)
MONDAY = date(2022, 1, 10)
CREATED = datetime(2022, 1, 7, 9, 0, tzinfo=EXCHANGE_TIMEZONE)
ASSESSED = CREATED + timedelta(minutes=1)


def _calendar() -> TradingCalendar:
    return TradingCalendar(
        sessions=(FRIDAY, MONDAY),
        coverage_start=FRIDAY,
        coverage_end=MONDAY,
        source_id="synthetic-calendar",
        source_sha256="c" * 64,
    )


def _identities(board: str = "MAIN", exchange: str = "SSE") -> PITIdentityBook:
    return PITIdentityBook(
        (
            InstrumentIdentity(
                instrument_id="600000.SH",
                exchange=exchange,
                board=board,
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="identity-600000",
            ),
        )
    )


def _engine(board: str = "MAIN", exchange: str = "SSE") -> AShareConstraintEngine:
    return AShareConstraintEngine(
        calendar=_calendar(),
        identities=_identities(board, exchange),
        rules=default_a_share_rule_book(),
    )


def _account(*, lots=()) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="account-1",
        as_of=CREATED,
        cash_fen=10_000_000,
        lots=tuple(lots),
    )


def _intent(
    *,
    side: Side = Side.BUY,
    quantity: int = 100,
    trade_date: date = FRIDAY,
    basis: PriceBasis = PriceBasis.RAW,
) -> OrderIntent:
    return OrderIntent(
        order_id=f"order-{side.value}-{quantity}-{trade_date}",
        instruction_id="rebalance-1",
        instrument_id="600000.SH",
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10.00"),
        intended_trade_date=trade_date,
        created_at=CREATED,
        limit_price_basis=basis,
        limit_price_source_id="raw-daily-close-2022-01-06",
    )


def _open_evidence(trade_date: date = FRIDAY) -> SuspensionEvidence:
    return SuspensionEvidence(
        instrument_id="600000.SH",
        trade_date=trade_date,
        state=SuspensionState.VERIFIED_OPEN,
        observed_at=CREATED,
        source_record_ids=("suspension-coverage-partition",),
        coverage_complete=True,
    )


def _by_dimension(result):
    return {decision.dimension: decision for decision in result.event.decisions}


def test_production_engine_reaches_submission_eligible_with_unknown_fillability() -> None:
    """The REAL AShareConstraintEngine (not hand-built decisions) reaches
    VALIDATED when admissibility/access/fees are verified, even though fill
    probability stays unknown: submission eligibility and eventual
    fillability are different things, and the uncertainty stays on the
    fillability audit row."""
    synthetic_fee = FeeScheduleEvidence(
        schedule_id="synthetic-fee-schedule-test-only",
        effective_from=date(2020, 1, 1),
        effective_to=date(2027, 12, 31),
        broker_commission_exact=True,
        statutory_fees_exact=True,
        source_sha256="f" * 64,
    )
    result = _engine().assess(
        _intent(),
        _account(),
        ASSESSED,
        suspension=_open_evidence(),
        fee_schedule=synthetic_fee,
        daily_bar_available=None,
    )
    decisions = _by_dimension(result)
    assert decisions[ConstraintDimension.ORDER_ADMISSIBILITY].status is (
        ConstraintStatus.ALLOWED
    )
    assert decisions[ConstraintDimension.MARKET_ACCESSIBILITY].status is (
        ConstraintStatus.ALLOWED
    )
    assert decisions[ConstraintDimension.FEE_DETERMINABILITY].status is (
        ConstraintStatus.ALLOWED
    )
    assert decisions[ConstraintDimension.FILLABILITY].status is (
        ConstraintStatus.UNKNOWN
    )
    assert decisions[ConstraintDimension.FILLABILITY].reason_code == (
        "daily_bar_availability_unknown"
    )
    assert result.derived_status is OrderStatus.VALIDATED

    # the production path is reachable end to end: the ledger accepts the
    # engine's assessment and the order becomes submittable
    ledger = ExecutionLedger(_account())
    ledger.append(OrderIntended("event-intent", CREATED, intent := _intent()))
    ledger.append(result.event)
    assert ledger.order(intent.order_id).status is OrderStatus.VALIDATED


def test_daily_bar_never_becomes_fill_evidence() -> None:
    intent = _intent()
    result = _engine().assess(
        intent,
        _account(),
        ASSESSED,
        suspension=_open_evidence(),
        fee_schedule=None,
        daily_bar_available=True,
    )
    decisions = _by_dimension(result)
    assert decisions[ConstraintDimension.ORDER_ADMISSIBILITY].status is (
        ConstraintStatus.ALLOWED
    )
    assert decisions[ConstraintDimension.FILLABILITY].status is ConstraintStatus.UNKNOWN
    assert decisions[ConstraintDimension.FILLABILITY].reason_code == (
        "daily_bar_not_fill_evidence"
    )
    # fee determinability unknown still gates submission eligibility
    assert result.derived_status is OrderStatus.UNKNOWN

    ledger = ExecutionLedger(_account())
    ledger.append(OrderIntended("event-intent", CREATED, intent))
    ledger.append(result.event)
    assert ledger.order(intent.order_id).status is OrderStatus.UNKNOWN


def test_missing_bar_is_not_suspension_and_absent_row_is_unknown() -> None:
    result = _engine().assess(
        _intent(),
        _account(),
        ASSESSED,
        suspension=None,
        fee_schedule=None,
        daily_bar_available=False,
    )
    decisions = _by_dimension(result)
    access = decisions[ConstraintDimension.MARKET_ACCESSIBILITY]
    fillability = decisions[ConstraintDimension.FILLABILITY]
    assert access.status is ConstraintStatus.UNKNOWN
    assert access.reason_code == "suspension_coverage_unknown"
    assert fillability.reason_code == "missing_daily_bar_not_suspension_evidence"
    assert all(
        decision.reason_code != "verified_suspension"
        for decision in result.event.decisions
    )


def test_positive_suspension_record_rejects_market_access() -> None:
    suspended = replace(
        _open_evidence(),
        state=SuspensionState.VERIFIED_SUSPENDED,
        source_record_ids=("suspend-row-1",),
        coverage_complete=False,
    )
    result = _engine().assess(
        _intent(),
        _account(),
        ASSESSED,
        suspension=suspended,
        fee_schedule=None,
        daily_bar_available=True,
    )
    access = _by_dimension(result)[ConstraintDimension.MARKET_ACCESSIBILITY]
    assert access.status is ConstraintStatus.REJECTED
    assert result.derived_status is OrderStatus.REJECTED


def test_adjusted_price_is_rejected_while_raw_price_reaches_other_gaps() -> None:
    adjusted = _engine().assess(
        _intent(basis=PriceBasis.ADJUSTED),
        _account(),
        ASSESSED,
        suspension=_open_evidence(),
        fee_schedule=None,
        daily_bar_available=True,
    )
    raw = _engine().assess(
        _intent(basis=PriceBasis.RAW),
        _account(),
        ASSESSED,
        suspension=_open_evidence(),
        fee_schedule=None,
        daily_bar_available=True,
    )
    assert _by_dimension(adjusted)[
        ConstraintDimension.ORDER_ADMISSIBILITY
    ].reason_code == "adjusted_price_not_executable"
    assert adjusted.derived_status is OrderStatus.REJECTED
    assert _by_dimension(raw)[
        ConstraintDimension.ORDER_ADMISSIBILITY
    ].status is ConstraintStatus.ALLOWED
    assert raw.derived_status is OrderStatus.UNKNOWN


def test_tick_quantity_and_cash_fail_hard_as_order_rejections() -> None:
    engine = _engine()
    bad_tick = replace(_intent(), limit_price=Decimal("10.001"))
    bad_quantity = _intent(quantity=150)
    insufficient = replace(_account(), cash_fen=50_000)
    cases = (
        (bad_tick, _account(), "invalid_price_tick"),
        (bad_quantity, _account(), "invalid_order_quantity"),
        (_intent(), insufficient, "insufficient_cash_before_fees"),
    )
    for intent, account, reason in cases:
        result = engine.assess(
            intent,
            account,
            ASSESSED,
            suspension=_open_evidence(),
            fee_schedule=None,
            daily_bar_available=True,
        )
        decision = _by_dimension(result)[ConstraintDimension.ORDER_ADMISSIBILITY]
        assert decision.status is ConstraintStatus.REJECTED
        assert decision.reason_code == reason


def test_t_plus_one_sellability_uses_explicit_next_session() -> None:
    lot = PositionLot(
        lot_id="fill-friday",
        instrument_id="600000.SH",
        quantity=100,
        acquired_trade_date=FRIDAY,
        sellable_from=MONDAY,
    )
    account = _account(lots=(lot,))
    friday = _engine().assess(
        _intent(side=Side.SELL),
        account,
        ASSESSED,
        suspension=_open_evidence(),
        fee_schedule=None,
        daily_bar_available=True,
    )
    monday = _engine().assess(
        _intent(side=Side.SELL, trade_date=MONDAY),
        account,
        ASSESSED,
        suspension=_open_evidence(MONDAY),
        fee_schedule=None,
        daily_bar_available=True,
    )
    assert _by_dimension(friday)[
        ConstraintDimension.POSITION_SELLABILITY
    ].status is ConstraintStatus.REJECTED
    assert _by_dimension(monday)[
        ConstraintDimension.POSITION_SELLABILITY
    ].status is ConstraintStatus.ALLOWED


def test_historical_rule_gap_stays_unknown_instead_of_using_current_rule() -> None:
    identity = InstrumentIdentity(
        instrument_id="600000.SH",
        exchange="SZSE",
        board="MAIN",
        effective_from=date(2020, 1, 1),
        effective_to=None,
        source_record_id="synthetic-szse-identity",
    )
    calendar = TradingCalendar(
        sessions=(date(2024, 1, 5),),
        coverage_start=date(2024, 1, 5),
        coverage_end=date(2024, 1, 5),
        source_id="synthetic-calendar",
        source_sha256="d" * 64,
    )
    engine = AShareConstraintEngine(
        calendar=calendar,
        identities=PITIdentityBook((identity,)),
        rules=default_a_share_rule_book(),
    )
    intent = replace(
        _intent(),
        intended_trade_date=date(2024, 1, 5),
        order_id="order-rule-gap",
    )
    result = engine.assess(
        intent,
        _account(),
        ASSESSED,
        suspension=_open_evidence(date(2024, 1, 5)),
        fee_schedule=None,
        daily_bar_available=True,
    )
    decision = _by_dimension(result)[ConstraintDimension.ORDER_ADMISSIBILITY]
    assert decision.status is ConstraintStatus.UNKNOWN
    assert decision.reason_code == "pit_rule_missing"


def test_st_status_is_disclosed_context_but_not_an_exit_or_rejection_rule() -> None:
    kwargs = {
        "suspension": _open_evidence(),
        "fee_schedule": None,
        "daily_bar_available": True,
    }
    without_st = _engine().assess(_intent(), _account(), ASSESSED, **kwargs)
    with_st = _engine().assess(
        _intent(),
        _account(),
        ASSESSED,
        st_status="ST",
        **kwargs,
    )
    assert tuple(
        (item.dimension, item.status, item.reason_code)
        for item in without_st.event.decisions
    ) == tuple(
        (item.dimension, item.status, item.reason_code)
        for item in with_st.event.decisions
    )
