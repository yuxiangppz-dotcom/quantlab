"""Immutable execution-domain contracts for A-share order handling.

Money posted to the account is always represented in integer fen. Prices are
``Decimal`` values and are never implicitly constructed from binary floats.
Every timestamp is an aware instant; every ``trade_date`` is an explicit
Asia/Shanghai exchange-local date rather than an inferred UTC date.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from quantlab.execution.planning import FeeCapQuote

EXCHANGE_TIMEZONE_NAME = "Asia/Shanghai"
EXCHANGE_TIMEZONE = ZoneInfo(EXCHANGE_TIMEZONE_NAME)


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ExecutionValidationError(ValueError):
    """An execution contract is internally inconsistent."""


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    """Explicit time-in-force. A-share equity orders in scope are DAY only.

    DAY is declared explicitly rather than implied: an order without an
    intended trade date can never bind a fill's exchange-local trade date,
    and no silent GTC default exists in this layer.
    """

    DAY = "day"


class OrderSession(StrEnum):
    OPENING_AUCTION = "opening_auction"
    CONTINUOUS_AUCTION = "continuous_auction"
    CLOSING_AUCTION = "closing_auction"
    AFTER_HOURS_FIXED = "after_hours_fixed"


class PriceBasis(StrEnum):
    RAW = "raw_unadjusted"
    ADJUSTED = "adjusted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OrderPriceEvidence:
    """Raw, unadjusted, independently sourced order-price evidence.

    This is the ONLY permitted source of an order limit price. It is
    deliberately a different type from the handoff planning price so the
    two roles can never be conflated. Defined here (not in planning) so
    the ledger and events can verify its canonical fingerprint without an
    import cycle.
    """

    instrument_id: str
    price: Decimal
    price_date: date
    available_at: datetime
    basis: PriceBasis
    source_id: str
    source_fingerprint: str

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_decimal(self.price, "order price", positive=True)
        require_aware(self.available_at, "order price available_at")
        if not isinstance(self.basis, PriceBasis):
            raise ExecutionValidationError("order price basis must be a PriceBasis")
        require_identifier(self.source_id, "order price source_id")
        if len(self.source_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_fingerprint
        ):
            raise ExecutionValidationError(
                "order price source_fingerprint must be SHA-256"
            )


def fingerprint_order_price_evidence(evidence: OrderPriceEvidence) -> str:
    """Canonical SHA-256 of the full order-price evidence payload."""
    return "sha256:" + _canonical_hash({
        "instrument_id": evidence.instrument_id,
        "price": str(evidence.price),
        "price_date": evidence.price_date.isoformat(),
        "available_at": evidence.available_at.isoformat(),
        "basis": evidence.basis.value,
        "source_id": evidence.source_id,
        "source_fingerprint": evidence.source_fingerprint,
    })


@dataclass(frozen=True)
class FeeCapQuote:
    """Explicit worst-case fee cap used to reserve cash for a buy.

    The cap is the CUMULATIVE fee ceiling over the whole lifetime of one
    order (every partial fill included). The quote is typed provenance, not
    a bare integer: it binds the instrument, the account, the intended
    trade date, the fee-schedule evidence id, and a SHA-256 fingerprint of
    the schedule evidence it was derived from. ``synthetic`` marks test-only
    quotes. Production callers must supply a quote derived from a real,
    effective-dated, account-specific fee schedule; without one the buy leg
    stays unknown and never reserves cash on an assumed zero fee or an
    invented bps number.
    """

    instrument_id: str
    account_id: str
    trade_date: date
    cap_fen: int
    evidence_id: str
    source_fingerprint: str
    synthetic: bool

    def __post_init__(self) -> None:
        require_identifier(self.instrument_id, "instrument_id")
        require_identifier(self.account_id, "account_id")
        if not isinstance(self.trade_date, date):
            raise ExecutionValidationError("trade_date must be a date")
        require_int(self.cap_fen, "cap_fen", minimum=1)
        require_identifier(self.evidence_id, "evidence_id")
        if len(self.source_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_fingerprint
        ):
            raise ExecutionValidationError(
                "fee quote source_fingerprint must be SHA-256"
            )
        if not isinstance(self.synthetic, bool):
            raise ExecutionValidationError(
                "fee quote synthetic flag must be a strict bool"
            )


def fingerprint_fee_cap_quote(quote) -> str:
    """Canonical SHA-256 of the FULL fee quote payload.

    The lineage fingerprint carried by legs, intents, assessments,
    requests, and submission events. Derived from every quote field -
    instrument, account, trade date, cap, evidence id, source
    fingerprint, synthetic flag - never copied from the source SHA.
    """
    return _canonical_hash({
        "instrument_id": quote.instrument_id,
        "account_id": quote.account_id,
        "trade_date": quote.trade_date.isoformat(),
        "cap_fen": quote.cap_fen,
        "evidence_id": quote.evidence_id,
        "source_fingerprint": quote.source_fingerprint,
        "synthetic": quote.synthetic,
    })


def fingerprint_order_intent(intent: OrderIntent) -> str:
    """Canonical SHA-256 of the intent's full economic payload."""
    return "sha256:" + _canonical_hash({
        "order_id": intent.order_id,
        "instruction_id": intent.instruction_id,
        "instrument_id": intent.instrument_id,
        "side": intent.side.value,
        "quantity": intent.quantity,
        "order_type": intent.order_type.value,
        "limit_price": (
            str(intent.limit_price) if intent.limit_price is not None else None
        ),
        "intended_trade_date": intent.intended_trade_date.isoformat(),
        "created_at": intent.created_at.isoformat(),
        "session": intent.session.value,
        "limit_price_basis": (
            intent.limit_price_basis.value
            if intent.limit_price_basis is not None
            else None
        ),
        "limit_price_source_id": intent.limit_price_source_id,
        "time_in_force": intent.time_in_force.value,
        "plan_id": intent.plan_id,
        "leg_id": intent.leg_id,
        "availability_fingerprint": intent.availability_fingerprint,
        "limit_price_source_fingerprint": intent.limit_price_source_fingerprint,
        "fee_quote_fingerprint": intent.fee_quote_fingerprint,
    })


