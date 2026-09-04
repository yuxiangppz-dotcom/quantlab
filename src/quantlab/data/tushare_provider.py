"""Tushare-backed :class:`DataProvider` implementation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd
import tushare as ts

from quantlab.data.models import (
    SSE,
    SZSE,
    DailyBar,
    Security,
    TradingCalendar,
    format_yyyymmdd,
    parse_instrument_id,
    parse_required_yyyymmdd,
    parse_yyyymmdd,
)
from quantlab.data.provider import DataProvider

_SECURITY_FIELDS = "ts_code,symbol,name,exchange,market,list_status,list_date,delist_date"
_LIST_STATUSES = ("L", "D", "P")
_CALENDAR_EXCHANGES = (SSE, SZSE)


def security_from_row(row: Mapping[str, Any]) -> Security:
    """Map a Tushare ``stock_basic`` row to a :class:`Security`."""
    symbol, market = parse_instrument_id(row["ts_code"])
    return Security(
        instrument_id=row["ts_code"],
        symbol=symbol,
        name=str(row["name"]),
        exchange=str(row["exchange"]),
        market=market,
        board=str(row["market"]),
        list_status=str(row["list_status"]),
        list_date=parse_required_yyyymmdd(row["list_date"]),
        delist_date=parse_yyyymmdd(row.get("delist_date")),
    )


def calendar_from_row(row: Mapping[str, Any]) -> TradingCalendar:
    """Map a Tushare ``trade_cal`` row to a :class:`TradingCalendar`."""
    return TradingCalendar(
        exchange=str(row["exchange"]),
        trade_date=parse_required_yyyymmdd(row["cal_date"]),
        is_open=bool(int(row["is_open"])),
    )


def daily_bar_from_row(row: Mapping[str, Any]) -> DailyBar:
    """Map a Tushare ``daily`` row to a :class:`DailyBar`.

    Tushare's ``vol`` is in hands (lots of 100 shares) and ``amount`` is in
    thousands of yuan; both are converted to canonical units (shares and CNY).
    """
    return DailyBar(
        instrument_id=row["ts_code"],
        trade_date=parse_required_yyyymmdd(row["trade_date"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        pre_close=float(row["pre_close"]),
        volume=float(row["vol"]) * 100,
        amount=float(row["amount"]) * 1000,
    )


class TushareProvider(DataProvider):
    """Data provider backed by the Tushare HTTP API."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("TUSHARE_TOKEN")
        if not self._token:
            raise RuntimeError(
                "TUSHARE_TOKEN is not set. Set the Tushare API token via the "
                "TUSHARE_TOKEN environment variable, e.g. `export TUSHARE_TOKEN=...`."
            )
        self._pro = ts.pro_api(self._token)

    def get_securities(self) -> list[Security]:
        frames = []
        for status in _LIST_STATUSES:
            frame = self._pro.stock_basic(
                exchange="",
                list_status=status,
                fields=_SECURITY_FIELDS,
            )
            frames.append(frame)
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset=["ts_code"])
        return [security_from_row(row) for row in merged.to_dict("records")]

    def get_trading_calendar(self, start_date: date, end_date: date) -> list[TradingCalendar]:
        rows: list[dict[str, Any]] = []
        for exchange in _CALENDAR_EXCHANGES:
            frame = self._pro.trade_cal(
                exchange=exchange,
                start_date=format_yyyymmdd(start_date),
                end_date=format_yyyymmdd(end_date),
            )
            rows.extend(frame.to_dict("records"))
        return [calendar_from_row(row) for row in rows]

    def get_daily_bars(
        self,
        instrument_ids: list[str],
        start_date: date,
        end_date: date,
    ) -> list[DailyBar]:
        frame = self._pro.daily(
            ts_code=",".join(instrument_ids),
            start_date=format_yyyymmdd(start_date),
            end_date=format_yyyymmdd(end_date),
        )
        return [daily_bar_from_row(row) for row in frame.to_dict("records")]

    def get_daily_bars_by_date(self, trade_date: date) -> list[DailyBar]:
        frame = self._pro.daily(trade_date=format_yyyymmdd(trade_date))
        return [daily_bar_from_row(row) for row in frame.to_dict("records")]
