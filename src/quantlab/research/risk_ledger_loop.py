"""Daily closed loop joining the market risk layer to the research ledger.

Each session executes yesterday's intents through the existing quantity kernel,
marks the completed book with raw closes, decides the risk cap from that
auditable fee-inclusive equity, scales the still-valid base target, and diffs
the next session's order intents. Nothing here certifies data, issues real
orders or claims returns; unknown evidence keeps the existing stop semantics
and retains the last complete day.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from quantlab.portfolio.models import TargetPortfolio
from quantlab.research.market_risk import (
    APPROVED_EQUITY_PROXY_INDEX_ID,
    RULE_IDS,
    IndexClose,
    RiskDecision,
    RiskInputs,
    RiskState,
    StrategyNav,
    UnscaledReturn,
    evaluate_market_risk_rule,
)
from quantlab.research.quantity_kernel import (
    ResearchBook,
    ResearchOrder,
    ResearchQuantityRules,
    ResearchSession,
)
from quantlab.research.quantity_scheduler import (
    RawCloseMark,
    ResearchDay,
    ScheduledAttempt,
    advance_research_day,
)
from quantlab.research.risk_target_adapter import RESULT_READY, apply_risk_cap

DAY_DECIDED = "decided"
DAY_RISK_UNKNOWN = "risk_unknown"

_D_BEARING_RULES = ("D", "VMD")
_V_BEARING_RULES = ("V", "VM", "VMD")
_M_BEARING_RULES = ("M", "VM", "VMD")


def _day(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")


def _typed_tuple(values: object, kind: type, name: str) -> None:
    if type(values) is not tuple or any(type(x) is not kind for x in values):
        raise ValueError(f"{name} must be an immutable tuple of {kind.__name__}")


@dataclass(frozen=True)
class LedgerSessionEvidence:
    """Caller-declared execution evidence for one session, sans orders."""

    session: date
    contexts: tuple[ResearchSession, ...]
    marks: tuple[RawCloseMark, ...]
    corporate_processing_complete: bool | None

    def __post_init__(self) -> None:
        _day(self.session, "evidence session")
        _typed_tuple(self.contexts, ResearchSession, "contexts")
        _typed_tuple(self.marks, RawCloseMark, "marks")
        if type(self.corporate_processing_complete) is not bool:
            raise ValueError("corporate processing must be boolean or explicit unknown")
        for name in ("contexts", "marks"):
            values = getattr(self, name)
            if len({x.instrument_id for x in values}) != len(values):
                raise ValueError(f"duplicate instrument in {name}")
        if any(x.execution_date != self.session for x in self.contexts):
            raise ValueError("context belongs to a different session")
        if any(x.session != self.session for x in self.marks):
            raise ValueError("mark belongs to a different session")


@dataclass(frozen=True)
class RiskLedgerConfig:
    """Loop configuration; rule parameters stay frozen in the risk layer."""

    rule_id: str
    nav_series_id: str
    nav_source: str
    generation_rules: ResearchQuantityRules
    return_source: str = ""
    index_source: str = ""

    def __post_init__(self) -> None:
        if self.rule_id not in RULE_IDS:
            raise ValueError(f"unknown market risk rule {self.rule_id!r}")
        for name in ("nav_series_id", "nav_source"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty identifier")
        if self.rule_id in _V_BEARING_RULES and not self.return_source:
            raise ValueError("return_source is required for V-bearing rules")
        if self.rule_id in _M_BEARING_RULES and not self.index_source:
            raise ValueError("index_source is required for M-bearing rules")
        if type(self.generation_rules) is not ResearchQuantityRules:
            raise ValueError("generation_rules must be a declared quantity scenario")


@dataclass(frozen=True)
class RiskLedgerDayRecord:
    """One committed session: ledger, decision, targets and next intents."""

    session: date
    book: ResearchBook
    position_value_fen: int
    marked_equity_fen: int
    modeled_fees_fen: int
    attempts: tuple[ScheduledAttempt, ...]
    base_target: TargetPortfolio
    decision: RiskDecision
    risk_target: TargetPortfolio | None
    signal_as_of: date | None
    next_intents: tuple[ResearchOrder, ...]
    no_intent_reasons: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        if self.status not in (DAY_DECIDED, DAY_RISK_UNKNOWN):
            raise ValueError("day status must be decided or risk_unknown")
        if self.status == DAY_DECIDED:
            if not isinstance(self.risk_target, TargetPortfolio):
                raise ValueError("a decided day must carry a risk target")
            if self.signal_as_of is None:
                raise ValueError("a decided day must record signal_as_of")
        elif self.risk_target is not None:
            raise ValueError("a risk-unknown day must not carry a risk target")

    @property
    def actual_gross_exposure(self) -> Decimal:
        """Realized stock exposure of the completed book, not a target claim."""
        if self.marked_equity_fen <= 0:
            return Decimal(0)
        return Decimal(self.position_value_fen) / Decimal(self.marked_equity_fen)

    @property
    def risk_target_gross_exposure(self) -> float | None:
        if self.risk_target is None:
            return None
        return self.risk_target.gross_exposure


@dataclass(frozen=True)
class RiskLedgerLoopResult:
    status: str
    rule_id: str
    initial_cash_fen: int
    requested_end: date
    book: ResearchBook
    records: tuple[RiskLedgerDayRecord, ...]
    stopped_on: date | None
    stop_reason: str | None
    drawdown_state: RiskState | None
    scope: str = field(default="hypothetical_risk_ledger_scenario_only", init=False)
    execution_authority: bool = field(default=False, init=False)
    historical_data_certified: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)

    @property
    def valid_through(self) -> date:
        return self.book.asof_date


def _slice_through(
    observations: tuple, decision_date: date
) -> tuple:
    return tuple(x for x in observations if x.session <= decision_date)


def _order_id(signal_date: date, side: str, instrument_id: str) -> str:
    return f"{signal_date.isoformat()}|{side}|{instrument_id}"


def _grid_floor(quantity: int, minimum: int, increment: int) -> int:
    return 0 if quantity < minimum else minimum + (quantity - minimum) // increment * increment


def _diff_next_intents(
    book: ResearchBook,
    risk_target: TargetPortfolio,
    equity_fen: int,
    marks: Mapping[str, int],
    rules: ResearchQuantityRules,
    signal_date: date,
    no_intent_reasons: list[str],
) -> tuple[ResearchOrder, ...]:
    """Synthesize one net intent per instrument/side from target vs holdings.

    Sizing uses the declared generation scenario only to shape quantity grids
    and the single-order maximum; execution constraints remain the kernel's
    authority on the next session. Sells are ordered by instrument, buys by
    descending target value so the most underweight name competes first for
    the cash that simulated sales actually free.
    """
    if equity_fen <= 0:
        no_intent_reasons.append("nonpositive_equity")
        return ()
    held: dict[str, int] = {}
    for lot in book.lots:
        held[lot.instrument_id] = held.get(lot.instrument_id, 0) + lot.quantity
    sells: list[ResearchOrder] = []
    buys: list[tuple[int, str, int]] = []
    for position in risk_target.positions:
        code = position.instrument_id
        price = marks.get(code)
        if price is None:
            no_intent_reasons.append(f"mark_missing:{code}")
            continue
        target_quantity = int(position.target_weight * equity_fen) // price
        difference = target_quantity - held.get(code, 0)
        if difference > 0:
            desired = min(
                _grid_floor(difference, rules.buy_minimum, rules.buy_increment),
                rules.max_order_quantity,
            )
            if desired:
                buys.append((desired * price, code, desired))
        elif difference < 0:
            desired = min(
                _grid_floor(-difference, rules.sell_minimum, rules.sell_increment),
                rules.max_order_quantity,
            )
            if desired:
                sells.append(
                    ResearchOrder(
                        _order_id(signal_date, "sell", code), code, "sell", desired, signal_date
                    )
                )
    # Instruments the base strategy no longer targets must exit in full; the
    # strategy's expiry and any risk scaling merge into this single intent.
    for code in sorted(set(held) - {p.instrument_id for p in risk_target.positions}):
        desired = min(held[code], rules.max_order_quantity)
        if desired:
            sells.append(
                ResearchOrder(
                    _order_id(signal_date, "sell", code), code, "sell", desired, signal_date
                )
            )
    buys.sort(key=lambda item: (-item[0], item[1]))
    return (
        *sells,
        *(
            ResearchOrder(_order_id(signal_date, "buy", code), code, "buy", desired, signal_date)
            for _, code, desired in buys
        ),
    )


def _risk_inputs_for_session(
    session: date,
    calendar: tuple[date, ...],
    config: RiskLedgerConfig,
    equity_fen: int,
    state: RiskState | None,
    unscaled_risk_returns: tuple[UnscaledReturn, ...],
    index_closes: tuple[IndexClose, ...],
) -> RiskInputs:
    nav: StrategyNav | None = None
    if config.rule_id in _D_BEARING_RULES and equity_fen > 0:
        # Integer fen as an exact Decimal keeps drawdown ratios scale-free.
        nav = StrategyNav(as_of=session, nav=Decimal(equity_fen))
    return RiskInputs(
        decision_date=session,
        sessions=calendar,
        unscaled_risk_returns=(
            _slice_through(unscaled_risk_returns, session)
            if config.rule_id in _V_BEARING_RULES
            else ()
        ),
        return_source=config.return_source if config.rule_id in _V_BEARING_RULES else "",
        index_closes=(
            _slice_through(index_closes, session)
            if config.rule_id in _M_BEARING_RULES
            else ()
        ),
        index_id=APPROVED_EQUITY_PROXY_INDEX_ID if config.rule_id in _M_BEARING_RULES else "",
        index_source=config.index_source if config.rule_id in _M_BEARING_RULES else "",
        strategy_nav=nav,
        drawdown_state=state,
        nav_source=config.nav_source if nav is not None else "",
        nav_series_id=config.nav_series_id if nav is not None or state is not None else "",
    )


def run_risk_ledger_loop(
    *,
    initial_book: ResearchBook,
    calendar: tuple[date, ...],
    requested_end: date,
    base_targets: Mapping[date, TargetPortfolio],
    evidence: Mapping[date, LedgerSessionEvidence],
    config: RiskLedgerConfig,
    drawdown_state: RiskState | None = None,
    unscaled_risk_returns: tuple[UnscaledReturn, ...] = (),
    index_closes: tuple[IndexClose, ...] = (),
) -> RiskLedgerLoopResult:
    """Drive the daily closed loop over ``calendar[first:last]``.

    The loop resumes naturally from ``initial_book`` (fresh-flat for a new
    path, or the last complete book plus its persisted drawdown state after an
    interruption). Every session commits atomically as one day record; a stop
    returns the last complete book and no suffix.
    """
    if type(initial_book) is not ResearchBook:
        raise ValueError("an explicit research book is required")
    _typed_tuple(calendar, date, "calendar")
    if len(calendar) < 3 or tuple(sorted(set(calendar))) != calendar:
        raise ValueError("calendar must contain at least three unique ordered sessions")
    _day(requested_end, "requested end")
    if initial_book.asof_date not in calendar or requested_end not in calendar:
        raise ValueError("initial/end dates must lie in the supplied calendar")
    first = calendar.index(initial_book.asof_date) + 1
    last = calendar.index(requested_end)
    if first > last or last + 1 >= len(calendar):
        raise ValueError("interval requires decision and following-session calendar padding")
    if config.rule_id in _D_BEARING_RULES and drawdown_state is None:
        raise ValueError(
            "D-bearing rules require an explicit initial or persisted RiskState"
        )
    if drawdown_state is not None and drawdown_state.series_id != config.nav_series_id:
        raise ValueError("drawdown state belongs to another NAV series")
    sessions = tuple(calendar[first : last + 1])
    for session in sessions:
        if session not in base_targets:
            raise ValueError(f"base target missing for session {session}")
        if session not in evidence:
            raise ValueError(f"session evidence missing for {session}")
    book, records, attempted = initial_book, [], set()
    state = drawdown_state
    pending: tuple[ResearchOrder, ...] = ()
    for i in range(first, last + 1):
        session = calendar[i]
        day_evidence = evidence[session]
        batch = ResearchDay(
            session=session,
            orders=pending,
            contexts=day_evidence.contexts,
            marks=day_evidence.marks,
            corporate_processing_complete=day_evidence.corporate_processing_complete,
        )
        advance = advance_research_day(book, calendar, i, batch, attempted)
        if advance.status != "advanced":
            return RiskLedgerLoopResult(
                "stopped",
                config.rule_id,
                initial_book.cash_fen,
                requested_end,
                book,
                tuple(records),
                session,
                advance.reason,
                state,
            )
        assert advance.record is not None
        completed = advance.record
        book = completed.book
        equity_fen = completed.marked_equity_fen

        risk_inputs = _risk_inputs_for_session(
            session, calendar, config, equity_fen, state, unscaled_risk_returns, index_closes
        )
        decision = evaluate_market_risk_rule(config.rule_id, risk_inputs)
        if decision.drawdown_state is not None:
            state = decision.drawdown_state

        base_target = base_targets[session]
        adapted = apply_risk_cap(base_target, decision)
        no_intent_reasons: list[str] = []
        if adapted.status != RESULT_READY or adapted.target is None:
            # Unknown risk is neither an all-cash target nor a stop: holdings
            # are kept, no new intents are issued, and the session is recorded.
            risk_target, signal_as_of, intents, day_status = None, None, (), DAY_RISK_UNKNOWN
            if adapted.reason:
                no_intent_reasons.append(f"risk_unknown:{adapted.reason}")
        else:
            risk_target = adapted.target
            signal_as_of = adapted.signal_as_of
            marks_map = {x.instrument_id: x.price_fen for x in day_evidence.marks}
            intents = _diff_next_intents(
                book,
                risk_target,
                equity_fen,
                marks_map,
                config.generation_rules,
                session,
                no_intent_reasons,
            )
            day_status = DAY_DECIDED

        records.append(
            RiskLedgerDayRecord(
                session=session,
                book=book,
                position_value_fen=completed.position_value_fen,
                marked_equity_fen=equity_fen,
                modeled_fees_fen=completed.modeled_fees_fen,
                attempts=completed.attempts,
                base_target=base_target,
                decision=decision,
                risk_target=risk_target,
                signal_as_of=signal_as_of,
                next_intents=intents,
                no_intent_reasons=tuple(no_intent_reasons),
                status=day_status,
            )
        )
        pending = intents
    return RiskLedgerLoopResult(
        "completed_scenario",
        config.rule_id,
        initial_book.cash_fen,
        requested_end,
        book,
        tuple(records),
        None,
        None,
        state,
    )
