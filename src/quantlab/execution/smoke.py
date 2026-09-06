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

# cumulative order-lifetime fee cap used by the reconciliation scenario
RECON_FEE_CAP_FEN = 1_000


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
    fee_fen: int = 500,
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
        fee_fen=fee_fen,
        buy_lot_sellable_from=sellable_from,
    )


def _order_price_evidence(
    instrument_id: str = SMOKE_INSTRUMENT,
) -> OrderPriceEvidence:
    return OrderPriceEvidence(
        instrument_id=instrument_id,
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

    # 2. account-aware planning through the REAL handoff + order adapter:
    #    positive TargetPortfolio -> RebalanceInstruction -> OrderPlan ->
    #    OrderBatch. No order field is copied by hand anywhere below.
    from quantlab.execution.handoff import (
        PlanningPrice,
        TargetHandoffConfig,
        build_rebalance_instruction,
    )
    from quantlab.execution.orchestration import (
        materialize_order_batch,
        verify_lineage,
    )
    from quantlab.execution.planning import ExecutionStateView
    from quantlab.portfolio import TargetPortfolio, TargetWeight

    planning_nav_fen = 4_000_000
    plan_account = _account(
        as_of=_instant(SMOKE_FRI, 0), cash_fen=50_000_000
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
    state_view = ExecutionStateView(
        account=plan_account,
        available_cash_fen=50_000_000,
        available_sellable_shares={"600001.SH": 200},
        fingerprint="sha256:" + "5" * 64,
    )
    planning_prices = {
        SMOKE_INSTRUMENT: PlanningPrice(
            instrument_id=SMOKE_INSTRUMENT,
            price=SMOKE_PRICE,
            price_date=SMOKE_FRI,
            available_at=_instant(SMOKE_FRI, 360),
            basis=PriceBasis.RAW,
            source_id="synthetic-raw-price-source",
        ),
    }
    order_prices = {
        SMOKE_INSTRUMENT: _order_price_evidence(),
        "600001.SH": _order_price_evidence("600001.SH"),
    }
    fee_caps = {SMOKE_INSTRUMENT: _fee_cap()}

    def _handoff_plan(weights, *, account=None, view=None, cash_weight=0.1):
        target = TargetPortfolio(
            as_of=SMOKE_FRI, positions=tuple(weights), cash_weight=cash_weight
        )
        handoff = build_rebalance_instruction(
            target,
            TargetHandoffConfig(
                portfolio_id="smoke-portfolio",
                signal_as_of=_instant(SMOKE_FRI, 375),
                execution_date=SMOKE_MON,
                planning_nav_fen=planning_nav_fen,
                minimum_cash_fen=0,
            ),
            planning_prices,
            calendar=calendar,
            identities=identities,
            rules=rules,
        )
        instruction = handoff.instruction
        assert instruction is not None
        plan = build_order_plan(
            instruction,
            account or plan_account,
            created_at=_instant(SMOKE_MON, 0),
            order_prices=order_prices,
            fee_caps=fee_caps,
            calendar=calendar,
            identities=identities,
            rules=rules,
            execution_state=view or state_view,
        )
        return instruction, plan

    instruction_one, plan_one = _handoff_plan(
        (TargetWeight(SMOKE_INSTRUMENT, 0.9),)
    )
    instruction_two, plan_two = _handoff_plan(
        (TargetWeight(SMOKE_INSTRUMENT, 0.9),)
    )
    leg = plan_one.legs[0]
    batch = materialize_order_batch(
        instruction_one,
        plan_one,
        state=state_view,
        created_at=_instant(SMOKE_MON, 1),
    )
    target_shares = leg.target_shares
    evidence["order_plan_deterministic"] = (
        plan_one.plan_id == plan_two.plan_id
        and batch.intents
        == materialize_order_batch(
            instruction_one,
            plan_one,
            state=state_view,
            created_at=_instant(SMOKE_MON, 1),
        ).intents
    )
    evidence["buy_limit_from_order_price_evidence"] = (
        leg.limit_price_source_fingerprint == "1" * 64
        and leg.limit_price_available_at == _instant(SMOKE_FRI, 0)
    )
    evidence["buys_funded_from_available_cash_only"] = (
        plan_one.worst_case_cash_fen_required
        == target_shares * 1_000 + SMOKE_FEE_CAP_FEN
    )
    intent = batch.intents[0]
    request = batch.requests[0]
    drifted_view = replace(
        state_view, fingerprint="sha256:" + "6" * 64
    )
    try:
        verify_lineage(intent, instruction_one, plan_one, drifted_view)
        drift_detected = False
    except Exception:
        drift_detected = True
    evidence["plan_to_order_lineage"] = (
        intent.plan_id == plan_one.plan_id
        and intent.leg_id == leg.leg_id
        and intent.instruction_id == instruction_one.instruction_id
        and intent.execution_state_fingerprint == state_view.fingerprint
        and intent.limit_price_source_fingerprint == "1" * 64
        and intent.fee_quote_fingerprint == "4" * 64
        and intent.intended_trade_date == SMOKE_MON
        and intent.time_in_force.value == "day"
        and intent.quantity == target_shares
        and all(
            getattr(request, field) == getattr(intent, field)
            for field in (
                "instrument_id", "side", "quantity", "order_type",
                "limit_price", "intended_trade_date", "limit_price_basis",
                "limit_price_source_id", "time_in_force",
            )
        )
        and drift_detected
    )
    evidence["account_aware_order_planning"] = (
        evidence["order_plan_deterministic"]
        and evidence["buy_limit_from_order_price_evidence"]
        and evidence["buys_funded_from_available_cash_only"]
        and plan_one.status.value == "submit_ready"
    )

    omitted = _handoff_plan((TargetWeight(SMOKE_INSTRUMENT, 0.9),))
    evidence["omitted_held_name_exits"] = any(
        item.side is Side.SELL and item.delta_shares == -200
        and item.status.value == "orderable"
        for item in omitted[1].legs
    )

    # a delta that cannot satisfy the lot grid blocks the leg: the account
    # already holds 50 shares, so the planned buy delta is 3600 - 50 = 3550
    odd_lot = PositionLot(
        lot_id="smoke-lot-odd-600000",
        instrument_id=SMOKE_INSTRUMENT,
        quantity=50,
        acquired_trade_date=SMOKE_THU,
        sellable_from=SMOKE_FRI,
    )
    odd_account = replace(
        plan_account, lots=(odd_lot,) + plan_account.lots
    )
    odd_view = replace(state_view, account=odd_account)
    _, odd_plan = _handoff_plan(
        (TargetWeight(SMOKE_INSTRUMENT, 0.9),), account=odd_account, view=odd_view
    )
    evidence["non_conforming_delta_blocks"] = (
        odd_plan.status.value == "blocked"
        and odd_plan.legs[0].reason_code == "buy_delta_not_lot_conforming"
        and odd_plan.legs[0].target_shares == 3600
        and odd_plan.legs[0].delta_shares == 3550
    )

    # 2b. transactional submission of the real adapter batch: intents ->
    #     assessments (execution-state bound) -> submit_orders with the
    #     typed fee quote. Internal ledger OrderSubmitted events are NOT an
    #     external broker submission. The buy leg carries the full path;
    #     the omitted-exit sell leg is exercised by the share-reservation
    #     scenario below.
    lineage_ledger = ExecutionLedger(
        _account(as_of=_instant(SMOKE_MON, 0), cash_fen=5_000_000),
        calendar=calendar,
    )
    submit_batch = materialize_order_batch(
        instruction_one,
        plan_one,
        state=state_view,
        created_at=_instant(SMOKE_MON, 10),
    )
    buy_pairs = [
        (order_intent, order_request)
        for order_intent, order_request in zip(
            submit_batch.intents, submit_batch.requests, strict=True
        )
        if order_intent.side is Side.BUY
    ]
    for order_intent, order_request in buy_pairs:
        lineage_ledger.append(
            OrderIntended(
                f"smoke-event-{order_intent.order_id}",
                order_intent.created_at,
                order_intent,
            )
        )
        assessment = engine.assess(
            order_intent,
            lineage_ledger.snapshot(order_intent.created_at),
            order_intent.created_at,
            suspension=_open(SMOKE_MON),
            fee_schedule=_smoke_fee(),
            daily_bar_available=None,
            execution_state_fingerprint=(
                lineage_ledger.execution_state_fingerprint()
            ),
        )
        lineage_ledger.append(assessment.event)
        lineage_ledger.submit_orders(
            [
                (
                    OrderSubmitted(
                        f"smoke-event-{order_intent.order_id}-submitted",
                        order_request.created_at,
                        order_request,
                        worst_case_fee_fen=SMOKE_FEE_CAP_FEN,
                        execution_state_fingerprint=(
                            lineage_ledger.execution_state_fingerprint()
                        ),
                    ),
                    _fee_cap(),
                )
            ]
        )
    evidence["transactional_submission_committed"] = all(
        item.status.value == "submitted" for item in lineage_ledger.orders
    ) and lineage_ledger.reserved_cash_fen() == (
        target_shares * 1_000 + SMOKE_FEE_CAP_FEN
    )
    evidence["external_broker_submission"] = False

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

    # 8. order-lifetime fee budget: multi-partial-fill reconciliation,
    #    price-improvement release, full-fill release
    recon_ledger = ExecutionLedger(account, calendar=calendar)
    recon_intent = _intent("smoke-recon", quantity=100)
    recon_ledger.append(
        OrderIntended(
            "smoke-event-recon-intent", _instant(SMOKE_FRI, 1), recon_intent
        )
    )
    _assess_and_submit(
        recon_ledger, engine, recon_intent, fee_cap_fen=RECON_FEE_CAP_FEN,
        suspension=_open(SMOKE_FRI), fee_schedule=_smoke_fee(),
    )
    initial_reservation = recon_ledger.reserved_cash_fen()
    recon_ledger.append(
        _fill("smoke-recon", "smoke-recon-f1", _instant(SMOKE_FRI, 60),
              quantity=40, fee_fen=500)
    )
    after_fill_one = recon_ledger.reserved_cash_fen()
    recon_ledger.append(
        FillRecorded(
            event_id="smoke-event-recon-f2",
            fill_id="smoke-recon-f2",
            occurred_at=_instant(SMOKE_FRI, 61),
            order_id="smoke-recon",
            trade_date=SMOKE_FRI,
            quantity=60,
            price=Decimal("9.90"),
            gross_notional_fen=59_400,
            fee_fen=500,
            buy_lot_sellable_from=SMOKE_MON,
        )
    )
    fully_filled = recon_ledger.order("smoke-recon")
    evidence["multi_partial_fee_reconciliation"] = (
        initial_reservation == 100 * 1_000 + RECON_FEE_CAP_FEN
        and after_fill_one == 60 * 1_000 + (RECON_FEE_CAP_FEN - 500)
        and fully_filled.status.value == "filled"
        and fully_filled.fee_fen == RECON_FEE_CAP_FEN
        and recon_ledger.reserved_cash_fen() == 0
    )
    evidence["full_fill_release"] = (
        fully_filled.status.value == "filled"
        and recon_ledger.reserved_cash_fen() == 0
    )
    fault_report = run_transaction_fault_injection()
    evidence["batch_rollback_preserves_state"] = (
        fault_report["all_passed"]
    )
    evidence["fault_injection_matrix"] = fault_report
    return evidence


def _ledger_snapshot(ledger: ExecutionLedger) -> tuple:
    """White-box snapshot for before/after fault-injection comparison."""
    return (
        ledger.cash_fen,
        ledger.lots,
        tuple(ledger.orders),
        ledger.reservations,
        tuple(ledger.events),
        dict(ledger._events_by_id),
        set(ledger._fill_ids),
        set(ledger._request_ids),
    )


def run_transaction_fault_injection() -> dict:
    """Prove strong exception safety on the real ledger.

    Injects Exceptions and KeyboardInterrupt at the first, middle, and last
    batch submission, right after a reservation is drawn down, and around
    the invariant check; the ledger state must be byte-for-byte identical
    after each injection. No provider, no broker, no canonical write.
    """
    from quantlab.execution.models import (
        AccountSnapshot,
        OrderRequest,
        OrderType,
    )

    calendar = _calendar()
    engine = AShareConstraintEngine(
        calendar=calendar,
        identities=_identities(),
        rules=default_a_share_rule_book(),
    )
    report: dict[str, object] = {
        "synthetic": True,
        "non_trading": True,
        "external_broker_submission": False,
    }

    def snapshot(ledger):
        return _ledger_snapshot(ledger)

    def fresh(count: int = 3):
        ledger = ExecutionLedger(
            AccountSnapshot(
                account_id="fault-account",
                as_of=_instant(SMOKE_FRI, 0),
                cash_fen=5_000_000,
                lots=(),
            ),
            calendar=calendar,
        )
        events = []
        for index in range(count):
            order_id = f"fault-buy-{index}"
            intent = _intent(order_id, minute=1 + index * 10)
            ledger.append(
                OrderIntended(
                    f"fault-{order_id}-intended",
                    _instant(SMOKE_FRI, 1 + index * 10),
                    intent,
                )
            )
            assess_at = _instant(SMOKE_FRI, 3 + index * 10)
            result = engine.assess(
                intent,
                ledger.snapshot(assess_at),
                assess_at,
                suspension=_open(SMOKE_FRI),
                fee_schedule=_smoke_fee(),
                daily_bar_available=None,
                execution_state_fingerprint=(
                    ledger.execution_state_fingerprint()
                ),
            )
            ledger.append(result.event)
            request = OrderRequest(
                request_id=f"fault-request-{order_id}",
                order_id=order_id,
                instrument_id=intent.instrument_id,
                side=intent.side,
                quantity=intent.quantity,
                order_type=OrderType.LIMIT,
                limit_price=intent.limit_price,
                intended_trade_date=intent.intended_trade_date,
                created_at=_instant(SMOKE_FRI, 40 + index),
                limit_price_basis=intent.limit_price_basis,
                limit_price_source_id=intent.limit_price_source_id,
                time_in_force=intent.time_in_force,
            )
            events.append(
                OrderSubmitted(
                    f"fault-{order_id}-submitted",
                    request.created_at,
                    request,
                    worst_case_fee_fen=SMOKE_FEE_CAP_FEN,
                    execution_state_fingerprint=(
                        ledger.execution_state_fingerprint()
                    ),
                )
            )
        return ledger, events

    from quantlab.execution.planning import FeeCapQuote

    fault_quote = FeeCapQuote(
        instrument_id=SMOKE_INSTRUMENT,
        account_id="fault-account",
        trade_date=SMOKE_FRI,
        cap_fen=SMOKE_FEE_CAP_FEN,
        evidence_id="fault-fee-quote",
        source_fingerprint="7" * 64,
        synthetic=True,
    )

    def inject_batch(index: int, error: type[BaseException]) -> bool:
        ledger, events = fresh()
        before = snapshot(ledger)
        original = ExecutionLedger._apply_submitted
        calls = {"n": 0}

        def failing(self, event):
            if calls["n"] == index:
                calls["n"] += 1
                raise error(f"injected at {index}")
            calls["n"] += 1
            original(self, event)

        ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
        try:
            try:
                ledger.submit_orders(
                    [(event, fault_quote) for event in events]
                )
            except error:
                return snapshot(ledger) == before
            return False
        finally:
            ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]

    def inject_invariant() -> bool:
        ledger, events = fresh()
        before = snapshot(ledger)
        original = ExecutionLedger._check_invariants

        def failing(self):
            raise LedgerAccountingError("injected invariant failure")

        ExecutionLedger._check_invariants = failing  # type: ignore[method-assign]
        try:
            try:
                ledger.submit_orders([(events[0], fault_quote)])
            except LedgerAccountingError:
                return snapshot(ledger) == before
            return False
        finally:
            ExecutionLedger._check_invariants = original  # type: ignore[method-assign]

    def inject_single_append() -> bool:
        ledger, events = fresh()
        before = snapshot(ledger)
        original = ExecutionLedger._apply_submitted

        def failing(self, event):
            raise KeyboardInterrupt

        ExecutionLedger._apply_submitted = failing  # type: ignore[method-assign]
        try:
            try:
                ledger.append(events[0])
            except KeyboardInterrupt:
                return snapshot(ledger) == before
            return False
        finally:
            ExecutionLedger._apply_submitted = original  # type: ignore[method-assign]

    results = {
        "batch_first_submission_runtimeerror_restored": inject_batch(
            0, RuntimeError
        ),
        "batch_middle_submission_keyboardinterrupt_restored": inject_batch(
            1, KeyboardInterrupt
        ),
        "batch_last_submission_runtimeerror_restored": inject_batch(
            2, RuntimeError
        ),
        "invariant_failure_after_mutation_restored": inject_invariant(),
        "single_append_baseexception_restored": inject_single_append(),
    }
    report.update(results)
    report["all_passed"] = all(results.values())
    return report
