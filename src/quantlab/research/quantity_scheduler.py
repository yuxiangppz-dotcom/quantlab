"""Chronological, hypothetical stock ledger; no historical input certification.

Raw marks and complete corporate-processing declarations are scenario inputs.
Unknown necessary evidence stops the whole path before that day's mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from quantlab.research.quantity_kernel import (
    ResearchBook,
    ResearchOrder,
    ResearchSession,
    ResearchTransition,
    _missing_reason,
    simulate_research_order,
)


def _day(value, name):
    if type(value) is not date:
        raise ValueError(f"{name} must be an explicit date")


def _typed_tuple(values, kind, name):
    if type(values) is not tuple or any(type(x) is not kind for x in values):
        raise ValueError(f"{name} must be an immutable typed tuple")


def _calendar(sessions):
    _typed_tuple(sessions, date, "calendar")
    if len(sessions) < 3 or tuple(sorted(set(sessions))) != sessions:
        raise ValueError("calendar must contain at least three unique ordered sessions")


@dataclass(frozen=True)
class RawCloseMark:
    instrument_id: str
    session: date
    price_fen: int

    def __post_init__(self):
        if (
            type(self.instrument_id) is not str
            or not self.instrument_id
            or self.instrument_id.strip() != self.instrument_id
        ):
            raise ValueError("mark instrument must be a nonempty identifier")
        _day(self.session, "mark session")
        if type(self.price_fen) is not int or not 0 < self.price_fen <= 10**15:
            raise ValueError("raw mark must be positive integer fen")


@dataclass(frozen=True)
class ResearchDay:
    session: date
    orders: tuple[ResearchOrder, ...]
    contexts: tuple[ResearchSession, ...]
    marks: tuple[RawCloseMark, ...]
    corporate_processing_complete: bool | None

    def __post_init__(self):
        _day(self.session, "batch session")
        _typed_tuple(self.orders, ResearchOrder, "orders")
        _typed_tuple(self.contexts, ResearchSession, "contexts")
        _typed_tuple(self.marks, RawCloseMark, "marks")
        if (
            self.corporate_processing_complete is not None
            and type(self.corporate_processing_complete) is not bool
        ):
            raise ValueError("corporate processing must be boolean or explicit unknown")
        if len({x.order_id for x in self.orders}) != len(self.orders):
            raise ValueError("duplicate attempt ID in day")
        if len({(x.instrument_id, x.side) for x in self.orders}) != len(self.orders):
            raise ValueError("order splitting within instrument/side/day is unsupported")
        for name in ("contexts", "marks"):
            values = getattr(self, name)
            if len({x.instrument_id for x in values}) != len(values):
                raise ValueError(f"duplicate instrument in {name}")
        if any(x.execution_date != self.session for x in self.contexts):
            raise ValueError("context belongs to a different batch date")
        if any(x.session != self.session for x in self.marks):
            raise ValueError("mark belongs to a different batch date")


@dataclass(frozen=True)
class ScheduledAttempt:
    order: ResearchOrder
    transition: ResearchTransition


@dataclass(frozen=True)
class ResearchDayRecord:
    session: date
    book: ResearchBook
    position_value_fen: int
    marked_equity_fen: int
    modeled_fees_fen: int
    attempts: tuple[ScheduledAttempt, ...]


@dataclass(frozen=True)
class ResearchScheduleResult:
    status: str
    initial_cash_fen: int
    requested_end: date
    book: ResearchBook
    records: tuple[ResearchDayRecord, ...]
    stopped_on: date | None
    stop_reason: str | None
    scope: str = field(default="hypothetical_chronological_scenario_only", init=False)
    execution_authority: bool = field(default=False, init=False)
    historical_data_certified: bool = field(default=False, init=False)
    performance_eligible: bool = field(default=False, init=False)

    @property
    def valid_through(self):
        return self.book.asof_date


def s4_target_exit_session(acquired_on: date, calendar: tuple[date, ...]) -> date | None:
    """Target five sessions after acquisition, not a promise of a completed sale."""
    _day(acquired_on, "acquisition date")
    _calendar(calendar)
    if acquired_on not in calendar:
        raise ValueError("acquisition is outside the supplied calendar")
    target = calendar.index(acquired_on) + 5
    return calendar[target] if target < len(calendar) else None


def _preflight(book, batch, previous, following, attempted):
    if batch.corporate_processing_complete is not True:
        return "corporate_processing_incomplete_or_unknown"
    marks = {x.instrument_id: x for x in batch.marks}
    contexts = {x.instrument_id: x for x in batch.contexts}
    required = {x.instrument_id for x in book.lots} | {x.instrument_id for x in batch.orders}
    for code in sorted(required):
        if code not in marks:
            return f"raw_mark_unknown:{code}"
    for order in batch.orders:
        if order.signal_date != previous:
            raise ValueError("orders require the immediately previous session decision")
        if order.order_id in attempted:
            raise ValueError("attempt ID already used, including a previously blocked attempt")
        context = contexts.get(order.instrument_id)
        if context is None:
            return f"order_context_unknown:{order.instrument_id}"
        if context.calendar_verified is not True:
            return f"calendar_context_unknown_or_false:{order.instrument_id}"
        if context.next_session != following:
            return f"next_session_mismatch:{order.instrument_id}"
        if context.evidence_date != batch.session:
            return f"context_evidence_date_mismatch:{order.instrument_id}"
        if context.corporate_actions_processed is not True:
            return f"instrument_corporate_processing_unknown_or_false:{order.instrument_id}"
        if context.raw_close_fen is not None and (
            context.raw_close_fen != marks[order.instrument_id].price_fen
        ):
            raise ValueError("execution raw close and end-of-day raw mark disagree")
        reason = _missing_reason(context, order)
        # Known suspension blocks this order. It does not create a synthetic mark
        # or override corporate processing; those were independently checked above.
        if reason is not None and reason != "market_open_false":
            return f"{reason}:{order.instrument_id}"
        if reason is None and order.desired_quantity > context.rules.max_order_quantity:
            raise ValueError("desired quantity exceeds single order maximum")
    return None


@dataclass(frozen=True)
class DayAdvance:
    """One session's outcome: either a complete record or an explicit stop."""

    status: str
    record: ResearchDayRecord | None
    reason: str | None

    def __post_init__(self) -> None:
        if self.status not in ("advanced", "stopped"):
            raise ValueError("day advance status must be advanced or stopped")
        if self.status == "advanced":
            if not isinstance(self.record, ResearchDayRecord):
                raise ValueError("an advanced day must carry a record")
            if self.reason is not None:
                raise ValueError("an advanced day carries no stop reason")
        elif self.record is not None:
            raise ValueError("a stopped day must not carry a record")