def fingerprint_decisions(decisions: tuple) -> str:
    """Canonical SHA-256 of a complete constraint-decision tuple."""
    return "sha256:" + _canonical_hash({
        "decisions": [
            {
                "decision_id": decision.decision_id,
                "dimension": decision.dimension.value,
                "status": decision.status.value,
                "reason_code": decision.reason_code,
                "rule_ids": list(decision.rule_ids),
            }
            for decision in decisions
        ],
    })


@dataclass(frozen=True)
class AssessmentAuthority:
    """The immutable authorization produced by one constraint assessment.

    Persisted by the ledger when the assessment is appended; a submission
    must reference THIS object's ids, never a fresh fingerprint. Every
    field is mandatory on the production order path.
    """

    assessment_event_id: str
    order_id: str
    intent_fingerprint: str
    decision_fingerprint: str
    availability_fingerprint: str
    assessed_at: datetime
    dimension_statuses: tuple[tuple[str, str], ...]
    fee_schedule_evidence_id: str | None
    fee_schedule_source_fingerprint: str | None
    instruction_id: str
    plan_id: str | None = None
    leg_id: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.assessment_event_id, "assessment_event_id")
        require_identifier(self.order_id, "order_id")
        require_identifier(self.intent_fingerprint, "intent_fingerprint")
        require_identifier(
            self.decision_fingerprint, "decision_fingerprint"
        )
        require_identifier(
            self.availability_fingerprint, "availability_fingerprint"
        )
        require_aware(self.assessed_at, "assessed_at")
        require_identifier(self.instruction_id, "instruction_id")
        if not self.dimension_statuses:
            raise ExecutionValidationError(
                "assessment authority requires the full dimension results"
            )


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
class InstructionSourceMetadata:
    """Immutable provenance for a portfolio-to-shares handoff."""

    target_as_of: date
    target_fingerprint: str
    planning_input_fingerprint: str
    planner_version: str
    planning_nav_fen: int
    minimum_cash_fen: int
    planning_price_basis: PriceBasis
    planning_price_policy: str
    share_rounding_policy: str
    cash_policy: str
    planning_price_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.target_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.target_fingerprint
        ):
            raise ExecutionValidationError("target_fingerprint must be SHA-256")
        if len(self.planning_input_fingerprint) != 64 or any(
            char not in "0123456789abcdef"
            for char in self.planning_input_fingerprint
        ):
            raise ExecutionValidationError(
                "planning_input_fingerprint must be SHA-256"
            )
        require_identifier(self.planner_version, "planner_version")
        require_int(self.planning_nav_fen, "planning_nav_fen", minimum=1)
        require_int(self.minimum_cash_fen, "minimum_cash_fen")
        if self.minimum_cash_fen > self.planning_nav_fen:
            raise ExecutionValidationError("minimum_cash_fen exceeds planning NAV")
        if self.planning_price_basis is not PriceBasis.RAW:
            raise ExecutionValidationError(
                "portfolio handoff requires raw unadjusted planning prices"
            )
        require_identifier(self.planning_price_policy, "planning_price_policy")
        require_identifier(self.share_rounding_policy, "share_rounding_policy")
        require_identifier(self.cash_policy, "cash_policy")
        if tuple(sorted(set(self.planning_price_source_ids))) != (
            self.planning_price_source_ids
        ):
            raise ExecutionValidationError(
                "planning_price_source_ids must be unique and sorted"
            )
        for source_id in self.planning_price_source_ids:
            require_identifier(source_id, "planning_price_source_id")


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
    source_metadata: InstructionSourceMetadata

    def __post_init__(self) -> None:
        require_identifier(self.instruction_id, "instruction_id")
        require_identifier(self.portfolio_id, "portfolio_id")
        require_identifier(self.source_fingerprint, "source_fingerprint")
        if self.source_fingerprint != self.source_metadata.target_fingerprint:
            raise ExecutionValidationError(
                "source_fingerprint must match source metadata target fingerprint"
            )
        require_aware(self.signal_as_of, "signal_as_of")
        if self.source_metadata.target_as_of != exchange_date(self.signal_as_of):
            raise ExecutionValidationError(
                "source metadata target_as_of must match the signal cutoff date"
            )
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
    session: OrderSession = OrderSession.CONTINUOUS_AUCTION
    limit_price_basis: PriceBasis | None = None
    limit_price_source_id: str | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    plan_id: str | None = None
    leg_id: str | None = None
    availability_fingerprint: str | None = None
    limit_price_source_fingerprint: str | None = None
    fee_quote_fingerprint: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.order_id, "order_id")
        require_identifier(self.instruction_id, "instruction_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.quantity, "quantity", minimum=1)
        require_aware(self.created_at, "created_at")
        if not isinstance(self.time_in_force, TimeInForce):
            raise ExecutionValidationError("time_in_force must be a TimeInForce enum")
        if self.time_in_force is not TimeInForce.DAY:
            raise ExecutionValidationError(
                "only DAY orders are modeled in this layer"
            )
        if not isinstance(self.side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        if not isinstance(self.order_type, OrderType):
            raise ExecutionValidationError("order_type must be an OrderType enum")
        if not isinstance(self.session, OrderSession):
            raise ExecutionValidationError("session must be an OrderSession enum")
        for fingerprint, name in (
            (self.plan_id, "plan_id"),
            (self.leg_id, "leg_id"),
            (self.availability_fingerprint, "availability_fingerprint"),
        ):
            if fingerprint is not None:
                require_identifier(fingerprint, name)
        for fingerprint, name in (
            (self.limit_price_source_fingerprint, "limit_price_source_fingerprint"),
            (self.fee_quote_fingerprint, "fee_quote_fingerprint"),
        ):
            if fingerprint is not None and (
                len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)
            ):
                raise ExecutionValidationError(f"{name} must be SHA-256")
        if self.intended_trade_date < exchange_date(self.created_at):
            raise ExecutionValidationError(
                "intended_trade_date precedes the Shanghai-local creation date"
            )
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ExecutionValidationError("limit order requires limit_price")
            require_decimal(self.limit_price, "limit_price", positive=True)
            if not isinstance(self.limit_price_basis, PriceBasis):
                raise ExecutionValidationError(
                    "limit order requires an explicit PriceBasis"
                )
            if self.limit_price_source_id is None:
                raise ExecutionValidationError(
                    "limit order requires limit_price_source_id"
                )
            require_identifier(self.limit_price_source_id, "limit_price_source_id")
        elif any(
            value is not None
            for value in (
                self.limit_price,
                self.limit_price_basis,
                self.limit_price_source_id,
            )
        ):
            raise ExecutionValidationError(
                "market order cannot carry limit-price fields"
            )


@dataclass(frozen=True)
class OrderRequest:
    """Immutable broker-facing request; it must exactly match its intent.

    ``intended_trade_date`` is carried on the request itself so the DAY
    order's economic trade date is bound end to end: a broker report may
    arrive later, but its trade date can never drift from the intent.
    """

    request_id: str
    order_id: str
    instrument_id: str
    side: Side
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None
    intended_trade_date: date
    created_at: datetime
    session: OrderSession = OrderSession.CONTINUOUS_AUCTION
    limit_price_basis: PriceBasis | None = None
    limit_price_source_id: str | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    # v0.2.2 lineage bindings: the ledger verifies these against the
    # stored assessment authority before any reservation is created
    instruction_id: str | None = None
    plan_id: str | None = None
    leg_id: str | None = None
    assessment_event_id: str | None = None
    assessment_decision_fingerprint: str | None = None
    availability_fingerprint: str | None = None
    limit_price_source_fingerprint: str | None = None
    fee_quote_fingerprint: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.request_id, "request_id")
        require_identifier(self.order_id, "order_id")
        require_identifier(self.instrument_id, "instrument_id")
        require_int(self.quantity, "quantity", minimum=1)
        require_aware(self.created_at, "created_at")
        if not isinstance(self.time_in_force, TimeInForce):
            raise ExecutionValidationError("time_in_force must be a TimeInForce enum")
        if self.time_in_force is not TimeInForce.DAY:
            raise ExecutionValidationError(
                "only DAY orders are modeled in this layer"
            )
        if not isinstance(self.side, Side):
            raise ExecutionValidationError("side must be a Side enum")
        if not isinstance(self.order_type, OrderType):
            raise ExecutionValidationError("order_type must be an OrderType enum")
        if not isinstance(self.session, OrderSession):
            raise ExecutionValidationError("session must be an OrderSession enum")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None:
                raise ExecutionValidationError("limit request requires limit_price")
            require_decimal(self.limit_price, "limit_price", positive=True)
            if not isinstance(self.limit_price_basis, PriceBasis):
                raise ExecutionValidationError(
                    "limit request requires an explicit PriceBasis"
                )
            if self.limit_price_source_id is None:
                raise ExecutionValidationError(
                    "limit request requires limit_price_source_id"
                )
            require_identifier(self.limit_price_source_id, "limit_price_source_id")
        elif any(
            value is not None
            for value in (
                self.limit_price,
                self.limit_price_basis,
                self.limit_price_source_id,
            )
        ):
            raise ExecutionValidationError(
                "market request cannot carry limit-price fields"
            )


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


