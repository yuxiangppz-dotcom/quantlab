"""Pure stock research arithmetic; no fills, fee caps, account writes or returns.

The cash field accounts for declared components only. Missing costs, corporate
actions and fillability remain unknown. A separate source-binding/replay layer
must verify actual input documents and lifecycle/tradability before integration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.execution.models import EXCHANGE_TIMEZONE, Side
from quantlab.execution.rules import (
    AShareTradingRule,
    InstrumentIdentity,
    RuleSource,
    TradingCalendar,
)
from quantlab.research.costs import DEFAULT_COST_PROFILE, estimate_research_order_components

VERSION = "stock_research_quantity_cash_v1"


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise DataValidationError(f"{name} must be an integer >= {minimum}")


def _date(value, name):
    if type(value) is not date:
        raise DataValidationError(f"{name} must be an explicit date")


def _raw_fen(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise DataValidationError("raw price must be a positive finite Decimal")
    numerator, denominator = value.as_integer_ratio()
    if numerator * 100 % denominator:
        raise DataValidationError("stock raw price must be exactly representable in fen")
    return numerator * 100 // denominator


def _sha(value):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise DataValidationError("source fingerprint must be SHA-256")


def fingerprint(value):
    def encode(item):
        if is_dataclass(item):
            return {
                f.name: encode(getattr(item, f.name))
                for f in fields(item)
                if not f.name.startswith("_")
            }
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, tuple):
            return [encode(x) for x in item]
        if item is None or type(item) in (str, int, bool):
            return item
        # Existing rule fields include string enums, serialized by their value.
        if isinstance(item, str):
            return str(item)
        raise DataValidationError("unsupported evidence fingerprint value")

    return hashlib.sha256(
        json.dumps(encode(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ResearchMark:
    instrument_id: str
    session: date
    raw_price: Decimal
    basis: str
    available_at: datetime
    source_fingerprint: str

    def __post_init__(self):
        _date(self.session, "mark session")
        _sha(self.source_fingerprint)
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise DataValidationError("mark instrument is missing")
        if self.basis != "raw_unadjusted":
            raise DataValidationError("quantity arithmetic requires raw unadjusted currency price")
        _raw_fen(self.raw_price)
        if not isinstance(self.available_at, datetime) or self.available_at.utcoffset() is None:
            raise DataValidationError("price availability must be timezone aware")
        if self.available_at.astimezone(EXCHANGE_TIMEZONE).date() < self.session:
            raise DataValidationError("price cannot be available before its market date")


@dataclass(frozen=True)
class ResearchLot:
    instrument_id: str
    acquired_session: date
    quantity: int

    def __post_init__(self):
        _date(self.acquired_session, "acquisition session")
        _int(self.quantity, "lot quantity", 1)
        if not isinstance(self.instrument_id, str) or not self.instrument_id.strip():
            raise DataValidationError("lot instrument is missing")


@dataclass(frozen=True)
class ResearchCashState:
    as_of_session: date
    declared_components_cash_fen: int
    lots: tuple[ResearchLot, ...] = ()
    complete_cash_fen: None = field(default=None, init=False)
    execution_authority: bool = field(default=False, init=False)

    def __post_init__(self):
        _date(self.as_of_session, "state session")
        _int(self.declared_components_cash_fen, "declared-components cash")
        if type(self.lots) is not tuple or any(type(lot) is not ResearchLot for lot in self.lots):
            raise DataValidationError("lots must be an immutable tuple of research lots")
        if any(lot.acquired_session > self.as_of_session for lot in self.lots):
            raise DataValidationError("lot acquisition is after the state date")


@dataclass(frozen=True)
class StockContext:
    mark: ResearchMark
    identity: InstrumentIdentity
    rule: AShareTradingRule
    calendar: TradingCalendar
    knowledge_cutoff: datetime
    cost_profile_fingerprint: str

    def __post_init__(self):
        if (
            type(self.mark) is not ResearchMark
            or type(self.identity) is not InstrumentIdentity
            or type(self.rule) is not AShareTradingRule
            or type(self.calendar) is not TradingCalendar
        ):
            raise DataValidationError("stock context requires explicit typed evidence")
        _sha(self.cost_profile_fingerprint)
        day = self.mark.session
        if (
            type(self.calendar.sessions) is not tuple
            or type(self.rule.sources) is not tuple
            or any(type(source) is not RuleSource for source in self.rule.sources)
            or type(self.rule.supported_order_types) is not tuple
            or type(self.rule.supported_sessions) is not tuple
            or type(self.rule.limitations) is not tuple
            or type(self.rule.allow_full_odd_lot_exit) is not bool
        ):
            raise DataValidationError("rule/calendar evidence must retain immutable typed fields")
        _date(self.calendar.coverage_start, "calendar start")
        _date(self.calendar.coverage_end, "calendar end")
        for session in self.calendar.sessions:
            _date(session, "calendar session")
        if (
            not isinstance(self.knowledge_cutoff, datetime)
            or self.knowledge_cutoff.utcoffset() is None
        ):
            raise DataValidationError("knowledge cutoff must be timezone aware")
        if self.mark.available_at > self.knowledge_cutoff:
            raise DataValidationError("price not available at the stated knowledge cutoff")
        if self.calendar.session_status(day) is not True:
            raise DataValidationError("mark date is not a covered market session")
        if (
            self.mark.instrument_id != self.identity.instrument_id
            or not self.identity.applies_on(day)
            or not self.rule.applies_on(day)
            or (self.identity.exchange, self.identity.board)
            != (self.rule.exchange, self.rule.board)
        ):
            raise DataValidationError("price, identity and dated rule do not match")
        if (self.rule.exchange, self.rule.board) not in {
            ("SSE", "MAIN"),
            ("SSE", "STAR"),
            ("SZSE", "MAIN"),
            ("SZSE", "CHINEXT"),
        }:
            raise DataValidationError("only the reviewed common-stock scopes are supported")
        if any(source.published_on > day for source in self.rule.sources):
            raise DataValidationError("rule source published after the scenario date")
        if (
            self.rule.sellability_lag_sessions < 1
            or _raw_fen(self.mark.raw_price) % _raw_fen(self.rule.price_tick) != 0
        ):
            raise DataValidationError("unsupported stock sellability or off-tick raw price")


@dataclass(frozen=True)
class QuantityQuote:
    version: str
    context_fingerprint: str
    cost_profile_fingerprint: str
    side: str
    quantity: int
    notional_fen: int
    commission_fen: int
    stamp_duty_fen: int
    known_components_subtotal_fen: int
    cash_change_after_declared_components_fen: int
    complete_trading_cost_fen: None = field(default=None, init=False)
    fillability: None = field(default=None, init=False)
    corporate_action_cash_fen: None = field(default=None, init=False)
    execution_authority: bool = field(default=False, init=False)
    performance_evidence: bool = field(default=False, init=False)


def quote_stock_quantity(
    context, *, side, quantity, total_position_quantity=0, profile_path: Path = DEFAULT_COST_PROFILE
):
    """One hypothetical order aggregate; never split orders to change minimum fees."""
    if type(context) is not StockContext or type(side) is not Side:
        raise DataValidationError("typed stock context and Side are required")
    _int(quantity, "quantity", 1)
    _int(total_position_quantity, "position quantity")
    if side is Side.SELL and quantity > total_position_quantity:
        raise DataValidationError("sale exceeds the supplied hypothetical position")
    if not context.rule.quantity_is_admissible(
        side=side, quantity=quantity, total_position_quantity=total_position_quantity
    ):
        raise DataValidationError("quantity violates minimum, step or single-order maximum")
    notional = _raw_fen(context.mark.raw_price) * quantity
    # Do not inherit a caller's low-precision Decimal context for fee arithmetic.
    with localcontext() as arithmetic:
        arithmetic.prec = max(50, len(str(notional)) + 20)
        costs = estimate_research_order_components(
            [notional],
            asset_type="stock",
            side=side.value,
            trade_date=context.mark.session,
            profile_path=profile_path,
        )
    if costs["profile_fingerprint"] != context.cost_profile_fingerprint:
        raise DataValidationError("research cost profile changed")
    subtotal = costs["known_components_subtotal_fen"]
    change = (-notional if side is Side.BUY else notional) - subtotal
    return QuantityQuote(
        VERSION,
        fingerprint(context),
        context.cost_profile_fingerprint,
        side.value,
        quantity,
        notional,
        costs["commission_fen"],
        costs["stamp_duty_fen"],
        subtotal,
        change,
    )


def size_stock_buy(
    context,
    *,
    desired_quantity,
    declared_components_budget_fen,
    profile_path: Path = DEFAULT_COST_PROFILE,
):
    """Largest permitted quantity within a *declared-components-only* budget.

    None means even the minimum quantity cannot fit. This is not a complete-cost
    affordability check, fee cap or allocation of the rest of a portfolio.
    """
    if type(context) is not StockContext:
        raise DataValidationError("typed stock context is required")
    _int(desired_quantity, "desired quantity")
    _int(declared_components_budget_fen, "declared-components budget")
    rule = context.rule
    if desired_quantity > rule.max_limit_quantity:
        raise DataValidationError("desired quantity exceeds single-order maximum; no splitting")
    # Validate fee scope/identity even if a zero budget will select no quantity.
    minimum = quote_stock_quantity(
        context, side=Side.BUY, quantity=rule.buy_min_quantity, profile_path=profile_path
    )
    if (
        desired_quantity < rule.buy_min_quantity
        or -minimum.cash_change_after_declared_components_fen > declared_components_budget_fen
    ):
        return None
    low, high = 0, (desired_quantity - rule.buy_min_quantity) // rule.buy_quantity_step
    best = minimum
    while low <= high:
        middle = (low + high) // 2
        quote = quote_stock_quantity(
            context,
            side=Side.BUY,
            quantity=rule.buy_min_quantity + middle * rule.buy_quantity_step,
            profile_path=profile_path,
        )
        if -quote.cash_change_after_declared_components_fen <= declared_components_budget_fen:
            best, low = quote, middle + 1
        else:
            high = middle - 1
    return best


@dataclass(frozen=True)
class HypotheticalMovement:
    before_fingerprint: str
    after_fingerprint: str
    quote: QuantityQuote
    after: ResearchCashState
    lot_reduction_convention: str = field(
        default="fifo_scenario_not_dividend_tax_authority", init=False
    )
    actual_fill: None = field(default=None, init=False)
    complete_cash_fen: None = field(default=None, init=False)
    execution_authority: bool = field(default=False, init=False)


def apply_stock_quantity(
    state, context, *, side, quantity, profile_path: Path = DEFAULT_COST_PROFILE
):
    """Return a new hypothetical state; no ledger or broker event is constructed.

    The calendar and lots establish only the modeled settlement-lag subset.
    Suspensions, limits, available liquidity and corporate-action completeness
    still require a separate integration gate before a portfolio replay.
    """
    if type(state) is not ResearchCashState or type(context) is not StockContext:
        raise DataValidationError("typed hypothetical state/context required")
    day = context.mark.session
    calendar = context.calendar
    if state.as_of_session > day or calendar.session_status(state.as_of_session) is not True:
        raise DataValidationError("state date is future or outside calendar coverage")
    if any(calendar.session_status(lot.acquired_session) is not True for lot in state.lots):
        raise DataValidationError("lot acquisition has unknown/nontrading calendar evidence")
    code = context.mark.instrument_id
    total = sum(lot.quantity for lot in state.lots if lot.instrument_id == code)
    quote = quote_stock_quantity(
        context,
        side=side,
        quantity=quantity,
        total_position_quantity=total,
        profile_path=profile_path,
    )
    cash = state.declared_components_cash_fen + quote.cash_change_after_declared_components_fen
    if cash < 0:
        raise DataValidationError("insufficient cash even under declared components")
    lots = list(state.lots)
    if side is Side.BUY:
        lots.append(ResearchLot(code, day, quantity))
    else:
        index = {s: i for i, s in enumerate(calendar.sessions)}
        eligible = sorted(
            (
                i
                for i, lot in enumerate(lots)
                if lot.instrument_id == code
                and index[day] - index[lot.acquired_session]
                >= context.rule.sellability_lag_sessions
            ),
            key=lambda i: (lots[i].acquired_session, i),
        )
        if quantity > sum(lots[i].quantity for i in eligible):
            raise DataValidationError("sale exceeds research T+1 sellable quantity")
        remaining = quantity
        for i in eligible:
            reduction = min(remaining, lots[i].quantity)
            if reduction:
                left = lots[i].quantity - reduction
                lots[i] = ResearchLot(code, lots[i].acquired_session, left) if left else None
                remaining -= reduction
        lots = [lot for lot in lots if lot is not None]
    after = ResearchCashState(day, cash, tuple(lots))
    after_quantity = sum(lot.quantity for lot in after.lots if lot.instrument_id == code)
    signed = quantity if side is Side.BUY else -quantity
    if after_quantity != total + signed:
        raise DataValidationError("hypothetical quantity conservation failed")
    return HypotheticalMovement(fingerprint(state), fingerprint(after), quote, after)
