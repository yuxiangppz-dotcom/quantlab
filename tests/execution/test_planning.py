"""Account-aware order planning (v0.2): instruction + account -> plan."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from quantlab.execution import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    ExecutionValidationError,
    InstrumentIdentity,
    OrderPlanLegStatus,
    OrderPlanStatus,
    OrderPriceEvidence,
    PITIdentityBook,
    PITRuleBook,
    PositionLot,
    PositionTarget,
    PriceBasis,
    RebalanceInstruction,
    Side,
    build_order_plan,
    default_a_share_rule_book,
    fingerprint_account_state,
)
from quantlab.execution.handoff import InstructionSourceMetadata
from quantlab.execution.planning import FeeCapQuote

SIGNAL = datetime(2024, 11, 29, 15, 30, tzinfo=EXCHANGE_TIMEZONE)
CREATED = datetime(2024, 12, 2, 9, 20, tzinfo=EXCHANGE_TIMEZONE)
EXECUTION = date(2024, 12, 2)
FRIDAY = date(2024, 11, 29)
SYNTHETIC_SOURCE = "0" * 64


def _calendar() -> object:
    from quantlab.execution import TradingCalendar

    return TradingCalendar(
        sessions=(FRIDAY, EXECUTION),
        coverage_start=FRIDAY,
        coverage_end=EXECUTION,
        source_id="synthetic-calendar",
        source_sha256="c" * 64,
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
                source_record_id="identity-600000",
            ),
            InstrumentIdentity(
                instrument_id="600001.SH",
                exchange="SSE",
                board="MAIN",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="identity-600001",
            ),
        )
    )


def _rules() -> PITRuleBook:
    return default_a_share_rule_book()


def _metadata(fingerprint: str) -> InstructionSourceMetadata:
    return InstructionSourceMetadata(
        target_as_of=FRIDAY,
        target_fingerprint=fingerprint,
        planning_input_fingerprint="1" * 64,
        planner_version="target_weight_to_share_target_v0_1",
        planning_nav_fen=1_000_000_00,
        minimum_cash_fen=0,
        planning_price_basis=PriceBasis.RAW,
        planning_price_policy="raw_observation_available_by_signal_cutoff",
        share_rounding_policy="applicable_buy_lot_floor_from_zero",
        cash_policy="max_target_cash_weight_and_minimum_cash_fen",
        planning_price_source_ids=("planning-source",),
    )


def _instruction(targets) -> RebalanceInstruction:
    import hashlib
    import json

    fingerprint = hashlib.sha256(
        json.dumps(
            [(t.instrument_id, t.target_shares) for t in targets], sort_keys=True
        ).encode()
    ).hexdigest()
    return RebalanceInstruction(
        instruction_id="instruction-1",
        portfolio_id="portfolio-1",
        signal_as_of=SIGNAL,
        execution_date=EXECUTION,
        targets=tuple(targets),
        source_fingerprint=fingerprint,
        source_metadata=_metadata(fingerprint),
    )


def _account(
    *,
    cash_fen: int = 500_000_00,
    lots=(),
    as_of: datetime | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="account-1",
        as_of=as_of or datetime(2024, 11, 29, 16, 0, tzinfo=EXCHANGE_TIMEZONE),
        cash_fen=cash_fen,
        lots=tuple(lots),
    )


def _price(instrument_id: str = "600000.SH") -> OrderPriceEvidence:
    return OrderPriceEvidence(
        instrument_id=instrument_id,
        price=Decimal("10.00"),
        price_date=FRIDAY,
        available_at=datetime(2024, 11, 29, 16, 0, tzinfo=EXCHANGE_TIMEZONE),
        basis=PriceBasis.RAW,
        source_id="raw-close-source",
        source_fingerprint=SYNTHETIC_SOURCE,
    )


def _fee_cap(instrument_id: str = "600000.SH", cap: int = 30_000) -> FeeCapQuote:
    return FeeCapQuote(
        instrument_id=instrument_id,
        cap_fen=cap,
        evidence_id="synthetic-fee-cap-test-only",
        synthetic=True,
    )


def _plan(instruction, account, *, prices=None, fee_caps=None):
    return build_order_plan(
        instruction,
        account,
        created_at=CREATED,
        order_prices=prices if prices is not None else {
            "600000.SH": _price(),
            "600001.SH": _price("600001.SH"),
        },
        fee_caps=fee_caps if fee_caps is not None else {
            "600000.SH": _fee_cap(),
            "600001.SH": _fee_cap("600001.SH"),
        },
        calendar=_calendar(),
        identities=_identities(),
        rules=_rules(),
    )


def _by_instrument(plan):
    return {leg.instrument_id: leg for leg in plan.legs}


def test_target_400_current_100_plans_buy_300() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.delta_shares == 300
    assert leg.side is Side.BUY
    assert leg.status is OrderPlanLegStatus.ORDERABLE
    assert leg.limit_price == Decimal("10.00")
    assert leg.limit_price_basis is PriceBasis.RAW
    assert leg.limit_price_source_fingerprint == SYNTHETIC_SOURCE
    assert plan.status is OrderPlanStatus.SUBMIT_READY


def test_omitted_held_name_gets_target_zero_and_sell() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(
            PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),
            PositionLot("lot-2", "600001.SH", 200, date(2024, 11, 28), FRIDAY),
        )
    )
    plan = _plan(instruction, account)
    legs = _by_instrument(plan)
    # 600001.SH is held but omitted from the instruction: target zero
    assert legs["600001.SH"].target_shares == 0
    assert legs["600001.SH"].delta_shares == -200
    assert legs["600001.SH"].side is Side.SELL
    assert legs["600001.SH"].status is OrderPlanLegStatus.ORDERABLE
    assert plan.status is OrderPlanStatus.SUBMIT_READY


def test_buy_limit_price_is_never_the_handoff_planning_price() -> None:
    """The plan's limit comes from separate raw order-price evidence with its
    own available_at and source fingerprint, never from handoff inputs."""
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.limit_price_source_id == "raw-close-source"
    assert leg.limit_price_available_at == datetime(
        2024, 11, 29, 16, 0, tzinfo=EXCHANGE_TIMEZONE
    )
    # handoff planning prices are a different type entirely; the planner
    # rejects them with a structured reason instead of using their value
    from quantlab.execution.handoff import PlanningPrice

    plan = _plan(
        instruction,
        account,
        prices={
            "600000.SH": PlanningPrice(  # type: ignore[dict-item]
                instrument_id="600000.SH",
                price=Decimal("10.00"),
                price_date=FRIDAY,
                available_at=SIGNAL,
                basis=PriceBasis.RAW,
                source_id="planning-source",
            )
        },
    )
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "order_price_evidence_invalid"
    assert leg.limit_price is None


def test_adjusted_or_future_or_missing_order_price_blocks_leg() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    adjusted = OrderPriceEvidence(
        instrument_id="600000.SH",
        price=Decimal("9.00"),
        price_date=FRIDAY,
        available_at=datetime(2024, 11, 29, 16, 0, tzinfo=EXCHANGE_TIMEZONE),
        basis=PriceBasis.ADJUSTED,
        source_id="adjusted-source",
        source_fingerprint=SYNTHETIC_SOURCE,
    )
    plan = _plan(instruction, account, prices={"600000.SH": adjusted})
    assert _by_instrument(plan)["600000.SH"].status is OrderPlanLegStatus.BLOCKED
    assert plan.status is OrderPlanStatus.BLOCKED

    future = OrderPriceEvidence(
        instrument_id="600000.SH",
        price=Decimal("10.00"),
        price_date=EXECUTION,
        available_at=datetime(2024, 12, 2, 15, 0, tzinfo=EXCHANGE_TIMEZONE),
        basis=PriceBasis.RAW,
        source_id="future-source",
        source_fingerprint=SYNTHETIC_SOURCE,
    )
    plan = _plan(instruction, account, prices={"600000.SH": future})
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "order_price_evidence_from_the_future"

    plan = _plan(instruction, account, prices={})
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.reason_code == "order_price_evidence_missing"


def test_fee_cap_unknown_blocks_buy_leg_but_target_unchanged() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    plan = _plan(instruction, account, fee_caps={})
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "fee_cap_unknown"
    assert leg.target_shares == 400  # the target is never silently rounded
    assert plan.status is OrderPlanStatus.BLOCKED


def test_odd_lot_buy_delta_is_blocked_without_target_change() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 450)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.delta_shares == 350
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "buy_delta_not_lot_conforming"
    assert leg.target_shares == 450
    assert plan.status is OrderPlanStatus.BLOCKED


def test_full_odd_lot_exit_is_sellable() -> None:
    instruction = _instruction([PositionTarget("600001.SH", 0)])
    account = _account(
        lots=(PositionLot("lot-1", "600001.SH", 137, date(2024, 11, 28), FRIDAY),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600001.SH"]
    assert leg.delta_shares == -137
    assert leg.side is Side.SELL
    assert leg.status is OrderPlanLegStatus.ORDERABLE
    assert plan.status is OrderPlanStatus.SUBMIT_READY


def test_partial_odd_lot_sell_is_blocked() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 40)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 137, date(2024, 11, 28), FRIDAY),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.delta_shares == -97
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "sell_delta_not_lot_conforming"


def test_sell_blocked_when_t_plus_one_sellable_is_short() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 0)])
    account = _account(
        lots=(
            PositionLot("lot-1", "600000.SH", 100, EXECUTION, date(2024, 12, 3)),
        ),
        as_of=CREATED,
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.status is OrderPlanLegStatus.BLOCKED
    assert leg.reason_code == "insufficient_sellable_shares_t_plus_one"


def test_insufficient_sellable_is_reported_for_every_leg(tmp_path=None) -> None:
    instruction = _instruction(
        [PositionTarget("600000.SH", 0), PositionTarget("600001.SH", 0)]
    )
    account = _account(
        lots=(
            PositionLot("lot-1", "600000.SH", 100, EXECUTION, date(2024, 12, 3)),
            PositionLot("lot-2", "600001.SH", 100, EXECUTION, date(2024, 12, 3)),
        ),
        as_of=CREATED,
    )
    plan = _plan(instruction, account)
    assert plan.status is OrderPlanStatus.BLOCKED
    assert all(
        leg.status is OrderPlanLegStatus.BLOCKED
        and leg.reason_code == "insufficient_sellable_shares_t_plus_one"
        for leg in plan.legs
    )


def test_aggregate_buy_worst_case_exceeding_cash_blocks_plan() -> None:
    instruction = _instruction(
        [PositionTarget("600000.SH", 400), PositionTarget("600001.SH", 400)]
    )
    account = _account(
        lots=(
            PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),
            PositionLot("lot-2", "600001.SH", 100, FRIDAY, EXECUTION),
        ),
        cash_fen=600_000,
    )
    plan = _plan(instruction, account)
    # each leg individually looks fundable (300 shares x 10.00 + fee cap);
    # together (660_000 fen) they exceed the 600_000 fen available cash
    assert plan.status is OrderPlanStatus.BLOCKED
    assert "aggregate_buy_worst_case_exceeds_available_cash" in plan.reason_codes


def test_not_traded_leg_when_already_at_target() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 100)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    plan = _plan(instruction, account)
    leg = _by_instrument(plan)["600000.SH"]
    assert leg.status is OrderPlanLegStatus.NOT_TRADED
    assert leg.reason_code == "already_at_target"
    assert plan.status is OrderPlanStatus.SUBMIT_READY


def test_plan_ids_and_fingerprints_are_deterministic() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account(
        lots=(PositionLot("lot-1", "600000.SH", 100, FRIDAY, EXECUTION),)
    )
    first = _plan(instruction, account)
    second = _plan(instruction, account)
    assert first.plan_id == second.plan_id
    assert first.instruction_fingerprint == second.instruction_fingerprint
    assert first.account_fingerprint == fingerprint_account_state(account)
    assert first.account_fingerprint != fingerprint_account_state(
        _account(cash_fen=499_999_00)
    )
    # any economic change reshuffles the plan id
    changed = _plan(
        _instruction([PositionTarget("600000.SH", 500)]),
        account,
    )
    assert changed.plan_id != first.plan_id


def test_plan_created_after_execution_date_is_rejected() -> None:
    instruction = _instruction([PositionTarget("600000.SH", 400)])
    account = _account()
    with pytest.raises(ExecutionValidationError, match="execution date"):
        build_order_plan(
            instruction,
            account,
            created_at=datetime(2024, 12, 3, 9, 0, tzinfo=EXCHANGE_TIMEZONE),
            order_prices={"600000.SH": _price()},
            fee_caps={"600000.SH": _fee_cap()},
            calendar=_calendar(),
            identities=_identities(),
            rules=_rules(),
        )