def derive_order_status(
    side: Side,
    decisions: tuple[ConstraintDecision, ...],
) -> OrderStatus:
    """Fail-closed aggregate of the five independent constraint dimensions.

    Submission eligibility (``VALIDATED``) and eventual fillability are
    deliberately distinct: unknown fill probability or queue position is an
    auditable residual risk, never a reason to block an otherwise fully
    qualified limit order. What MUST gate submission is anything that makes
    the order terms or the market itself unverifiable:

    - order admissibility unknown/rejected;
    - market accessibility unknown/rejected;
    - fee determinability unknown/rejected;
    - position sellability unknown/rejected (sell orders only).

    Fillability unknown keeps the order submittable with the uncertainty
    recorded in its audit trail; a fillability REJECTION still blocks.
    """
    by_dimension = {decision.dimension: decision.status for decision in decisions}
    if set(by_dimension) != set(ConstraintDimension):
        raise ExecutionValidationError("cannot derive status without all dimensions")
    if by_dimension[ConstraintDimension.FILLABILITY] is ConstraintStatus.REJECTED:
        return OrderStatus.REJECTED
    if ConstraintStatus.REJECTED in set(by_dimension.values()):
        return OrderStatus.REJECTED
    required_allowed = {
        ConstraintDimension.ORDER_ADMISSIBILITY,
        ConstraintDimension.MARKET_ACCESSIBILITY,
        ConstraintDimension.FEE_DETERMINABILITY,
    }
    if side is Side.SELL:
        required_allowed.add(ConstraintDimension.POSITION_SELLABILITY)
    if any(
        by_dimension[dimension] is ConstraintStatus.UNKNOWN
        for dimension in required_allowed
    ):
        return OrderStatus.UNKNOWN
    return (
        OrderStatus.VALIDATED
        if all(
            by_dimension[dimension] is ConstraintStatus.ALLOWED
            for dimension in required_allowed
        )
        else OrderStatus.UNKNOWN
    )
