"""Research dataset builder."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from quantlab.data.models import DataValidationError, Security, SecurityCodeChange
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.research.price import build_prices, filter_point_in_time, prices_to_frame
from quantlab.research.returns import (
    _validate_horizons,
    calculate_forward_returns,
    calculate_returns,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_CODE_CHANGES_PATH = _PROJECT_ROOT / "config" / "security_code_changes.csv"


def _empty_frame(
    return_horizons: tuple[int, ...],
    forward_horizons: tuple[int, ...],
) -> pd.DataFrame:
    columns = ["instrument_id", "trade_date", "close", "adj_factor", "adj_close"]
    columns += [f"return_{h}d" for h in return_horizons]
    columns += [f"future_return_{h}d" for h in forward_horizons]
    return pd.DataFrame(columns=columns)


def _build_list_dates(
    securities: list[Security],
    code_changes: list[SecurityCodeChange],
) -> dict[str, date]:
    list_dates = {s.instrument_id: s.list_date for s in securities}
    for change in code_changes:
        list_dates[change.old_instrument_id] = change.original_list_date
        list_dates[change.new_instrument_id] = change.effective_date
    return list_dates


def _build_delist_dates(
    securities: list[Security],
    code_changes: list[SecurityCodeChange],
) -> dict[str, date | None]:
    delist_dates = {s.instrument_id: s.delist_date for s in securities}
    for change in code_changes:
        delist_dates[change.old_instrument_id] = change.effective_date - timedelta(days=1)
    return delist_dates


def build_research_dataset(
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    return_horizons: tuple[int, ...] = (1, 5, 20),
    forward_horizons: tuple[int, ...] = (1, 5, 20),
) -> pd.DataFrame:
    """Build a research sample DataFrame from local canonical data.

    Reads securities, trading calendar, daily bars and adj factors from
    ``storage``, applies point-in-time filtering, then computes historical and
    forward returns on the global market calendar. The internal read range is
    padded by ``max(return_horizons)`` sessions before ``start_date`` and
    ``max(forward_horizons)`` sessions after ``end_date`` (market sessions,
    not calendar days); the output is trimmed to ``[start_date, end_date]``.
    """
    if start_date > end_date:
        raise ValueError(f"start_date ({start_date}) must be <= end_date ({end_date})")
    _validate_horizons(return_horizons)
    _validate_horizons(forward_horizons)

    securities = storage.load_securities()
    if not securities:
        raise DataValidationError("securities canonical data is missing or empty")
    calendar = storage.load_trading_calendar()
    if not calendar:
        raise DataValidationError("trading calendar canonical data is missing or empty")

    cal_min = min(c.trade_date for c in calendar)
    cal_max = max(c.trade_date for c in calendar)
    if start_date < cal_min or end_date > cal_max:
        raise DataValidationError(
            f"requested range [{start_date}, {end_date}] is outside "
            f"calendar coverage [{cal_min}, {cal_max}]"
        )

    code_changes = load_security_code_changes(_CODE_CHANGES_PATH)
    list_dates = _build_list_dates(securities, code_changes)
    delist_dates = _build_delist_dates(securities, code_changes)

    open_dates = sorted({c.trade_date for c in calendar if c.is_open})
    in_range = [d for d in open_dates if start_date <= d <= end_date]
    if not in_range:
        return _empty_frame(return_horizons, forward_horizons)

    max_hist = max(return_horizons) if return_horizons else 0
    max_fwd = max(forward_horizons) if forward_horizons else 0

    first_idx = open_dates.index(in_range[0])
    last_idx = open_dates.index(in_range[-1])
    padded_first = max(0, first_idx - max_hist)
    padded_last = min(len(open_dates) - 1, last_idx + max_fwd)
    padded_dates = open_dates[padded_first : padded_last + 1]

    daily_bars = []
    adj_factors = []
    for trade_date in padded_dates:
        if not storage.daily_bars_exists(trade_date):
            raise DataValidationError(f"Missing daily parquet for {trade_date}")
        if not storage.adj_factor_exists(trade_date):
            raise DataValidationError(f"Missing adj_factor parquet for {trade_date}")
        daily_bars.extend(storage.load_daily_bars_by_date(trade_date))
        adj_factors.extend(storage.load_adj_factors_by_date(trade_date))

    prices = build_prices(daily_bars, adj_factors, strict=True)
    prices = filter_point_in_time(prices, list_dates, delist_dates)

    frame = prices_to_frame(prices)
    frame = calculate_returns(frame, open_dates, horizons=return_horizons)
    frame = calculate_forward_returns(frame, open_dates, horizons=forward_horizons)

    frame = frame[(frame["trade_date"] >= start_date) & (frame["trade_date"] <= end_date)]
    return frame.reset_index(drop=True)
