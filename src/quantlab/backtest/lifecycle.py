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

from quantlab.data.models import Security, SecurityCodeChange

LEGACY_DELIST_DATE_INCLUSIVE = "legacy_delist_date_inclusive"
DELIST_DATE_IS_FIRST_INVALID_V1 = "delist_date_is_first_invalid_v1"

_MODES = (LEGACY_DELIST_DATE_INCLUSIVE, DELIST_DATE_IS_FIRST_INVALID_V1)


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
        if self.mode == DELIST_DATE_IS_FIRST_INVALID_V1:
            return trade_date >= delist
        return trade_date > delist

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
            return EventSpec(
                event_type="delist",
                event_date=delist,
                description=f"invalid at delist_date {delist} (mode={self.mode})",
            )
        if code_change_fired:
            return EventSpec(
                event_type="code_change",
                event_date=code_change,
                description=f"invalid from code-change effective_date {code_change}",
            )
        return None
