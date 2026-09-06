"""Plan-to-order lineage adapter tests (v0.2.1 Part E).

The v0.2 smoke hand-built OrderIntent/OrderRequest fields, so nothing
proved an OrderPlan actually reaches the order path, and price/fee
evidence was accepted across instruments. v0.2.1 adds a pure adapter
``materialize_order_batch`` that turns a validated plan plus a
reservation-aware account state into deterministic intents and requests
bound to instruction fingerprint, plan id, leg id, execution-state
fingerprint, price-evidence fingerprint, and fee-quote fingerprint — and
the planner rejects evidence whose instrument does not match its leg.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quantlab.execution.handoff import (
    PlanningPrice,
    TargetHandoffConfig,
    build_rebalance_instruction,
)
from quantlab.execution.ledger import ExecutionLedger
from quantlab.execution.models import (
    AccountSnapshot,
    ExecutionValidationError,
    PositionLot,
    PositionTarget,
    PriceBasis,
    TimeInForce,
)
from quantlab.execution.orchestration import (
    materialize_order_batch,
    verify_lineage,
)
from quantlab.execution.planning import (
    ExecutionStateView,
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
        source_id="lineage-calendar",
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
                source_record_id="lineage-identity",
            ),
            InstrumentIdentity(
                instrument_id="600001.SH",
                exchange="SSE",
                board="MAIN",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                source_record_id="lineage-identity-2",
            ),
        )
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        account_id="lineage-account",
        as_of=_instant(FRI, 500),
        cash_fen=50_000_000,
        lots=(),
    )


def _view(account: AccountSnapshot | None = None) -> ExecutionStateView:
    return ExecutionStateView(
        account=account or _account(),
        available_cash_fen=50_000_000,
        available_sellable_shares={},
        fingerprint="sha256:" + "7" * 64,
    )


def _price_evidence(
    instrument_id: str = "600000.SH",
    *,
    price_date: date = FRI,
    available_at: datetime | None = None,
) -> OrderPriceEvidence:
    return OrderPriceEvidence(
        instrument_id=instrument_id,
        price=PRICE,
        price_date=price_date,
        available_at=available_at or _instant(FRI, 400),
        basis=PriceBasis.RAW,
        source_id="lineage-price-source",
        source_fingerprint="b" * 64,
    )


def _fee_quote(
    instrument_id: str = "600000.SH",
    *,
    account_id: str = "lineage-account",
    trade_date: date = MON,
) -> FeeCapQuote:
    return FeeCapQuote(
        instrument_id=instrument_id,
        account_id=account_id,
        trade_date=trade_date,
        cap_fen=FEE_CAP,
        evidence_id="lineage-fee-quote",
        source_fingerprint="c" * 64,
        synthetic=True,
    )


def _instruction(targets, account: AccountSnapshot | None = None):
    from quantlab.execution.models import InstructionSourceMetadata, RebalanceInstruction

    fingerprint = "d" * 64
    return RebalanceInstruction(
        instruction_id="lineage-instruction",
        portfolio_id="lineage-portfolio",
        signal_as_of=_instant(FRI, 375),
        execution_date=MON,
        targets=tuple(targets),
        source_fingerprint=fingerprint,
        source_metadata=InstructionSourceMetadata(
            target_as_of=FRI,
            target_fingerprint=fingerprint,
            planning_input_fingerprint="e" * 64,
            planner_version="target_weight_to_share_target_v0_1",
            planning_nav_fen=20_000_000,
            minimum_cash_fen=0,
            planning_price_basis=PriceBasis.RAW,
            planning_price_policy="lineage-test",
            share_rounding_policy="lineage-test",
            cash_policy="lineage-test",
            planning_price_source_ids=("lineage-price-source",),
        ),
    )


def _plan(
    targets,
    *,
    prices=None,
    fee_caps=None,
    account: AccountSnapshot | None = None,
    execution_state: ExecutionStateView | None = "unset",
):
    instruction = _instruction(targets, account)
    kwargs = dict(
        created_at=_instant(MON, 0),
        order_prices=(
            prices
            if prices is not None
            else {"600000.SH": _price_evidence()}
        ),
        fee_caps=(
            fee_caps if fee_caps is not None else {"600000.SH": _fee_quote()}
        ),
        calendar=_calendar(),
        identities=_identities(),
        rules=default_a_share_rule_book(),
    )
    if execution_state == "unset":
        kwargs["execution_state"] = _view(account)
    elif execution_state is not None:
        kwargs["execution_state"] = execution_state
    return build_order_plan(instruction, account or _account(), **kwargs)


def test_planner_rejects_cross_instrument_price_evidence() -> None:
    """600000.SH must not plan with 600001.SH price or fee evidence."""
    plan = _plan(
        (PositionTarget("600000.SH", 400),),
        prices={"600000.SH": _price_evidence("600001.SH")},
        execution_state=None,
    )
    leg = plan.legs[0]
    assert leg.status.value == "blocked"
    assert leg.reason_code == "order_price_evidence_instrument_mismatch"


def test_planner_rejects_mismatched_fee_quote() -> None:
    for quote in (
        _fee_quote("600001.SH"),
        _fee_quote(account_id="other-account"),
        _fee_quote(trade_date=FRI),
    ):
        plan = _plan(
            (PositionTarget("600000.SH", 400),),
            fee_caps={"600000.SH": quote},
            execution_state=None,
        )
        assert plan.legs[0].status.value == "blocked"
        assert plan.legs[0].reason_code == "fee_quote_mismatch"


def test_planner_rejects_pre_dated_price_evidence() -> None:
    """available_at must not be earlier than the price date."""
    plan = _plan(
        (PositionTarget("600000.SH", 400),),
        prices={
            "600000.SH": _price_evidence(
                price_date=FRI, available_at=_instant(THU, 500)
            )
        },
        execution_state=None,
    )
    assert plan.legs[0].status.value == "blocked"
    assert plan.legs[0].reason_code == "order_price_evidence_pre_dated"


def test_blocked_plan_materializes_nothing() -> None:
    plan = _plan(
        (PositionTarget("600000.SH", 450),),
        execution_state=None,
    )
    assert plan.status.value == "blocked"
    with pytest.raises(ExecutionValidationError):
        materialize_order_batch(
            _instruction((PositionTarget("600000.SH", 450),)),
            plan,
            state=_view(),
            created_at=_instant(MON, 1),
        )


def test_materialized_batch_binds_full_lineage() -> None:
    account = _account()
    plan = _plan((PositionTarget("600000.SH", 400),), account=account)
    assert plan.status.value == "submit_ready"
    batch = materialize_order_batch(
        _instruction((PositionTarget("600000.SH", 400),)),
        plan,
        state=_view(account),
        created_at=_instant(MON, 1),
    )
    assert len(batch.intents) == 1
    intent = batch.intents[0]
    assert intent.instruction_id == plan.instruction_id
    assert intent.plan_id == plan.plan_id
    assert intent.leg_id == plan.legs[0].leg_id
    assert intent.execution_state_fingerprint == _view(account).fingerprint
    assert intent.limit_price_source_fingerprint == "b" * 64
    assert intent.limit_price == PRICE
    assert intent.limit_price_basis is PriceBasis.RAW
    assert intent.intended_trade_date == MON
    assert intent.time_in_force is TimeInForce.DAY
    assert intent.quantity == 400
    assert intent.fee_quote_fingerprint == "c" * 64
    # deterministic: same inputs, same intents
    batch_two = materialize_order_batch(
        _instruction((PositionTarget("600000.SH", 400),)),
        plan,
        state=_view(account),
        created_at=_instant(MON, 1),
    )
    assert batch_two.intents == batch.intents
    # requests mirror the intents term for term
    assert len(batch.requests) == 1
    request = batch.requests[0]
    for field in (
        "instrument_id", "side", "quantity", "order_type",
        "limit_price", "intended_trade_date", "limit_price_basis",
        "limit_price_source_id", "time_in_force",
    ):
        assert getattr(request, field) == getattr(intent, field)


def test_not_traded_legs_are_not_materialized() -> None:
    lot = PositionLot(
        lot_id="lineage-lot",
        instrument_id="600000.SH",
        quantity=400,
        acquired_trade_date=THU,
        sellable_from=FRI,
    )
    account = AccountSnapshot(
        account_id="lineage-account",
        as_of=_instant(FRI, 500),
        cash_fen=10_000_000,
        lots=(lot,),
    )
    view = ExecutionStateView(
        account=account,
        available_cash_fen=10_000_000,
        available_sellable_shares={"600000.SH": 400},
        fingerprint="sha256:" + "9" * 64,
    )
    plan = _plan(
        (PositionTarget("600000.SH", 400),),
        account=account,
        execution_state=view,
    )
    assert plan.status.value == "submit_ready"
    batch = materialize_order_batch(
        _instruction((PositionTarget("600000.SH", 400),), account),
        plan,
        state=view,
        created_at=_instant(MON, 1),
    )
    assert batch.intents == ()


def test_lineage_drift_invalidates_reuse() -> None:
    account = _account()
    instruction = _instruction((PositionTarget("600000.SH", 400),))
    plan = _plan((PositionTarget("600000.SH", 400),), account=account)
    batch = materialize_order_batch(
        instruction, plan, state=_view(account), created_at=_instant(MON, 1)
    )
    intent = batch.intents[0]
    verify_lineage(intent, instruction, plan, _view(account))
    # drifted plan: same instruction, different account state fingerprint
    drifted_state = ExecutionStateView(
        account=account,
        available_cash_fen=1,
        available_sellable_shares={},
        fingerprint="sha256:" + "8" * 64,
    )
    with pytest.raises(ExecutionValidationError, match="lineage"):
        verify_lineage(intent, instruction, plan, drifted_state)
    # drifted instruction: different targets change the fingerprint
    other_instruction = _instruction((PositionTarget("600000.SH", 500),))
    with pytest.raises(ExecutionValidationError, match="lineage"):
        verify_lineage(intent, other_instruction, plan, _view(account))


def test_full_handoff_to_batch_through_real_adapter() -> None:
    """Positive TargetPortfolio → handoff → plan → batch, no field copying."""
    from quantlab.portfolio import TargetPortfolio, TargetWeight

    calendar = _calendar()
    target = TargetPortfolio(
        as_of=FRI,
        positions=(TargetWeight("600000.SH", 0.9),),
        cash_weight=0.1,
    )
    handoff = build_rebalance_instruction(
        target,
        TargetHandoffConfig(
            portfolio_id="lineage-portfolio",
            signal_as_of=_instant(FRI, 375),
            execution_date=MON,
            planning_nav_fen=20_000_000,
            minimum_cash_fen=0,
        ),
        {
            "600000.SH": PlanningPrice(
                instrument_id="600000.SH",
                price=PRICE,
                price_date=FRI,
                available_at=_instant(FRI, 360),
                basis=PriceBasis.RAW,
                source_id="lineage-price-source",
            )
        },
        calendar=calendar,
        identities=_identities(),
        rules=default_a_share_rule_book(),
    )
    assert handoff.instruction is not None
    instruction = handoff.instruction
    positive_targets = [
        item for item in instruction.targets if item.target_shares > 0
    ]
    assert positive_targets
    account = _account()
    plan = build_order_plan(
        instruction,
        account,
        created_at=_instant(MON, 0),
        order_prices={"600000.SH": _price_evidence()},
        fee_caps={"600000.SH": _fee_quote()},
        calendar=calendar,
        identities=_identities(),
        rules=default_a_share_rule_book(),
        execution_state=_view(account),
    )
    assert plan.status.value == "submit_ready"
    batch = materialize_order_batch(
        instruction, plan, state=_view(account), created_at=_instant(MON, 1)
    )
    assert batch.intents
    ledger = ExecutionLedger(account, calendar=calendar)
    for intent in batch.intents:
        ledger.append(
            __import__("quantlab.execution.ledger", fromlist=["OrderIntended"]).OrderIntended(
                f"lineage-{intent.order_id}", intent.created_at, intent
            )
        )
    # the ledger can now assess and (transactionally) submit the batch
    assert all(
        item.status.value == "intended" for item in ledger.orders
    )
