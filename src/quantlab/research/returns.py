"""Return calculation."""

from __future__ import annotations

import pandas as pd


def calculate_returns(
    prices: pd.DataFrame,
    horizons: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Calculate n-period adjusted-close returns per instrument.

    ``return_nd = adj_close_t / adj_close_(t-n) - 1``. Each instrument is
    computed independently on its own sorted trade-date sequence; missing
    dates are not forward-filled and only past data is used.
    """
    frame = prices.sort_values(["instrument_id", "trade_date"]).copy()
    for horizon in horizons:
        frame[f"return_{horizon}d"] = frame.groupby("instrument_id")["adj_close"].transform(
            lambda series, h=horizon: series / series.shift(h) - 1
        )
    return frame.reset_index(drop=True)
