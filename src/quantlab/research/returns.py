"""Return calculation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd


def _validate_horizons(horizons: Sequence[int]) -> None:
    for horizon in horizons:
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")


def calculate_returns(
    prices: pd.DataFrame,
    open_trade_dates: Sequence[date],
    horizons: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Calculate N-session returns on the global trading calendar.

    ``return_Nd = adj_close_t / adj_close_s - 1`` where ``s`` is the market
    session exactly ``N`` open days before ``t`` on the global calendar. If the
    instrument has no bar on that session (e.g. suspended), the return is NaN.
    No forward fill, no per-stock row fallback, no future leakage.
    """
    _validate_horizons(horizons)
    open_dates = sorted(open_trade_dates)
    index_map = {day: index for index, day in enumerate(open_dates)}

    frame = prices.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    frame = frame.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)

    for horizon in horizons:
        column = f"return_{horizon}d"
        frame[column] = np.nan
        for _, group in frame.groupby("instrument_id", sort=False):
            price_by_date = dict(zip(group["trade_date"], group["adj_close"], strict=True))
            returns: list[float] = []
            for trade_date in group["trade_date"]:
                current_index = index_map.get(trade_date)
                target_index = None if current_index is None else current_index - horizon
                if target_index is not None and target_index >= 0:
                    target_date = open_dates[target_index]
                    target_price = price_by_date.get(target_date)
                    if target_price is not None:
                        returns.append(price_by_date[trade_date] / target_price - 1)
                    else:
                        returns.append(math.nan)
                else:
                    returns.append(math.nan)
            frame.loc[group.index, column] = returns

    return frame
