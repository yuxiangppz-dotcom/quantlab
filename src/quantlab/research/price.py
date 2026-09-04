"""Adjusted price construction."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pandas as pd

from quantlab.data.models import AdjFactor, DailyBar
from quantlab.research.models import ResearchDailyPrice

_PRICE_COLUMNS = ["instrument_id", "trade_date", "close", "adj_factor", "adj_close"]


def build_prices(
    daily: list[DailyBar],
    adj_factors: list[AdjFactor],
) -> list[ResearchDailyPrice]:
    """Join daily bars with adj factors and compute ``adj_close = close * adj_factor``.

    Bars without a matching adj factor are dropped. The result is not sorted.
    """
    factor_map = {(f.instrument_id, f.trade_date): f.adj_factor for f in adj_factors}
    prices: list[ResearchDailyPrice] = []
    for bar in daily:
        factor = factor_map.get((bar.instrument_id, bar.trade_date))
        if factor is None:
            continue
        prices.append(
            ResearchDailyPrice(
                instrument_id=bar.instrument_id,
                trade_date=bar.trade_date,
                close=bar.close,
                adj_factor=factor,
                adj_close=bar.close * factor,
            )
        )
    return prices


def filter_point_in_time(
    prices: list[ResearchDailyPrice],
    list_dates: dict[str, date],
    delist_dates: dict[str, date | None] | None = None,
) -> list[ResearchDailyPrice]:
    """Keep only prices within each security's [list_date, delist_date] window."""
    delist_dates = delist_dates or {}
    kept: list[ResearchDailyPrice] = []
    for price in prices:
        list_date = list_dates.get(price.instrument_id)
        if list_date is not None and price.trade_date < list_date:
            continue
        delist_date = delist_dates.get(price.instrument_id)
        if delist_date is not None and price.trade_date > delist_date:
            continue
        kept.append(price)
    return kept


def prices_to_frame(prices: list[ResearchDailyPrice]) -> pd.DataFrame:
    """Convert research prices to a DataFrame sorted by instrument_id, trade_date."""
    frame = pd.DataFrame([asdict(price) for price in prices], columns=_PRICE_COLUMNS)
    return frame.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
