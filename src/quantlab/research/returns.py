"""Return calculation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

from quantlab.data.models import DataValidationError


def _validate_horizons(horizons: Sequence[int]) -> None:
    for horizon in horizons:
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")


def _normalize_open_trade_dates(open_trade_dates: Sequence[date]) -> list[date]:
    """Return the unique, sorted open trading sessions (dedupe SSE/SZSE)."""
    return sorted(set(open_trade_dates))


def _prepare_frame(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)


def _validate_calendar_consistency(frame: pd.DataFrame, open_dates: list[date]) -> None:
    if frame.empty:
        return
    if not open_dates:
        raise DataValidationError("open_trade_dates is empty while prices is not")
    valid = set(open_dates)
    missing = sorted(set(frame["trade_date"]) - valid)
    if missing:
        raise DataValidationError(
            f"price trade_date not in open market calendar: {missing[:5]}"
        )


def _session_lookup_returns(
    frame: pd.DataFrame,
    open_dates: list[date],
    index_map: dict[date, int],
    horizons: tuple[int, ...],
    *,
    forward: bool,
) -> pd.DataFrame:
    """Compute exact-session returns per instrument for each horizon."""
    for horizon in horizons:
        column = f"future_return_{horizon}d" if forward else f"return_{horizon}d"
        frame[column] = np.nan
        offset = horizon if forward else -horizon
        for _, group in frame.groupby("instrument_id", sort=False):
            price_by_date = dict(zip(group["trade_date"], group["adj_close"], strict=True))
            values: list[float] = []
            for trade_date in group["trade_date"]:
                current_index = index_map.get(trade_date)
                target_index = None if current_index is None else current_index + offset
                if target_index is not None and 0 <= target_index < len(open_dates):
                    target_date = open_dates[target_index]
                    target_price = price_by_date.get(target_date)
                    if target_price is not None:
                        current_price = price_by_date[trade_date]
                        if forward:
                            values.append(target_price / current_price - 1)
                        else:
                            values.append(current_price / target_price - 1)
                    else:
                        values.append(np.nan)
                else:
                    values.append(np.nan)
            frame.loc[group.index, column] = values
    return frame


def calculate_returns(
    prices: pd.DataFrame,
    open_trade_dates: Sequence[date],
    horizons: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Calculate N-session historical returns on the global trading calendar.

    ``return_Nd = adj_close_t / adj_close_s - 1`` where ``s`` is the market
    session exactly ``N`` open days before ``t``. If the instrument has no bar
    on that session (e.g. suspended), the return is NaN. No forward fill, no
    per-stock row fallback, no future leakage.
    """
    _validate_horizons(horizons)
    open_dates = _normalize_open_trade_dates(open_trade_dates)
    frame = _prepare_frame(prices)
    _validate_calendar_consistency(frame, open_dates)
    index_map = {day: index for index, day in enumerate(open_dates)}
    return _session_lookup_returns(frame, open_dates, index_map, horizons, forward=False)


def calculate_forward_returns(
    prices: pd.DataFrame,
    open_trade_dates: Sequence[date],
    horizons: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Calculate N-session forward returns on the global trading calendar.

    ``future_return_Nd = adj_close_s / adj_close_t - 1`` where ``s`` is the
    market session exactly ``N`` open days after ``t``. This is a research
    *label* (uses future prices) for evaluating alpha signals; it must not feed
    into t-time feature computation. Suspended target sessions yield NaN.
    """
    _validate_horizons(horizons)
    open_dates = _normalize_open_trade_dates(open_trade_dates)
    frame = _prepare_frame(prices)
    _validate_calendar_consistency(frame, open_dates)
    index_map = {day: index for index, day in enumerate(open_dates)}
    return _session_lookup_returns(frame, open_dates, index_map, horizons, forward=True)
