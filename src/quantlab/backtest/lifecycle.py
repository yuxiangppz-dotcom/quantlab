"""Instrument lifecycle monitor for the research backtest.

Four time concepts are kept distinct:

- ``termination_decision_date``: when the termination decision was made
  (does NOT immediately stop trading; a delisting-arrangement period may
  follow).
- ``last_trading_date``: the last day the instrument traded, only filled when
  evidence exists. It is never derived as ``delist_date - 1``.
- ``delisting_effective_date``: when the instrument actually becomes invalid.
- ``available_from``: when the strategy may use the announcement information.

A position's last mark is only ``last_observed_price_date`` — it is not assumed
to equal ``last_trading_date``.

Two delisting boundary interpretations are supported:

- ``legacy_delist_date_inclusive``: ``delist_date`` is valid through its date
  (invalid on ``trade_date > delist_date``). This is the reference baseline.
- ``delist_date_is_first_invalid_v1``: ``delist_date`` is the delisting
  effective date (invalid on ``trade_date >= delist_date``).

``SecurityCodeChange`` keeps its own ``effective_date`` rule in both modes.
A missing price is never evidence of a delisting by itself, and ``list_status``
is never used as a historical filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quantlab.data.models import Security, SecurityCodeChange

LEGACY_DELIST_DATE_INCLUSIVE = "legacy_delist_date_inclusive"
DELIST_DATE_IS_FIRST_INVALID_V1 = "delist_date_is_first_invalid_v1"

_MODES = (LEGACY_DELIST_DATE_INCLUSIVE, DELIST_DATE_IS_FIRST_INVALID_V1)


def is_instrument_invalid_on_delist_boundary(
    trade_date: date,
    delist_date: date,
    mode: str,
) -> bool:
    """Return whether ``trade_date`` is past the delist validity boundary.

    This is the single source of truth for the legacy/v1 delist date
    comparison. Both ``LifecycleMonitor`` and ``first_invalid_open_session``
    delegate to it, and the audit table never re-handwrites ``>`` / ``>=``.

    - ``legacy_delist_date_inclusive``: invalid when ``trade_date > delist_date``.
    - ``delist_date_is_first_invalid_v1``: invalid when ``trade_date >= delist_date``.
    """
    if mode not in _MODES:
        raise ValueError(f"invalid lifecycle mode {mode!r}")
    if mode == DELIST_DATE_IS_FIRST_INVALID_V1:
        return trade_date >= delist_date
    return trade_date > delist_date


@dataclass(frozen=True)
class EventSpec:
    """A lifecycle event detected for one instrument on one session."""

    event_type: str  # "delist" | "code_change" | "conflict"
    event_date: date
    description: str


class LifecycleMonitor:
    """Detect unsupported instrument lifecycle events from canonical data."""

    def __init__(
        self,
        securities: list[Security],
        code_changes: list[SecurityCodeChange],
        mode: str = LEGACY_DELIST_DATE_INCLUSIVE,
    ) -> None:
        if mode not in _MODES:
            raise ValueError(f"invalid lifecycle mode {mode!r}")
        self.mode = mode
        self.delist_map: dict[str, date] = {
            s.instrument_id: s.delist_date
            for s in securities
            if s.delist_date is not None
        }
        self.code_change_map: dict[str, date] = {
            cc.old_instrument_id: cc.effective_date for cc in code_changes
        }
        self.conflicts: set[str] = set(self.delist_map) & set(self.code_change_map)

    def conflict_diagnostics(self) -> list[dict]:
        """Structured static conflict diagnostics (do not imply termination yet)."""
        return [
            {
                "instrument_id": i,
                "delist_date": self.delist_map[i].isoformat(),
                "code_change_date": self.code_change_map[i].isoformat(),
            }
            for i in sorted(self.conflicts)
        ]

    def _delist_fired(self, delist: date | None, trade_date: date) -> bool:
        if delist is None:
            return False
        return is_instrument_invalid_on_delist_boundary(trade_date, delist, self.mode)

    def event_for(self, instrument_id: str, trade_date: date) -> EventSpec | None:
        """Return an event if ``instrument_id`` is invalid at ``trade_date``."""
        delist = self.delist_map.get(instrument_id)
        code_change = self.code_change_map.get(instrument_id)

        delist_fired = self._delist_fired(delist, trade_date)
        code_change_fired = code_change is not None and trade_date >= code_change

        if instrument_id in self.conflicts:
            if delist_fired or code_change_fired:
                fired_dates = []
                if delist_fired:
                    fired_dates.append(delist)
                if code_change_fired:
                    fired_dates.append(code_change)
                return EventSpec(
                    event_type="conflict",
                    event_date=min(fired_dates),
                    description=(
                        f"conflict: delist_date {delist} and "
                        f"code_change effective_date {code_change}"
                    ),
                )
            return None

        if delist_fired:
            if self.mode == DELIST_DATE_IS_FIRST_INVALID_V1:
                description = f"invalid from delist_date {delist}"
            else:
                description = f"invalid after delist_date {delist}"
            return EventSpec(
                event_type="delist",
                event_date=delist,
                description=description,
            )
        if code_change_fired:
            return EventSpec(
                event_type="code_change",
                event_date=code_change,
                description=f"invalid from code-change effective_date {code_change}",
            )
        return None


def first_invalid_open_session(
    delist_date: date,
    open_dates: list[date],
    mode: str,
) -> date | None:
    """Return the first open session where the instrument is invalid.

    Delegates to :func:`is_instrument_invalid_on_delist_boundary` so the
    ``>`` / ``>=`` comparison lives in exactly one place. If ``delist_date``
    precedes every ``open_dates`` entry (instrument already invalid before the
    window), the first open session is returned.
    """
    if mode not in _MODES:
        raise ValueError(f"invalid lifecycle mode {mode!r}")
    return next(
        (
            d
            for d in open_dates
            if is_instrument_invalid_on_delist_boundary(d, delist_date, mode)
        ),
        None,
    )


def pit_eligible_instrument_ids(
    securities: list[Security],
    code_changes: list[SecurityCodeChange],
    as_of: date,
    mode: str,
    universe_predicate=None,
) -> list[str]:
    """Return instrument ids PIT-eligible on ``as_of`` from the security master
    and the frozen code-change lineage facts.

    Eligibility is defined purely by the instrument master, the code-change
    lineage and the frozen lifecycle boundary semantics — never by price
    availability:

    - **Code-change identity intervals are authoritative.** Before a
      code-change ``effective_date`` only the OLD instrument id exists;
      from the effective date (inclusive) only the NEW id exists. A
      successor's vendor-backfilled ``original_list_date`` (mirrored into
      the successor master row's ``list_date``) must never make the future
      successor id PIT-visible early, and backfilled successor-id price
      history never makes it a historically visible code.
    - **Predecessors absent from the master stay eligible** (from the
      lineage ``original_list_date`` until the day before the effective
      date). Such an instrument is typically eligible-but-unpriced and is
      handled by the engine's missing-price rules (unfilled weight stays
      cash); it is never silently dropped and never silently aliased to the
      successor's backfilled prices.
    - Old and new identities of one lineage can never be co-eligible on the
      same session (no double counting).
    - Other instruments: listed on or before ``as_of`` and not
      lifecycle-invalid at ``as_of`` under ``mode`` (delist boundary via
      :func:`is_instrument_invalid_on_delist_boundary`, code-change
      ``effective_date`` rule), checked through the same
      :class:`LifecycleMonitor` the engine uses.
    - ``universe_predicate`` (e.g. the V1 SH/SZ A-share definition) applies
      to every candidate; current ``list_status`` is never used as a
      historical filter.
    """
    monitor = LifecycleMonitor(securities, code_changes, mode=mode)
    predicate = (
        universe_predicate if universe_predicate is not None else (lambda _: True)
    )
    master_ids = {s.instrument_id for s in securities}
    predecessor_change = {c.old_instrument_id: c for c in code_changes}
    successor_change = {c.new_instrument_id: c for c in code_changes}

    eligible: set[str] = set()
    for s in securities:
        successor = successor_change.get(s.instrument_id)
        if successor is not None:
            # successor identity begins exactly at the effective date; the
            # master's backfilled original list_date is NOT a visibility fact
            if as_of < successor.effective_date:
                continue
        elif s.list_date is None or s.list_date > as_of:
            continue
        if not predicate(s.instrument_id):
            continue
        if monitor.event_for(s.instrument_id, as_of) is None:
            eligible.add(s.instrument_id)

    for old_id, change in predecessor_change.items():
        if old_id in master_ids:
            # master row present: the monitor governs identity (its
            # code_change event fires from the effective date)
            continue
        if not predicate(old_id):
            continue
        start = change.original_list_date
        if start is not None and start <= as_of < change.effective_date:
            eligible.add(old_id)
    return sorted(eligible)


def pit_eligibility_frame(
    securities: list[Security],
    code_changes: list[SecurityCodeChange],
    signal_dates: list[date],
    mode: str,
    universe_predicate=None,
) -> pd.DataFrame:
    """Build the (instrument_id, trade_date) PIT-eligibility cross-section.

    For every ``signal_dates`` entry the frame contains one row per
    PIT-eligible instrument under ``mode`` — including instruments with no
    price row on that date. This is the eligibility input the benchmark
    control portfolio consumes (as opposed to a price-backed research
    universe, where a suspension silently drops the instrument).
    """
    rows = [
        {"instrument_id": instrument_id, "trade_date": d}
        for d in signal_dates
        for instrument_id in pit_eligible_instrument_ids(
            securities, code_changes, d, mode, universe_predicate
        )
    ]
    return pd.DataFrame(rows, columns=["instrument_id", "trade_date"])
