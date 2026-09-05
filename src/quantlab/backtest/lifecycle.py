"""Instrument lifecycle monitor for the research backtest.

Decouples unsupported-event detection from price availability. The canonical
validity boundary is:

- ``Security.list_date`` / ``delist_date`` describe an **inclusive** valid
  interval, so a held instrument becomes unsupported on the first session with
  ``trade_date > delist_date``.
- ``SecurityCodeChange.old_instrument_id`` is invalid from ``effective_date``
  onward, so it is checked with ``trade_date >= effective_date``.

A missing price is never evidence of a delisting by itself, and ``list_status``
is never used as a historical filter. A static conflict (an instrument having
both a delist date and a code-change date) is a structured diagnostic only; it
does not block historical sessions until one of the two invalidation conditions
actually fires.
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

    def event_for(self, instrument_id: str, trade_date: date) -> EventSpec | None:
        """Return an event if ``instrument_id`` is invalid at ``trade_date``."""
        delist = self.delist_map.get(instrument_id)
        code_change = self.code_change_map.get(instrument_id)

        delist_fired = delist is not None and trade_date > delist
        code_change_fired = code_change is not None and trade_date >= code_change

        if instrument_id in self.conflicts:
            # Only block once one of the two invalidation conditions actually
            # fires; the mere coexistence of both definitions is not termination.
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
            return EventSpec(
                event_type="delist",
                event_date=delist,
                description=f"held beyond delist_date {delist}",
            )
        if code_change_fired:
            return EventSpec(
                event_type="code_change",
                event_date=code_change,
                description=f"invalid from code-change effective_date {code_change}",
            )
        return None