def advance_research_day(
    book: ResearchBook,
    calendar: tuple[date, ...],
    index: int,
    batch: ResearchDay,
    attempted: set[str],
) -> DayAdvance:
    """Advance one calendar session: preflight, sells-then-buys, marks, record.

    Shared single-day engine of :func:`simulate_research_schedule`. The book
    is carried forward chronologically instead of being recreated flat, so a
    daily driver can read completed state before deciding the next session's
    orders. Semantics are identical to the whole-schedule entry.
    """
    if type(index) is not int or not 1 <= index < len(calendar) - 1:
        raise ValueError("day index requires previous and following calendar padding")
    if batch.session != calendar[index]:
        raise ValueError("batch session does not match the calendar index")
    reason = _preflight(book, batch, calendar[index - 1], calendar[index + 1], attempted)
    if reason:
        return DayAdvance("stopped", None, reason)
    day = calendar[index]
    # No same-day capacity reset between attempts. Only older days are pruned.
    working = replace(book, asof_date=day, capacity_used=())
    sells = sorted(
        (x for x in batch.orders if x.side == "sell"),
        key=lambda x: (x.instrument_id, x.order_id),
    )
    buys = [x for x in batch.orders if x.side == "buy"]
    contexts = {x.instrument_id: x for x in batch.contexts}
    attempts = []
    for order in (*sells, *buys):
        transition = simulate_research_order(working, order, contexts[order.instrument_id])
        working = transition.book
        attempted.add(order.order_id)
        attempts.append(ScheduledAttempt(order, transition))
    marks = {x.instrument_id: x.price_fen for x in batch.marks}
    value = sum(lot.quantity * marks[lot.instrument_id] for lot in working.lots)
    return DayAdvance(
        "advanced",
        ResearchDayRecord(
            day,
            working,
            value,
            working.cash_fen + value,
            sum(x.transition.modeled_fee_fen for x in attempts),
            tuple(attempts),
        ),
        None,
    )


def simulate_research_schedule(
    initial_book: ResearchBook,
    calendar: tuple[date, ...],
    days: tuple[ResearchDay, ...],
    *,
    requested_end: date,
) -> ResearchScheduleResult:
    """Apply explicit day orders, with no automatic retry, targeting or liquidation.

    Sells precede buys. Buy order order is deliberately the frozen caller order.
    A stop retains the last complete book and all unsold shares; no suffix is made.
    Even a complete result remains a hypothetical, uncertified scenario ledger.
    """
    if type(initial_book) is not ResearchBook:
        raise ValueError("an explicit research book is required")
    if initial_book.lots or initial_book.capacity_used or initial_book.simulated_order_ids:
        raise ValueError("scheduler requires a fresh initially flat research book")
    _calendar(calendar)
    _typed_tuple(days, ResearchDay, "day batches")
    _day(requested_end, "requested end")
    if initial_book.asof_date not in calendar or requested_end not in calendar:
        raise ValueError("initial/end dates must lie in the supplied calendar")
    first = calendar.index(initial_book.asof_date) + 1
    last = calendar.index(requested_end)
    if first > last or last + 1 >= len(calendar):
        raise ValueError("interval requires decision and following-session calendar padding")
    dates = tuple(x.session for x in days)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("day batches must be unique and chronological")
    if any(day not in calendar[first : last + 1] for day in dates):
        raise ValueError("batch outside the requested trading sessions")
    batches = {x.session: x for x in days}
    book, records, attempted = initial_book, [], set()
    for i in range(first, last + 1):
        day = calendar[i]
        batch = batches.get(day)
        advance = (
            DayAdvance("stopped", None, "session_batch_missing")
            if batch is None
            else advance_research_day(book, calendar, i, batch, attempted)
        )
        if advance.status != "advanced":
            return ResearchScheduleResult(
                "stopped",
                initial_book.cash_fen,
                requested_end,
                book,
                tuple(records),
                day,
                advance.reason,
            )
        assert advance.record is not None
        records.append(advance.record)
        book = advance.record.book
    return ResearchScheduleResult(
        "completed_scenario", initial_book.cash_fen, requested_end, book, tuple(records), None, None
    )
