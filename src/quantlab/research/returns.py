"""Return calculation."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

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
    """Vectorized exact-session return lookup per instrument.

    ``target_session = session + offset`` where ``offset = +horizon`` for
    forward and ``-horizon`` for historical. The target price is looked up in a
    unique ``(instrument_id, session)`` index; a missing target key yields NaN.
    """
    if frame.duplicated(subset=["instrument_id", "trade_date"]).any():
        raise DataValidationError("duplicate instrument_id + trade_date in prices")

    frame = frame.copy()
    frame["_session"] = frame["trade_date"].map(index_map)

    price = frame.set_index(["instrument_id", "_session"])["adj_close"]
    current_price = frame["adj_close"].to_numpy()

    for horizon in horizons:
        offset = horizon if forward else -horizon
        target_session = frame["_session"].to_numpy() + offset
        keys = pd.MultiIndex.from_arrays(
            [frame["instrument_id"].to_numpy(), target_session]
        )
        target_price = price.reindex(keys).to_numpy()
        if forward:
            result = target_price / current_price - 1
        else:
            result = current_price / target_price - 1
        column = f"future_return_{horizon}d" if forward else f"return_{horizon}d"
        frame[column] = result

    return frame.drop(columns=["_session"])


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
