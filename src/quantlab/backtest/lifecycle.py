"""Instrument lifecycle monitor for the research backtest.

Decouples unsupported-event detection from price availability. The canonical
validity boundary is:

- ``Security.list_date`` / ``delist_date`` describe an **inclusive** valid
  interval, so a held instrument becomes unsupported on the first session with
  ``trade_date > delist_date``.
- ``SecurityCodeChange.old_instrument_id`` is invalid from ``effective_date``
  onward, so it is checked with ``trade_date >= effective_date``.

A missing price is never evidence of a delisting by itself, and ``list_status``
is never used as a historical filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from quantlab.data.models import Security, SecurityCodeChange


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
    ) -> None:
        self.delist_map: dict[str, date] = {
            s.instrument_id: s.delist_date
            for s in securities
            if s.delist_date is not None
        }
        self.code_change_map: dict[str, date] = {
            cc.old_instrument_id: cc.effective_date for cc in code_changes
        }
        self.conflicts: set[str] = set(self.delist_map) & set(self.code_change_map)

    def event_for(self, instrument_id: str, trade_date: date) -> EventSpec | None:
        """Return an event if ``instrument_id`` is invalid at ``trade_date``."""
        if instrument_id in self.conflicts:
            event_date = min(
                self.delist_map[instrument_id], self.code_change_map[instrument_id]
            )
            return EventSpec(
                event_type="conflict",
                event_date=event_date,
                description="instrument has both delist and code_change definitions",
            )
        delist = self.delist_map.get(instrument_id)
        if delist is not None and trade_date > delist:
            return EventSpec(
                event_type="delist",
                event_date=delist,
                description=f"held beyond delist_date {delist}",
            )
        code_change = self.code_change_map.get(instrument_id)
        if code_change is not None and trade_date >= code_change:
            return EventSpec(
                event_type="code_change",
                event_date=code_change,
                description=f"invalid from code-change effective_date {code_change}",
            )
        return None
