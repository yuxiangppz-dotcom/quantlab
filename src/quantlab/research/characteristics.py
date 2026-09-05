"""Market characteristics (daily_basic) loading."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pandas as pd

from quantlab.data.storage import ParquetStorage

_COLUMNS = ["instrument_id", "trade_date", "turnover_rate", "total_mv", "circ_mv"]


def load_market_characteristics(
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Load daily_basic metrics for every open trading day in a date range.

    Returns a DataFrame with ``instrument_id, trade_date, turnover_rate,
    total_mv, circ_mv``. Missing daily_basic files are skipped; the caller
    joins exactly on ``(instrument_id, trade_date)`` and gets NaN for missing
    characteristics (no forward/backward fill).
    """
    calendar = storage.load_trading_calendar()
    open_dates = sorted({c.trade_date for c in calendar if c.is_open})
    dates = [d for d in open_dates if start_date <= d <= end_date]

    rows = []
    for d in dates:
        items = storage.load_daily_basic_by_date(d)
        rows.extend(asdict(item) for item in items)

    if not rows:
        return pd.DataFrame(columns=_COLUMNS)
    frame = pd.DataFrame(rows, columns=_COLUMNS)
    return frame.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
