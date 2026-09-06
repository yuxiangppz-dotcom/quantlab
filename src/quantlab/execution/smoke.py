"""Synthetic, non-trading smoke suite for the executable order path.

Every scenario exercises the REAL planning, constraint-engine, and ledger
code on synthetic data. Nothing calls a provider, nothing writes canonical
storage, nothing is submitted to any broker, and no historical fill is
claimed. The returned evidence feeds the execution-readiness audit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal

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
    LedgerTransitionError,
    OrderCanceled,
    OrderIntended,
    OrderSubmitted,
    account_state_fingerprint,
)
from quantlab.execution.models import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    OrderIntent,
    OrderRequest,
    OrderType,
    PositionLot,
    PositionTarget,
    PriceBasis,
    Side,
    TimeInForce,
)
from quantlab.execution.planning import (
    FeeCapQuote,
    OrderPriceEvidence,
    build_order_plan,
)
from quantlab.execution.rules import (
    InstrumentIdentity,
    PITIdentityBook,
    TradingCalendar,
    default_a_share_rule_book,
)

SMOKE_INSTRUMENT = "600000.SH"
SMOKE_THU = date(2024, 11, 28)
SMOKE_FRI = date(2024, 11, 29)
SMOKE_MON = date(2024, 12, 2)
SMOKE_TUE = date(2024, 12, 3)
SMOKE_FEE_CAP_FEN = 30_000
SMOKE_PRICE = Decimal("10.00")


def _instant(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(9, 30), EXCHANGE_TIMEZONE) + timedelta(
        minutes=minute
    )


def _minutes_since_open(moment: datetime) -> int:
    open_dt = datetime.combine(moment.date(), time(9, 30), EXCHANGE_TIMEZONE)
    return int((moment - open_dt).total_seconds() // 60)


def _calendar() -> TradingCalendar:
    return TradingCalendar(
        sessions=(SMOKE_THU, SMOKE_FRI, SMOKE_MON, SMOKE_TUE),
        coverage_start=SMOKE_THU,
        coverage_end=SMOKE_TUE,
        source_id="synthetic-smoke-calendar",
        source_sha256="c" * 64,
    )


def _identities() -> PITIdentityBook:
    return PITIdentityBook(
        (
            InstrumentIdentity(
                instrument_id=SMOKE_INSTRUMENT,
                exchange="SSE",
                board="MAIN",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="smoke-identity",
            ),
            InstrumentIdentity(
                instrument_id="600001.SH",
                exchange="SSE",
                board="MAIN",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="smoke-identity-600001",
            ),
        )
    )


def _open(trade_date: date) -> SuspensionEvidence:
    return SuspensionEvidence(
        instrument_id=SMOKE_INSTRUMENT,
        trade_date=trade_date,
        state=SuspensionState.VERIFIED_OPEN,
        observed_at=_instant(trade_date),
        source_record_ids=("smoke-suspension-coverage",),
        coverage_complete=True,
    )


def _smoke_fee() -> FeeScheduleEvidence:
    return FeeScheduleEvidence(
        schedule_id="synthetic-fee-schedule-test-only",
        effective_from=date(2020, 1, 1),
        effective_to=date(2027, 12, 31),
        broker_commission_exact=True,
        statutory_fees_exact=True,
        source_sha256="f" * 64,
    )


def _account(as_of: datetime | None = None, cash_fen: int = 1_000_000_000) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="smoke-account",
        as_of=as_of or _instant(SMOKE_FRI, 0),
        cash_fen=cash_fen,
        lots=(),
    )


def _intent(
    order_id: str,
    *,
    side: Side = Side.BUY,
    quantity: int = 300,
    trade_date: date = SMOKE_FRI,
    minute: int = 1,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        instruction_id="smoke-instruction",
        instrument_id=SMOKE_INSTRUMENT,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        limit_price=SMOKE_PRICE,
        intended_trade_date=trade_date,
        created_at=_instant(trade_date, minute),
        limit_price_basis=PriceBasis.RAW,
        limit_price_source_id="synthetic-raw-price",
        time_in_force=TimeInForce.DAY,
    )


def _request(
    ledger: ExecutionLedger, intent: OrderIntent, minute: int = 3
) -> OrderRequest:
    return OrderRequest(
        request_id=f"smoke-request-{intent.order_id}",
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


def _assess_and_submit(
    ledger: ExecutionLedger,
    engine: AShareConstraintEngine,
    intent: OrderIntent,
    *,
    fee_cap_fen: int = 0,
    fee_schedule=None,
    suspension: SuspensionEvidence,
) -> None:
    base = _minutes_since_open(intent.created_at)
    assess_at = _instant(intent.intended_trade_date, base + 2)
    result = engine.assess(
        intent,
        ledger.snapshot(assess_at),
        assess_at,
        suspension=suspension,
        fee_schedule=fee_schedule,
        daily_bar_available=None,
    )
    ledger.append(result.event)
    request = _request(ledger, intent, base + 3)
    ledger.append(
        OrderSubmitted(
            f"smoke-event-{intent.order_id}-submitted",
            request.created_at,
            request,
            worst_case_fee_fen=fee_cap_fen,
        )
    )


def _fill(
    order_id: str,
    fill_id: str,
    occurred_at: datetime,
    *,
    quantity: int = 100,
    trade_date: date = SMOKE_FRI,
    sellable_from: date | None = SMOKE_MON,
) -> FillRecorded:
    return FillRecorded(
        event_id=f"smoke-event-{fill_id}",
        fill_id=fill_id,
        occurred_at=occurred_at,
        order_id=order_id,
        trade_date=trade_date,
        quantity=quantity,
        price=SMOKE_PRICE,
        gross_notional_fen=quantity * 1_000,
        fee_fen=500,
        buy_lot_sellable_from=sellable_from,
    )


def _order_price_evidence() -> OrderPriceEvidence:
    return OrderPriceEvidence(
        instrument_id=SMOKE_INSTRUMENT,
        price=SMOKE_PRICE,
        price_date=SMOKE_FRI,
        available_at=_instant(SMOKE_FRI, 0),
        basis=PriceBasis.RAW,
        source_id="synthetic-raw-order-price",
        source_fingerprint="1" * 64,
    )


def _fee_cap() -> FeeCapQuote:
    return FeeCapQuote(
        instrument_id=SMOKE_INSTRUMENT,
        account_id="smoke-account",
        trade_date=SMOKE_MON,
        cap_fen=SMOKE_FEE_CAP_FEN,
        evidence_id="synthetic-fee-cap-test-only",
        source_fingerprint="4" * 64,
        synthetic=True,
    )


def run_order_path_smoke() -> dict:
    """Execute every smoke scenario and return the evidence dictionary."""
    evidence: dict[str, object] = {
        "synthetic": True,
        "non_trading": True,
        "provider_called": False,
        "canonical_data_written": False,
        "order_submission_attempted": False,
        "fill_claimed": False,
    }
    calendar = _calendar()
    identities = _identities()
    rules = default_a_share_rule_book()
    engine = AShareConstraintEngine(
        calendar=calendar, identities=identities, rules=rules
    )
    # enough settled cash for exactly ONE worst-case buy reservation
    # (300 shares x 10.00 limit = 300_000 fen gross + 30_000 fen fee cap),
    # so a second concurrent buy must fail its atomic reservation
    account = _account(cash_fen=500_000)

    # 1. production submission path: fillability unknown never gates
    intent_ok = _intent("smoke-order-ok")
    result = engine.assess(
        intent_ok, account, _instant(SMOKE_FRI, 2),
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
        daily_bar_available=None,
    )
    evidence["production_submission_path_reachable"] = (
        result.derived_status.value == "validated"
    )
    gate_matrix = {"fillability_unknown": result.derived_status.value}
    gated = engine.assess(
        intent_ok, account, _instant(SMOKE_FRI, 2),
        suspension=replace(
            _open(SMOKE_FRI), state=SuspensionState.UNKNOWN,
            coverage_complete=False, source_record_ids=(),
        ),
        fee_schedule=_smoke_fee(), daily_bar_available=None,
    )
    gate_matrix["market_accessibility_unknown"] = gated.derived_status.value
    gated = engine.assess(
        intent_ok, account, _instant(SMOKE_FRI, 2),
        suspension=_open(SMOKE_FRI), fee_schedule=None,
        daily_bar_available=None,
    )
    gate_matrix["fee_determinability_unknown"] = gated.derived_status.value
    gated = engine.assess(
        replace(intent_ok, limit_price_basis=PriceBasis.UNKNOWN), account,
        _instant(SMOKE_FRI, 2),
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
        daily_bar_available=None,
    )
    gate_matrix["order_admissibility_unknown"] = gated.derived_status.value
    evidence["submission_gate_matrix"] = gate_matrix
    evidence["gating_dimensions_fail_closed"] = all(
        value == "unknown"
        for key, value in gate_matrix.items()
        if key != "fillability_unknown"
    )

    # 2. account-aware planning
    plan_account = _account(
        as_of=_instant(SMOKE_FRI, 0), cash_fen=10_000_000_000
    )
    plan_account = replace(
        plan_account,
        lots=(
            PositionLot(
                lot_id="smoke-lot-600001",
                instrument_id="600001.SH",
                quantity=200,
                acquired_trade_date=SMOKE_THU,
                sellable_from=SMOKE_FRI,
            ),
        ),
    )
    prices = {
        "600000.SH": _order_price_evidence(),
        "600001.SH": _order_price_evidence(),
    }
    fee_caps = {"600000.SH": _fee_cap()}

    def _plan(targets):
        from quantlab.execution import InstructionSourceMetadata, RebalanceInstruction

        fingerprint = "2" * 64
        instruction = RebalanceInstruction(
            instruction_id="smoke-instruction",
            portfolio_id="smoke-portfolio",
            signal_as_of=_instant(SMOKE_FRI, 375),
            execution_date=SMOKE_MON,
            targets=tuple(targets),
            source_fingerprint=fingerprint,
            source_metadata=InstructionSourceMetadata(
                target_as_of=SMOKE_FRI,
                target_fingerprint=fingerprint,
                planning_input_fingerprint="3" * 64,
                planner_version="target_weight_to_share_target_v0_1",
                planning_nav_fen=10_000_000_000,
                minimum_cash_fen=0,
                planning_price_basis=PriceBasis.RAW,
                planning_price_policy="synthetic-smoke",
                share_rounding_policy="synthetic-smoke",
                cash_policy="synthetic-smoke",
                planning_price_source_ids=("synthetic-raw-price-source",),
            ),
        )
        return build_order_plan(
            instruction, plan_account, created_at=_instant(SMOKE_MON, 0),
            order_prices=prices, fee_caps=fee_caps,
            calendar=calendar, identities=identities, rules=rules,
        )

    plan_one = _plan((PositionTarget(SMOKE_INSTRUMENT, 400),))
    plan_two = _plan((PositionTarget(SMOKE_INSTRUMENT, 400),))
    leg = plan_one.legs[0]
    evidence["order_plan_deterministic"] = plan_one.plan_id == plan_two.plan_id
    evidence["buy_limit_from_order_price_evidence"] = (
        leg.limit_price_source_fingerprint == "1" * 64
        and leg.limit_price_available_at == _instant(SMOKE_FRI, 0)
    )
    evidence["buys_funded_from_available_cash_only"] = (
        plan_one.worst_case_cash_fen_required
        == 400 * 1_000 + SMOKE_FEE_CAP_FEN
    )
    evidence["account_aware_order_planning"] = (
        evidence["order_plan_deterministic"]
        and evidence["buy_limit_from_order_price_evidence"]
        and evidence["buys_funded_from_available_cash_only"]
    )

    omitted = _plan((PositionTarget(SMOKE_INSTRUMENT, 400),))
    evidence["omitted_held_name_exits"] = any(
        item.side is Side.SELL and item.delta_shares == -200
        and item.status.value == "orderable"
        for item in omitted.legs
    )

    odd_plan = _plan((PositionTarget(SMOKE_INSTRUMENT, 450),))
    evidence["non_conforming_delta_blocks"] = (
        odd_plan.legs[0].status.value == "blocked"
        and odd_plan.legs[0].reason_code == "buy_delta_not_lot_conforming"
        and odd_plan.legs[0].target_shares == 450
    )

    # 3. atomic cash reservation: contention, partial fill, cancel release
    cash_ledger = ExecutionLedger(account, calendar=calendar)
    intent_a = _intent("smoke-buy-a")
    cash_ledger.append(
        OrderIntended("smoke-event-buy-a-intent", _instant(SMOKE_FRI, 1), intent_a)
    )
    _assess_and_submit(
        cash_ledger, engine, intent_a, fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    reserved_after_first = cash_ledger.reserved_cash_fen()
    try:
        intent_b = _intent("smoke-buy-b", minute=11)
        cash_ledger.append(
            OrderIntended(
                "smoke-event-buy-b-intent", _instant(SMOKE_FRI, 11), intent_b
            )
        )
        _assess_and_submit(
            cash_ledger, engine, intent_b, fee_cap_fen=SMOKE_FEE_CAP_FEN,
            suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
        )
        evidence["aggregate_cash_contention_blocked"] = False
    except LedgerAccountingError:
        evidence["aggregate_cash_contention_blocked"] = True
    evidence["atomic_cash_reservation"] = (
        evidence["aggregate_cash_contention_blocked"]
        and cash_ledger.reserved_cash_fen() == reserved_after_first
    )
    cash_ledger.append(
        _fill("smoke-buy-a", "smoke-partial-fill", _instant(SMOKE_FRI, 30), quantity=100)
    )
    after_partial = cash_ledger.availability(SMOKE_INSTRUMENT, SMOKE_FRI)
    evidence["partial_fill_drawdown"] = (
        after_partial.reserved_cash_fen == reserved_after_first - 100_500
    )
    cash_ledger.append(
        OrderCanceled(
            "smoke-event-cancel", _instant(SMOKE_FRI, 40), "smoke-buy-a",
            "smoke cancel releases the remaining reservation",
        )
    )
    evidence["cancel_release"] = (
        cash_ledger.availability(SMOKE_INSTRUMENT, SMOKE_FRI).reserved_cash_fen == 0
    )

    # 4. atomic share reservation: same-lot contention
    share_lot = PositionLot(
        lot_id="smoke-lot-sell",
        instrument_id=SMOKE_INSTRUMENT,
        quantity=300,
        acquired_trade_date=SMOKE_THU,
        sellable_from=SMOKE_FRI,
    )
    share_ledger = ExecutionLedger(replace(account, lots=(share_lot,)), calendar=calendar)
    intent_sell_a = _intent("smoke-sell-a", side=Side.SELL, quantity=300)
    share_ledger.append(
        OrderIntended("smoke-event-sell-a-intent", _instant(SMOKE_FRI, 1), intent_sell_a)
    )
    _assess_and_submit(
        share_ledger, engine, intent_sell_a,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    reserved_shares = share_ledger.reserved_sellable_shares(SMOKE_INSTRUMENT)
    try:
        intent_sell_b = _intent(
            "smoke-sell-b", side=Side.SELL, quantity=100, minute=11
        )
        share_ledger.append(
            OrderIntended(
                "smoke-event-sell-b-intent", _instant(SMOKE_FRI, 11), intent_sell_b
            )
        )
        _assess_and_submit(
            share_ledger, engine, intent_sell_b,
            suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
        )
        evidence["share_contention_blocked"] = False
    except LedgerAccountingError:
        evidence["share_contention_blocked"] = True
    evidence["atomic_share_reservation"] = (
        evidence["share_contention_blocked"]
        and share_ledger.reserved_sellable_shares(SMOKE_INSTRUMENT) == reserved_shares
    )

    # 5. DAY trade-date binding
    day_ledger = ExecutionLedger(account, calendar=calendar)
    intent_day = _intent("smoke-buy-day")
    day_ledger.append(
        OrderIntended("smoke-event-day-intent", _instant(SMOKE_FRI, 1), intent_day)
    )
    _assess_and_submit(
        day_ledger, engine, intent_day, fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    try:
        day_ledger.append(
            _fill(
                "smoke-buy-day", "smoke-wrong-date", _instant(SMOKE_MON, 30),
                quantity=100, trade_date=SMOKE_MON, sellable_from=SMOKE_TUE,
            )
        )
        evidence["wrong_trade_date_rejected"] = False
    except LedgerAccountingError:
        evidence["wrong_trade_date_rejected"] = True

    # 6. calendar-derived T+1: weekend exact, holiday exact, coverage closed
    weekend_ledger = ExecutionLedger(account, calendar=calendar)
    intent_weekend = _intent("smoke-buy-weekend")
    weekend_ledger.append(
        OrderIntended(
            "smoke-event-weekend-intent", _instant(SMOKE_FRI, 1), intent_weekend
        )
    )
    _assess_and_submit(
        weekend_ledger, engine, intent_weekend, fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    weekend_ledger.append(
        _fill(
            "smoke-buy-weekend", "smoke-weekend-fill", _instant(SMOKE_MON, 8),
            quantity=100,
            sellable_from=calendar.next_session(SMOKE_FRI),
        )
    )
    evidence["weekend_t_plus_one_exact"] = (
        calendar.next_session(SMOKE_FRI) == SMOKE_MON
        and weekend_ledger.sellable_quantity(SMOKE_INSTRUMENT, SMOKE_FRI) == 0
        and weekend_ledger.sellable_quantity(SMOKE_INSTRUMENT, SMOKE_MON) == 100
    )

    holiday_calendar = TradingCalendar(
        sessions=(SMOKE_THU, SMOKE_MON, SMOKE_TUE),
        coverage_start=SMOKE_THU,
        coverage_end=SMOKE_TUE,
        source_id="synthetic-holiday-calendar",
        source_sha256="d" * 64,
    )
    holiday_ledger = ExecutionLedger(
        _account(as_of=_instant(SMOKE_THU, 0)), calendar=holiday_calendar
    )
    intent_holiday = _intent("smoke-buy-holiday", trade_date=SMOKE_THU)
    holiday_ledger.append(
        OrderIntended(
            "smoke-event-holiday-intent", _instant(SMOKE_THU, 1), intent_holiday
        )
    )
    _assess_and_submit(
        holiday_ledger, engine, intent_holiday, fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_THU), fee_schedule=_smoke_fee(),
    )
    holiday_ledger.append(
        _fill(
            "smoke-buy-holiday", "smoke-holiday-fill", _instant(SMOKE_THU, 30),
            quantity=100, trade_date=SMOKE_THU,
            sellable_from=holiday_calendar.next_session(SMOKE_THU),
        )
    )
    evidence["holiday_t_plus_one_exact"] = (
        holiday_calendar.next_session(SMOKE_THU) == SMOKE_MON
        and holiday_ledger.sellable_quantity(SMOKE_INSTRUMENT, SMOKE_FRI) == 0
        and holiday_ledger.sellable_quantity(SMOKE_INSTRUMENT, SMOKE_MON) == 100
    )

    truncated_calendar = TradingCalendar(
        sessions=(SMOKE_FRI,),
        coverage_start=SMOKE_FRI,
        coverage_end=SMOKE_FRI,
        source_id="synthetic-truncated-calendar",
        source_sha256="e" * 64,
    )
    truncated_ledger = ExecutionLedger(account, calendar=truncated_calendar)
    intent_truncated = _intent("smoke-buy-truncated")
    truncated_ledger.append(
        OrderIntended(
            "smoke-event-truncated-intent", _instant(SMOKE_FRI, 1), intent_truncated
        )
    )
    _assess_and_submit(
        truncated_ledger, engine, intent_truncated,
        fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    try:
        truncated_ledger.append(
            _fill(
                "smoke-buy-truncated", "smoke-truncated-fill",
                _instant(SMOKE_FRI, 30), quantity=100,
                sellable_from=SMOKE_MON,
            )
        )
        evidence["missing_next_session_fail_closed"] = False
    except LedgerAccountingError:
        evidence["missing_next_session_fail_closed"] = True

    # 7. stale assessment rejection
    stale_ledger = ExecutionLedger(account, calendar=calendar)
    stale_intent = _intent("smoke-stale")
    stale_ledger.append(
        OrderIntended(
            "smoke-event-stale-intent", _instant(SMOKE_FRI, 1), stale_intent
        )
    )
    stale_fingerprint = account_state_fingerprint(
        stale_ledger.snapshot(_instant(SMOKE_FRI, 2))
    )
    intent_other = _intent("smoke-other", minute=11)
    stale_ledger.append(
        OrderIntended(
            "smoke-event-other-intent", _instant(SMOKE_FRI, 11), intent_other
        )
    )
    _assess_and_submit(
        stale_ledger, engine, intent_other, fee_cap_fen=SMOKE_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    stale_ledger.append(
        _fill(
            "smoke-other", "smoke-other-fill", _instant(SMOKE_FRI, 30),
            quantity=100,
        )
    )
    try:
        stale_ledger.append(
            replace(
                engine.assess(
                    stale_intent, _account(), _instant(SMOKE_FRI, 60),
                    suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
                    daily_bar_available=None,
                ).event,
                account_fingerprint=stale_fingerprint,
            )
        )
        evidence["stale_assessment_rejected"] = False
    except LedgerTransitionError:
        evidence["stale_assessment_rejected"] = True

    replayed = ExecutionLedger.replay(account, cash_ledger.events, calendar=calendar)
    evidence["event_replay_matches_fresh_run"] = (
        replayed.events == cash_ledger.events
        and replayed.reservations == cash_ledger.reservations
    )
    return evidence
