"""Daily closed loop joining the market risk layer to the research ledger.

Each session executes yesterday's intents through the existing quantity kernel,
marks the completed book with raw closes, decides the risk cap from that
auditable fee-inclusive equity, scales the still-valid base target, and diffs
the next session's order intents. A session commits atomically — trades, risk
state and next intents together — and any necessary unknown inside the session
stops the path with the previous checkpoint, the failure date and the reason;
nothing advances first and gets relabelled complete. Nothing here certifies
data, issues real orders or claims returns.
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
    STATUS_OK,
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

_D_BEARING_RULES = ("D", "VMD")
_V_BEARING_RULES = ("V", "VM", "VMD")
_M_BEARING_RULES = ("M", "VM", "VMD")


def _day(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")


def _typed_tuple(values: object, kind: type, name: str) -> None:
    if type(values) is not tuple or any(type(x) is not kind for x in values):
        raise ValueError(f"{name} must be an immutable tuple of {kind.__name__}")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{name} must be a nonempty identifier")


@dataclass(frozen=True)
class LedgerSessionEvidence:
    """Caller-declared execution evidence for one session, sans orders.

    ``corporate_processing_complete`` may be None: that explicit unknown stops
    the session through the scheduler's existing preflight reason instead of
    being refused at construction.
    """

    session: date
    contexts: tuple[ResearchSession, ...]
    marks: tuple[RawCloseMark, ...]
    corporate_processing_complete: bool | None

    def __post_init__(self) -> None:
        _day(self.session, "evidence session")
        _typed_tuple(self.contexts, ResearchSession, "contexts")
        _typed_tuple(self.marks, RawCloseMark, "marks")
        if self.corporate_processing_complete is not None and type(
            self.corporate_processing_complete
        ) is not bool:
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
    run_id: str
    nav_series_id: str
    nav_source: str
    generation_rules: ResearchQuantityRules
    return_source: str = ""
    index_source: str = ""

    def __post_init__(self) -> None:
        if self.rule_id not in RULE_IDS:
            raise ValueError(f"unknown market risk rule {self.rule_id!r}")
        for name in ("run_id", "nav_series_id", "nav_source"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty identifier")
        if self.rule_id in _V_BEARING_RULES and not self.return_source:
            raise ValueError("return_source is required for V-bearing rules")
        if self.rule_id in _M_BEARING_RULES and not self.index_source:
            raise ValueError("index_source is required for M-bearing rules")
        if type(self.generation_rules) is not ResearchQuantityRules:
            raise ValueError("generation_rules must be a declared quantity scenario")


@dataclass(frozen=True)
class RiskLedgerCheckpoint:
    """Atomic resume point: book, pending intents, risk state, identity.

    ``pending_orders`` are the intents already decided on ``book.asof_date``
    and awaiting execution on the next common session, so a 2021-12-31 signal
    still executes on 2022-01-04. ``initial_cash_fen`` is the original starting
    capital — a resume reports it unchanged, never the residual cash.
    """

    book: ResearchBook
    pending_orders: tuple[ResearchOrder, ...]
    drawdown_state: RiskState | None
    attempted_order_ids: tuple[str, ...]
    initial_cash_fen: int
    run_id: str

    def __post_init__(self) -> None:
        if type(self.book) is not ResearchBook:
            raise ValueError("checkpoint book must be a ResearchBook")
        _typed_tuple(self.pending_orders, ResearchOrder, "pending orders")
        if len({(x.instrument_id, x.side) for x in self.pending_orders}) != len(
            self.pending_orders
        ):
            raise ValueError("pending orders must be unique per instrument and side")
        for order in self.pending_orders:
            if order.signal_date != self.book.asof_date:
                raise ValueError("pending orders must be signed on the checkpoint session")
        if self.drawdown_state is not None and not isinstance(
            self.drawdown_state, RiskState
        ):
            raise ValueError("drawdown_state must be a RiskState")
        _typed_tuple(self.attempted_order_ids, str, "attempted order ids")
        if len(set(self.attempted_order_ids)) != len(self.attempted_order_ids):
            raise ValueError("duplicate attempted order id")
        if type(self.initial_cash_fen) is not int or self.initial_cash_fen < 0:
            raise ValueError("initial cash must be a nonnegative integer")
        _identifier(self.run_id, "run_id")

    @classmethod
    def start(
        cls,
        *,
        signal_date: date,
        initial_cash_fen: int,
        run_id: str,
        pending_orders: tuple[ResearchOrder, ...] = (),
        drawdown_state: RiskState | None = None,
    ) -> RiskLedgerCheckpoint:
        """Fresh start: flat book on the pre-start decision session."""
        return cls(
            book=ResearchBook(asof_date=signal_date, cash_fen=initial_cash_fen),
            pending_orders=pending_orders,
            drawdown_state=drawdown_state,
            attempted_order_ids=(),
            initial_cash_fen=initial_cash_fen,
            run_id=run_id,
        )


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
    risk_target: TargetPortfolio
    signal_as_of: date
    next_intents: tuple[ResearchOrder, ...]

    @property
    def actual_gross_exposure(self) -> Decimal:
        """Realized stock exposure of the completed book, not a target claim."""
        if self.marked_equity_fen <= 0:
            return Decimal(0)
        return Decimal(self.position_value_fen) / Decimal(self.marked_equity_fen)

    @property
    def risk_target_gross_exposure(self) -> float:
        return self.risk_target.gross_exposure


@dataclass(frozen=True)
class RiskLedgerLoopResult:
    status: str
    rule_id: str
    initial_cash_fen: int
    requested_end: date
    book: ResearchBook
    records: tuple[RiskLedgerDayRecord, ...]
    checkpoint: RiskLedgerCheckpoint
    stopped_on: date | None
    stop_reason: str | None
    scope: str = field(default="hypothetical_risk_ledger_scenario_only", init=False)
    execution_authority: bool = field(default=False, init=False)
    historical_data_certified: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)

    @property
    def valid_through(self) -> date:
        return self.book.asof_date

    @property
    def drawdown_state(self) -> RiskState | None:
        return self.checkpoint.drawdown_state


def _slice_through(observations: tuple, decision_date: date) -> tuple:
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
) -> tuple[ResearchOrder, ...]:
    """Synthesize one net intent per instrument/side from target vs holdings.

    The weight-to-budget contract is exact: ``Decimal(str(weight))`` times the
    integer-fen equity, floor-divided by the raw mark price — no float
    multiplication, no epsilon, no price rounding — before the declared grid
    shapes the order. Sells are ordered by instrument, buys by descending
    target value so the most underweight name competes first for the cash that
    simulated sales actually free. Instruments the base strategy no longer
    targets exit in full, merging strategy expiry with risk scaling.
    """
    held: dict[str, int] = {}
    for lot in book.lots:
        held[lot.instrument_id] = held.get(lot.instrument_id, 0) + lot.quantity
    sells: list[ResearchOrder] = []
    buys: list[tuple[Decimal, str, int]] = []
    for position in risk_target.positions:
        code = position.instrument_id
        price = marks[code]
        budget = Decimal(str(position.target_weight)) * Decimal(equity_fen)
        target_quantity = int(budget // Decimal(price))
        difference = target_quantity - held.get(code, 0)
        if difference > 0:
            desired = min(
                _grid_floor(difference, rules.buy_minimum, rules.buy_increment),
                rules.max_order_quantity,
            )
            if desired:
                buys.append((Decimal(desired) * Decimal(price), code, desired))
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
    checkpoint: RiskLedgerCheckpoint,
    calendar: tuple[date, ...],
    requested_end: date,
    base_targets: Mapping[date, TargetPortfolio],
    evidence: Mapping[date, LedgerSessionEvidence],
    config: RiskLedgerConfig,
    unscaled_risk_returns: tuple[UnscaledReturn, ...] = (),
    index_closes: tuple[IndexClose, ...] = (),
) -> RiskLedgerLoopResult:
    """Drive the daily closed loop over ``calendar[first:last]``.

    Starts from ``checkpoint`` (fresh via :meth:`RiskLedgerCheckpoint.start`,
    or the last committed checkpoint of an interrupted run) and exposes the
    last committed checkpoint on the result for the next resume. A session
    commits only when every necessary fact was known; otherwise the path stops
    with the previous checkpoint, the failure date and the specific reason.
    """
    if config.run_id != checkpoint.run_id:
        raise ValueError(
            f"checkpoint run {checkpoint.run_id!r} does not match config run "
            f"{config.run_id!r}"
        )
    _typed_tuple(calendar, date, "calendar")
    if len(calendar) < 3 or tuple(sorted(set(calendar))) != calendar:
        raise ValueError("calendar must contain at least three unique ordered sessions")
    _day(requested_end, "requested end")
    if checkpoint.book.asof_date not in calendar or requested_end not in calendar:
        raise ValueError("checkpoint/end dates must lie in the supplied calendar")
    first = calendar.index(checkpoint.book.asof_date) + 1
    last = calendar.index(requested_end)
    if first > last or last + 1 >= len(calendar):
        raise ValueError("interval requires decision and following-session calendar padding")
    if config.rule_id in _D_BEARING_RULES and checkpoint.drawdown_state is None:
        raise ValueError("D-bearing rules require an explicit initial or persisted RiskState")
    if (
        checkpoint.drawdown_state is not None
        and checkpoint.drawdown_state.series_id != config.nav_series_id
    ):
        raise ValueError("drawdown state belongs to another NAV series")

    book = checkpoint.book
    pending = checkpoint.pending_orders
    state = checkpoint.drawdown_state
    attempted = set(checkpoint.attempted_order_ids)
    records: list[RiskLedgerDayRecord] = []

    def stopped(session: date, reason: str) -> RiskLedgerLoopResult:
        # The failed session never committed: the book, state, pending intents
        # and attempted identities are the previous complete checkpoint's.
        return RiskLedgerLoopResult(
            "stopped",
            config.rule_id,
            checkpoint.initial_cash_fen,
            requested_end,
            book,
            tuple(records),
            RiskLedgerCheckpoint(
                book=book,
                pending_orders=pending,
                drawdown_state=state,
                attempted_order_ids=tuple(sorted(attempted)),
                initial_cash_fen=checkpoint.initial_cash_fen,
                run_id=checkpoint.run_id,
            ),
            session,
            reason,
        )

    for i in range(first, last + 1):
        session = calendar[i]
        day_evidence = evidence.get(session)
        if day_evidence is None:
            # Discovered per day so the completed prefix survives the stop.
            return stopped(session, "session_evidence_missing")
        base_target = base_targets.get(session)
        if base_target is None:
            return stopped(session, "base_target_missing")
        batch = ResearchDay(
            session=session,
            orders=pending,
            contexts=day_evidence.contexts,
            marks=day_evidence.marks,
            corporate_processing_complete=day_evidence.corporate_processing_complete,
        )
        advance = advance_research_day(book, calendar, i, batch, attempted)
        if advance.status != "advanced":
            return stopped(session, advance.reason)
        assert advance.record is not None
        completed = advance.record
        equity_fen = completed.marked_equity_fen

        risk_inputs = _risk_inputs_for_session(
            session, calendar, config, equity_fen, state, unscaled_risk_returns, index_closes
        )
        decision = evaluate_market_risk_rule(config.rule_id, risk_inputs)
        if decision.status != STATUS_OK:
            return stopped(session, "risk_unknown:" + ";".join(decision.reasons))
        adapted = apply_risk_cap(base_target, decision)
        if adapted.status != RESULT_READY or adapted.target is None:
            return stopped(session, f"risk_unknown:{adapted.reason}")
        risk_target = adapted.target
        marks_map = {x.instrument_id: x.price_fen for x in day_evidence.marks}
        missing_marks = sorted(
            {
                position.instrument_id
                for position in risk_target.positions
                if position.instrument_id not in marks_map
            }
        )
        if missing_marks:
            return stopped(session, "mark_missing:" + ",".join(missing_marks))
        if equity_fen <= 0:
            return stopped(session, "nonpositive_equity")
        intents = _diff_next_intents(
            completed.book,
            risk_target,
            equity_fen,
            marks_map,
            config.generation_rules,
            session,
        )

        records.append(
            RiskLedgerDayRecord(
                session=session,
                book=completed.book,
                position_value_fen=completed.position_value_fen,
                marked_equity_fen=equity_fen,
                modeled_fees_fen=completed.modeled_fees_fen,
                attempts=completed.attempts,
                base_target=base_target,
                decision=decision,
                risk_target=risk_target,
                signal_as_of=adapted.signal_as_of,
                next_intents=intents,
            )
        )
        book = completed.book
        pending = intents
        if decision.drawdown_state is not None:
            state = decision.drawdown_state
    return RiskLedgerLoopResult(
        "completed_scenario",
        config.rule_id,
        checkpoint.initial_cash_fen,
        requested_end,
        book,
        tuple(records),
        RiskLedgerCheckpoint(
            book=book,
            pending_orders=pending,
            drawdown_state=state,
            attempted_order_ids=tuple(sorted(attempted)),
            initial_cash_fen=checkpoint.initial_cash_fen,
            run_id=checkpoint.run_id,
        ),
        None,
        None,
    )
