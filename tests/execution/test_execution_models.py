from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantlab.execution import (
    EXCHANGE_TIMEZONE,
    AccountSnapshot,
    ConstraintDecision,
    ConstraintDimension,
    ConstraintStatus,
    ExecutionValidationError,
    InstructionSourceMetadata,
    OrderIntent,
    OrderType,
    PositionLot,
    PositionTarget,
    PriceBasis,
    RebalanceInstruction,
    Side,
    exchange_date,
)


def _source_metadata(target_fingerprint: str) -> InstructionSourceMetadata:
    return InstructionSourceMetadata(
        target_as_of=date(2026, 1, 9),
        target_fingerprint=target_fingerprint,
        planning_input_fingerprint="c" * 64,
        planner_version="planner-v1",
        planning_nav_fen=1_000_000,
        minimum_cash_fen=0,
        planning_price_basis=PriceBasis.RAW,
        planning_price_policy="raw-close-known-by-cutoff",
        share_rounding_policy="buy-lot-floor",
        cash_policy="target-cash",
        planning_price_source_ids=("raw-daily-bar",),
    )


def test_rebalance_instruction_is_share_denominated_and_pit_ordered() -> None:
    cutoff = datetime(2026, 1, 9, 7, 0, tzinfo=UTC)
    instruction = RebalanceInstruction(
        instruction_id="rebalance-1",
        portfolio_id="strategy-v1",
        signal_as_of=cutoff,
        execution_date=date(2026, 1, 12),
        targets=(PositionTarget("000001.SZ", 200),),
        source_fingerprint="a" * 64,
        source_metadata=_source_metadata("a" * 64),
    )
    assert exchange_date(cutoff) == date(2026, 1, 9)
    assert instruction.targets[0].target_shares == 200

    with pytest.raises(ExecutionValidationError, match="precedes"):
        RebalanceInstruction(
            instruction_id="rebalance-2",
            portfolio_id="strategy-v1",
            signal_as_of=cutoff,
            execution_date=date(2026, 1, 8),
            targets=(),
            source_fingerprint="b" * 64,
            source_metadata=_source_metadata("b" * 64),
        )


def test_rebalance_instruction_binds_source_fingerprint_to_metadata() -> None:
    with pytest.raises(ExecutionValidationError, match="must match"):
        RebalanceInstruction(
            instruction_id="rebalance-3",
            portfolio_id="strategy-v1",
            signal_as_of=datetime(2026, 1, 9, 15, 1, tzinfo=EXCHANGE_TIMEZONE),
            execution_date=date(2026, 1, 12),
            targets=(),
            source_fingerprint="a" * 64,
            source_metadata=_source_metadata("b" * 64),
        )


@pytest.mark.parametrize("bad_quantity", [True, 1.5, -1])
def test_share_quantities_must_be_nonnegative_integers(bad_quantity) -> None:
    with pytest.raises(ExecutionValidationError, match="target_shares"):
        PositionTarget("000001.SZ", bad_quantity)


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ExecutionValidationError, match="timezone-aware"):
        AccountSnapshot(
            account_id="account-1",
            as_of=datetime(2026, 1, 9, 9, 0),
            cash_fen=100_000,
        )


def test_order_price_rejects_float_and_inconsistent_order_type() -> None:
    created_at = datetime(2026, 1, 9, 9, 0, tzinfo=EXCHANGE_TIMEZONE)
    with pytest.raises(ExecutionValidationError, match="Decimal"):
        OrderIntent(
            order_id="order-1",
            instruction_id="rebalance-1",
            instrument_id="000001.SZ",
            side=Side.BUY,
            quantity=100,
            order_type=OrderType.LIMIT,
            limit_price=10.0,  # type: ignore[arg-type]
            intended_trade_date=date(2026, 1, 9),
            created_at=created_at,
            limit_price_basis=PriceBasis.RAW,
            limit_price_source_id="raw-bar-close",
        )
    with pytest.raises(ExecutionValidationError, match="cannot carry"):
        OrderIntent(
            order_id="order-2",
            instruction_id="rebalance-1",
            instrument_id="000001.SZ",
            side=Side.BUY,
            quantity=100,
            order_type=OrderType.MARKET,
            limit_price=Decimal("10.00"),
            intended_trade_date=date(2026, 1, 9),
            created_at=created_at,
            limit_price_basis=PriceBasis.RAW,
            limit_price_source_id="raw-bar-close",
        )


def test_snapshot_rejects_float_cash_and_duplicate_lots() -> None:
    lot = PositionLot(
        lot_id="lot-1",
        instrument_id="000001.SZ",
        quantity=100,
        acquired_trade_date=date(2026, 1, 8),
        sellable_from=date(2026, 1, 9),
    )
    with pytest.raises(ExecutionValidationError, match="cash_fen"):
        AccountSnapshot(
            account_id="account-1",
            as_of=datetime(2026, 1, 9, 9, 0, tzinfo=EXCHANGE_TIMEZONE),
            cash_fen=100.0,  # type: ignore[arg-type]
        )
    with pytest.raises(ExecutionValidationError, match="duplicate lot_id"):
        AccountSnapshot(
            account_id="account-1",
            as_of=datetime(2026, 1, 9, 9, 0, tzinfo=EXCHANGE_TIMEZONE),
            cash_fen=100_000,
            lots=(lot, lot),
        )


def test_constraint_decision_requires_enum_and_unique_rules() -> None:
    assessed_at = datetime(2026, 1, 9, 9, 1, tzinfo=EXCHANGE_TIMEZONE)
    with pytest.raises(ExecutionValidationError, match="ConstraintStatus"):
        ConstraintDecision(
            decision_id="decision-1",
            dimension=ConstraintDimension.FILLABILITY,
            status="allowed",  # type: ignore[arg-type]
            reason_code="ok",
            message="",
            assessed_at=assessed_at,
        )
    with pytest.raises(ExecutionValidationError, match="duplicate rule_id"):
        ConstraintDecision(
            decision_id="decision-2",
            dimension=ConstraintDimension.FILLABILITY,
            status=ConstraintStatus.ALLOWED,
            reason_code="ok",
            message="",
            assessed_at=assessed_at,
            rule_ids=("rule-1", "rule-1"),
        )
