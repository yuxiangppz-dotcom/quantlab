"""Research-layer models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ResearchDailyPrice:
    """Adjusted daily close for a single instrument on a single trading date.

    ``adj_close`` is the raw close multiplied by the cumulative adjustment
    factor (no forward adjustment, no future-factor leakage).
    """

    instrument_id: str
    trade_date: date
    close: float
    adj_factor: float
    adj_close: float
