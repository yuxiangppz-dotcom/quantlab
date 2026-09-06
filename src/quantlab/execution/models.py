"""Immutable execution-domain contracts for A-share order handling.

Money posted to the account is always represented in integer fen. Prices are
``Decimal`` values and are never implicitly constructed from binary floats.
Every timestamp is an aware instant; every ``trade_date`` is an explicit
Asia/Shanghai exchange-local date rather than an inferred UTC date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

EXCHANGE_TIMEZONE_NAME = "Asia/Shanghai"
EXCHANGE_TIMEZONE = ZoneInfo(EXCHANGE_TIMEZONE_NAME)


class ExecutionValidationError(ValueError):
    """An execution contract is internally inconsistent."""


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class ConstraintStatus(StrEnum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ConstraintDimension(StrEnum):
    ORDER_ADMISSIBILITY = "order_admissibility"
    POSITION_SELLABILITY = "position_sellability"
    MARKET_ACCESSIBILITY = "market_accessibility"
    FILLABILITY = "fillability"
    FEE_DETERMINABILITY = "fee_determinability"


class OrderStatus(StrEnum):
    INTENDED = "intended"
    VALIDATED = "validated"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"


def require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionValidationError(f"{field_name} must be timezone-aware")


def exchange_date(value: datetime) -> date:
    """Return the explicit Shanghai-local date for an aware instant."""
    require_aware(value, "timestamp")
    return value.astimezone(EXCHANGE_TIMEZONE).date()


def require_int(
    value: int,
    field_name: str,
    *,
    minimum: int = 0,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExecutionValidationError(
            f"{field_name} must be an integer >= {minimum}, got {value!r}"
        )


def require_decimal(
    value: Decimal,
    field_name: str,
    *,
    positive: bool = False,
) -> None:
    if not isinstance(value, Decimal):
        raise ExecutionValidationError(
            f"{field_name} must be Decimal, never float, got {type(value).__name__}"
        )
    if not value.is_finite() or (positive and value <= 0):
        predicate = "finite and > 0" if positive else "finite"
        raise ExecutionValidationError(f"{field_name} must be {predicate}, got {value}")


def require_identifier(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ExecutionValidationError(f"{field_name} must be non-empty")


@dataclass(frozen=True)
class PositionTarget:
    instrument_id: str
    target_shares: int

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.target_shares, "target_shares")


@dataclass(frozen=True)
class RebalanceInstruction:
    """Share-denominated target handed from portfolio construction.

    It is an instruction, not an order and never evidence of a fill.
    ``signal_as_of`` is an aware information cutoff; ``execution_date`` is an
    exchange-local session requested no earlier than that cutoff's local day.
    """

    instruction_id: str
    portfolio_id: str
    signal_as_of: datetime
    execution_date: date
    targets: tuple[PositionTarget, ...]
    source_fingerprint: str

    def __post_init__(self) -> None:
        require_identifier(self.instruction_id, "instruction_id")
        require_identifier(self.portfolio_id, "portfolio_id")
        require_identifier(self.source_fingerprint, "source_fingerprint")
        require_aware(self.signal_as_of, "signal_as_of")
        if self.execution_date < exchange_date(self.signal_as_of):
            raise ExecutionValidationError(
                "execution_date precedes the Shanghai-local signal cutoff"
            )
        ids = [target.instrument_id for target in self.targets]
        if len(ids) != len(set(ids)):
            raise ExecutionValidationError("duplicate instrument target")


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    instrument_id: str
    quantity: int
    acquired_trade_date: date
    sellable_from: date

    def __post_init__(self) -> None:
        require_identifier(self.lot_id, "lot_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.quantity, "quantity", minimum=1)
        if self.sellable_from <= self.acquired_trade_date:
            raise ExecutionValidationError(
                "sellable_from must be after acquired_trade_date (T+1)"
            )


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    as_of: datetime
    cash_fen: int
    lots: tuple[PositionLot, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.account_id, "account_id")
        require_aware(self.as_of, "as_of")
        require_int(self.cash_fen, "cash_fen")
        lot_ids = [lot.lot_id for lot in self.lots]
        if len(lot_ids) != len(set(lot_ids)):
            raise ExecutionValidationError("duplicate lot_id in account snapshot")
        local_as_of = exchange_date(self.as_of)
        if any(lot.acquired_trade_date > local_as_of for lot in self.lots):
            raise ExecutionValidationError("account snapshot contains a future lot")


@dataclass(frozen=True)
class OrderIntent:
    order_id: str
    instruction_id: str
    instrument_id: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    intended_trade_date: date
    created_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.order_id, "order_id")
        require_identifier(self.instruction_id, "instruction_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.quantity, "quantity", minimum=1)
        require_aware(self.created_at, "created_at")
        if not isinstance(self.side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        if not isinstance(self.order_type, OrderType):
            raise ExecutionValidationError("order_type must be an OrderType enum")
        if self.intended_trade_date < exchange_date(self.created_at):
            raise ExecutionValidationError(
                "intended_trade_date precedes the Shanghai-local creation date"
            )
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ExecutionValidationError("limit order requires limit_price")
            require_decimal(self.limit_price, "limit_price", positive=True)
        elif self.limit_price is not None:
            raise ExecutionValidationError("market order cannot carry limit_price")


@dataclass(frozen=True)
class OrderRequest:
    """Immutable broker-facing request; it must exactly match its intent."""

    request_id: str
    order_id: str
    instrument_id: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    created_at: datetime

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.order_id, "order_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.quantity, "quantity", minimum=1)
        require_aware(self.created_at, "created_at")
        if not isinstance(self.side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        if not isinstance(self.order_type, OrderType):
            raise ExecutionValidationError("order_type must be an OrderType enum")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ExecutionValidationError("limit request requires limit_price")
            require_decimal(self.limit_price, "limit_price", positive=True)
        elif self.limit_price is not None:
            raise ExecutionValidationError("market request cannot carry limit_price")


@dataclass(frozen=True)
class ConstraintDecision:
    decision_id: str
    dimension: ConstraintDimension
    status: ConstraintStatus
    reason_code: str
    message: str
    assessed_at: datetime
    rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.decision_id, "decision_id")
        require_identifier(self.reason_code, "reason_code")
        require_aware(self.assessed_at, "assessed_at")
        if not isinstance(self.dimension, ConstraintDimension):
            raise ExecutionValidationError(
                "dimension must be a ConstraintDimension enum"
            )
        if not isinstance(self.status, ConstraintStatus):
            raise ExecutionValidationError("status must be a ConstraintStatus enum")
        if len(self.rule_ids) != len(set(self.rule_ids)):
            raise ExecutionValidationError("duplicate rule_id in constraint decision")
        for rule_id in self.rule_ids:
            require_identifier(rule_id, "rule_id")
