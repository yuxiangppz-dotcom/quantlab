"""Pure hypothetical stock accounting; neither broker fills nor a return backtest.

Every price, rule, fee and capacity input is a caller-declared research scenario.
This kernel does not certify historical inputs or infer corporate cash flows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext

MAX_INTEGER = 10**15


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError(f"{name} must be an integer in [{minimum}, {MAX_INTEGER}]")


def _date(value: date, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")


def _name(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty identifier")


def _rate(value: Decimal | None, name: str, *, positive: bool = False) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal or explicit unknown")
    if not (0 < value <= 1 if positive else 0 <= value <= 1):
        raise ValueError(f"{name} outside modeled fraction range")


@dataclass(frozen=True)
class ResearchLot:
    lot_id: str
    instrument_id: str
    quantity: int
    acquired_on: date
    sellable_on: date

    def __post_init__(self) -> None:
        _name(self.lot_id, "lot_id")
        _name(self.instrument_id, "instrument_id")
        _integer(self.quantity, "lot quantity", 1)
        _date(self.acquired_on, "acquired_on")
        _date(self.sellable_on, "sellable_on")
        if self.sellable_on <= self.acquired_on:
            raise ValueError("stock lot must remain locked beyond its acquisition date")


@dataclass(frozen=True)
class CapacityUsed:
    instrument_id: str
    trade_date: date
    quantity: int
    notional_fen: int
    session_fingerprint: str

    def __post_init__(self) -> None:
        _name(self.instrument_id, "capacity instrument")
        _date(self.trade_date, "capacity date")
        _integer(self.quantity, "used quantity", 1)
        _integer(self.notional_fen, "used notional", 1)
        if (
            not isinstance(self.session_fingerprint, str)
            or len(self.session_fingerprint) != 64
            or set(self.session_fingerprint) - set("0123456789abcdef")
        ):
            raise ValueError("capacity use must bind the modeled session")


@dataclass(frozen=True)
class ResearchBook:
    asof_date: date
    cash_fen: int
    lots: tuple[ResearchLot, ...] = ()
    capacity_used: tuple[CapacityUsed, ...] = ()
    simulated_order_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _date(self.asof_date, "book date")
        _integer(self.cash_fen, "cash")
        if type(self.lots) is not tuple or any(type(x) is not ResearchLot for x in self.lots):
            raise ValueError("lots must be an immutable tuple of ResearchLot")
        if len({x.lot_id for x in self.lots}) != len(self.lots):
            raise ValueError("duplicate lot identifier")
        if any(x.acquired_on > self.asof_date for x in self.lots):
            raise ValueError("book contains future acquisition")
        if type(self.capacity_used) is not tuple or any(
            type(x) is not CapacityUsed for x in self.capacity_used
        ):
            raise ValueError("capacity_used must be an immutable tuple")
        if len({(x.instrument_id, x.trade_date) for x in self.capacity_used}) != len(
            self.capacity_used
        ):
            raise ValueError("duplicate instrument/date capacity record")
        if any(x.trade_date > self.asof_date for x in self.capacity_used):
            raise ValueError("book contains future capacity use")
        if type(self.simulated_order_ids) is not frozenset:
            raise ValueError("simulated order ids must be immutable")
        for identifier in self.simulated_order_ids:
            _name(identifier, "simulated order id")


@dataclass(frozen=True)
class ResearchQuantityRules:
    scenario_id: str
    effective_from: date
    effective_through: date
    buy_minimum: int
    buy_increment: int
    sell_minimum: int
    sell_increment: int
    max_order_quantity: int
    full_position_odd_exit: bool

    def __post_init__(self) -> None:
        _name(self.scenario_id, "rules scenario")
        _date(self.effective_from, "rule start")
        _date(self.effective_through, "rule end")
        if self.effective_from > self.effective_through:
            raise ValueError("rule interval reversed")
        for name in ("buy_minimum", "buy_increment", "sell_minimum", "sell_increment"):
            _integer(getattr(self, name), name, 1)
        _integer(self.max_order_quantity, "max_order_quantity", 1)
        if self.max_order_quantity < max(self.buy_minimum, self.sell_minimum):
            raise ValueError("single order maximum is below a minimum")
        if type(self.full_position_odd_exit) is not bool:
            raise ValueError("odd exit must be explicit boolean")


@dataclass(frozen=True)
class ResearchFeeScenario:
    """All modeled components must be explicit; this is not verified fee authority."""

    scenario_id: str
    effective_from: date
    effective_through: date
    commission_rate: Decimal | None
    minimum_commission_fen: int | None
    buy_stamp_rate: Decimal | None
    sell_stamp_rate: Decimal | None
    additional_fee_rate: Decimal | None
    additional_fee_fixed_fen: int | None
    adverse_slippage_rate: Decimal | None

    def __post_init__(self) -> None:
        _name(self.scenario_id, "fee scenario")
        _date(self.effective_from, "fee start")
        _date(self.effective_through, "fee end")
        if self.effective_from > self.effective_through:
            raise ValueError("fee interval reversed")
        for name in (
            "commission_rate",
            "buy_stamp_rate",
            "sell_stamp_rate",
            "additional_fee_rate",
            "adverse_slippage_rate",
        ):
            _rate(getattr(self, name), name)
        if self.adverse_slippage_rate == 1:
            raise ValueError("slippage must leave a positive sell price")
        for name in ("minimum_commission_fen", "additional_fee_fixed_fen"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)

    @property
    def complete(self) -> bool:
        return all(
            getattr(self, name) is not None
            for name in (
                "commission_rate",
                "minimum_commission_fen",
                "buy_stamp_rate",
                "sell_stamp_rate",
                "additional_fee_rate",
                "additional_fee_fixed_fen",
                "adverse_slippage_rate",
            )
        )


@dataclass(frozen=True)
class ResearchOrder:
    order_id: str
    instrument_id: str
    side: str
    desired_quantity: int
    signal_date: date

    def __post_init__(self) -> None:
        _name(self.order_id, "order id")
        _name(self.instrument_id, "order instrument")
        if self.side not in {"buy", "sell"}:
            raise ValueError("only long stock buy/sell supported")
        _integer(self.desired_quantity, "desired quantity", 1)
        _date(self.signal_date, "signal date")


@dataclass(frozen=True)
class ResearchSession:
    instrument_id: str
    execution_date: date
    next_session: date | None
    evidence_date: date | None
    calendar_verified: bool | None
    market_open: bool | None
    corporate_actions_processed: bool | None
    raw_close_fen: int | None
    low_fen: int | None
    high_fen: int | None
    down_limit_fen: int | None
    up_limit_fen: int | None
    prior20_amount_fen: int | None
    prior20_asof: date | None
    prior20_sessions: int | None
    session_amount_fen: int | None
    session_volume_shares: int | None
    participation: Decimal | None
    rules: ResearchQuantityRules | None
    fees: ResearchFeeScenario | None

    def __post_init__(self) -> None:
        _name(self.instrument_id, "session instrument")
        _date(self.execution_date, "execution date")
        for name in ("next_session", "evidence_date", "prior20_asof"):
            if getattr(self, name) is not None:
                _date(getattr(self, name), name)
        for name in ("calendar_verified", "market_open", "corporate_actions_processed"):
            if getattr(self, name) is not None and type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean or unknown")
        for name in (
            "raw_close_fen",
            "low_fen",
            "high_fen",
            "down_limit_fen",
            "up_limit_fen",
        ):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name, 1)
        for name in ("prior20_amount_fen", "session_amount_fen", "session_volume_shares"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        if self.prior20_sessions is not None:
            _integer(self.prior20_sessions, "prior20_sessions")
        _rate(self.participation, "participation", positive=True)
        if self.rules is not None and type(self.rules) is not ResearchQuantityRules:
            raise ValueError("unsupported research rule object")
        if self.fees is not None and type(self.fees) is not ResearchFeeScenario:
            raise ValueError("unsupported research fee object")


@dataclass(frozen=True)
class ResearchTransition:
    book: ResearchBook
    status: str
    reason: str
    simulated_quantity: int = 0
    modeled_price_fen: int | None = None
    simulated_notional_fen: int = 0
    commission_fen: int = 0
    stamp_fen: int = 0
    additional_fee_fen: int = 0
    scope: str = field(default="hypothetical_research_transition_only", init=False)
    execution_authority: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)

    @property
    def modeled_fee_fen(self) -> int:
        return self.commission_fen + self.stamp_fen + self.additional_fee_fen


def _missing_reason(session: ResearchSession, order: ResearchOrder) -> str | None:
    for name in ("calendar_verified", "market_open", "corporate_actions_processed"):
        if getattr(session, name) is not True:
            return f"{name}_unknown" if getattr(session, name) is None else f"{name}_false"
    required = (
        "next_session",
        "evidence_date",
        "raw_close_fen",
        "low_fen",
        "high_fen",
        "down_limit_fen",
        "up_limit_fen",
        "prior20_amount_fen",
        "prior20_asof",
        "prior20_sessions",
        "session_amount_fen",
        "session_volume_shares",
        "participation",
        "rules",
        "fees",
    )
    for name in required:
        if getattr(session, name) is None:
            return f"{name}_unknown"
    if session.evidence_date != session.execution_date:
        return "evidence_date_mismatch"
    if session.next_session <= session.execution_date:
        raise ValueError("next session must follow execution")
    if session.prior20_asof > order.signal_date:
        raise ValueError("capacity input is future information relative to signal")
    if session.prior20_sessions != 20:
        return "prior20_coverage_incomplete"
    for name in ("rules", "fees"):
        scoped = getattr(session, name)
        if not scoped.effective_from <= session.execution_date <= scoped.effective_through:
            return f"{name}_outside_scope"
    if not session.fees.complete:
        return "fee_components_unknown"
    if not (
        session.down_limit_fen
        <= session.low_fen
        <= session.raw_close_fen
        <= session.high_fen
        <= session.up_limit_fen
    ):
        raise ValueError("inconsistent raw bar or limit bounds")
    return None


def _grid(quantity: int, minimum: int, increment: int) -> int:
    return 0 if quantity < minimum else minimum + (quantity - minimum) // increment * increment


def _fees(notional: int, side: str, scenario: ResearchFeeScenario) -> tuple[int, int, int]:
    _integer(notional, "modeled notional", 1)
    with localcontext() as context:
        context.prec = 60
        commission = max(
            scenario.minimum_commission_fen,
            int((notional * scenario.commission_rate).to_integral_value(rounding=ROUND_HALF_UP)),
        )
        stamp_rate = scenario.buy_stamp_rate if side == "buy" else scenario.sell_stamp_rate
        stamp = int((notional * stamp_rate).to_integral_value(rounding=ROUND_HALF_UP))
        extra = scenario.additional_fee_fixed_fen + int(
            (notional * scenario.additional_fee_rate).to_integral_value(rounding=ROUND_HALF_UP)
        )
    return commission, stamp, extra


def simulate_research_order(
    book: ResearchBook,
    order: ResearchOrder,
    session: ResearchSession,
) -> ResearchTransition:
    """Apply at most one modeled order aggregate, preserving blocked/locked shares.

    The returned state is only a scenario ledger. Realized daily volume is used at
    simulated execution, never to create a prior signal. No historical data loaded.
    """
    if not order.signal_date < session.execution_date or book.asof_date > session.execution_date:
        raise ValueError("signal/execution/book chronology invalid")
    if session.instrument_id != order.instrument_id:
        raise ValueError("session evidence belongs to another instrument")
    if order.order_id in book.simulated_order_ids:
        raise ValueError("duplicate simulated order id")
    reason = _missing_reason(session, order)
    if reason:
        return ResearchTransition(book, "blocked", reason)
    rules, fees = session.rules, session.fees
    if order.desired_quantity > rules.max_order_quantity:
        raise ValueError("desired quantity exceeds single order maximum; no automatic splitting")
    buy = order.side == "buy"
    if (buy and session.raw_close_fen >= session.up_limit_fen) or (
        not buy and session.raw_close_fen <= session.down_limit_fen
    ):
        return ResearchTransition(book, "blocked", "directional_close_limit")
    with localcontext() as context:
        context.prec = 60
        multiplier = 1 + fees.adverse_slippage_rate if buy else 1 - fees.adverse_slippage_rate
        price = int(
            (session.raw_close_fen * multiplier).to_integral_value(
                rounding=ROUND_CEILING if buy else ROUND_FLOOR
            )
        )
        amount_cap = int(
            min(session.prior20_amount_fen, session.session_amount_fen) * session.participation
        )
        volume_cap = int(session.session_volume_shares * session.participation)
    if not session.low_fen <= price <= session.high_fen or not (
        session.down_limit_fen <= price <= session.up_limit_fen
    ):
        return ResearchTransition(book, "blocked", "modeled_price_outside_bar_or_limits")
    used = next(
        (
            x
            for x in book.capacity_used
            if x.instrument_id == order.instrument_id and x.trade_date == session.execution_date
        ),
        None,
    )
    session_fingerprint = hashlib.sha256(
        json.dumps(
            asdict(session),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    if used and used.session_fingerprint != session_fingerprint:
        raise ValueError("modeled session assumptions changed after capacity was used")
    used_qty, used_amount = (used.quantity, used.notional_fen) if used else (0, 0)
    capacity = min(max(0, amount_cap - used_amount) // price, max(0, volume_cap - used_qty))
    if not capacity:
        return ResearchTransition(book, "blocked", "capacity_exhausted")
    held = [x for x in book.lots if x.instrument_id == order.instrument_id]
    total = sum(x.quantity for x in held)
    eligible = sorted(
        (x for x in held if x.sellable_on <= session.execution_date),
        key=lambda x: (x.acquired_on, x.lot_id),
    )
    sellable = sum(x.quantity for x in eligible)
    maximum = min(order.desired_quantity, capacity)
    if buy:
        maximum = _grid(maximum, rules.buy_minimum, rules.buy_increment)
        if maximum:
            low, high, affordable = 0, (maximum - rules.buy_minimum) // rules.buy_increment, 0
            while low <= high:
                mid = (low + high) // 2
                candidate = rules.buy_minimum + mid * rules.buy_increment
                cost = candidate * price + sum(_fees(candidate * price, order.side, fees))
                if cost <= book.cash_fen:
                    affordable, low = candidate, mid + 1
                else:
                    high = mid - 1
            quantity = affordable
        else:
            quantity = 0
    else:
        if not total:
            return ResearchTransition(book, "blocked", "no_position")
        if not sellable:
            return ResearchTransition(book, "blocked", "t1_locked")
        maximum = min(maximum, sellable)
        quantity = (
            total
            if rules.full_position_odd_exit and maximum == total
            else _grid(maximum, rules.sell_minimum, rules.sell_increment)
        )
    if not quantity:
        return ResearchTransition(book, "blocked", "cash_or_quantity_grid")
    notional = quantity * price
    commission, stamp, extra = _fees(notional, order.side, fees)
    cash = book.cash_fen + (-notional if buy else notional) - commission - stamp - extra
    if cash < 0:
        return ResearchTransition(book, "blocked", "sell_proceeds_cannot_cover_modeled_fees")
    if buy:
        lots = (
            *book.lots,
            ResearchLot(
                order.order_id,
                order.instrument_id,
                quantity,
                session.execution_date,
                session.next_session,
            ),
        )
    else:
        remaining, sold = quantity, {}
        for lot in eligible:
            take = min(remaining, lot.quantity)
            sold[lot.lot_id] = take
            remaining -= take
        assert remaining == 0
        lots = tuple(
            replace(lot, quantity=lot.quantity - sold.get(lot.lot_id, 0))
            for lot in book.lots
            if lot.quantity > sold.get(lot.lot_id, 0)
        )
    usage = tuple(x for x in book.capacity_used if x is not used) + (
        CapacityUsed(
            order.instrument_id,
            session.execution_date,
            used_qty + quantity,
            used_amount + notional,
            session_fingerprint,
        ),
    )
    new_book = ResearchBook(
        session.execution_date, cash, lots, usage, book.simulated_order_ids | {order.order_id}
    )
    reason = (
        "complete" if quantity == order.desired_quantity else "partial_under_declared_constraints"
    )
    return ResearchTransition(
        new_book, "simulated", reason, quantity, price, notional, commission, stamp, extra
    )
