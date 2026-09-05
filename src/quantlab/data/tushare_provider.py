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
    AdjFactor,
    DailyBar,
    DailyBasic,
    RawLifecycleAnnouncement,
    Security,
    StockSTStatus,
    SuspensionRecord,
    TradingCalendar,
    canonical_payload_fingerprint,
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


def _to_float(value: Any) -> float:
    """Convert a provider numeric value to float; None/NaN become float('nan')."""
    if value is None or pd.isna(value):
        return float("nan")
    return float(value)


def daily_bar_from_row(row: Mapping[str, Any]) -> DailyBar:
    """Map a Tushare ``daily`` row to a :class:`DailyBar`.

    Tushare's ``vol`` is in hands (lots of 100 shares) and ``amount`` is in
    thousands of yuan; both are converted to canonical units (shares and CNY).
    """
    return DailyBar(
        instrument_id=row["ts_code"],
        trade_date=parse_required_yyyymmdd(row["trade_date"]),
        open=_to_float(row["open"]),
        high=_to_float(row["high"]),
        low=_to_float(row["low"]),
        close=_to_float(row["close"]),
        pre_close=_to_float(row["pre_close"]),
        volume=_to_float(row["vol"]) * 100,
        amount=_to_float(row["amount"]) * 1000,
    )


def adj_factor_from_row(row: Mapping[str, Any]) -> AdjFactor:
    """Map a Tushare ``adj_factor`` row to a :class:`AdjFactor`."""
    return AdjFactor(
        instrument_id=row["ts_code"],
        trade_date=parse_required_yyyymmdd(row["trade_date"]),
        adj_factor=_to_float(row["adj_factor"]),
    )


def daily_basic_from_row(row: Mapping[str, Any]) -> DailyBasic:
    """Map a Tushare ``daily_basic`` row to a :class:`DailyBasic`.

    ``turnover_rate`` percent -> decimal; ``total_mv`` / ``circ_mv`` 万元 -> CNY.
    """
    return DailyBasic(
        instrument_id=row["ts_code"],
        trade_date=parse_required_yyyymmdd(row["trade_date"]),
        turnover_rate=_to_float(row["turnover_rate"]) / 100.0,
        total_mv=_to_float(row["total_mv"]) * 10000.0,
        circ_mv=_to_float(row["circ_mv"]) * 10000.0,
    )


def _optional_text(row: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and not pd.isna(value):
            text = str(value).strip()
            if text:
                return text
    return None


def lifecycle_announcement_from_row(row: Mapping[str, Any]) -> RawLifecycleAnnouncement:
    """Convert a Tushare ``anns_d`` index row without classifying its title."""
    payload = dict(row)
    announcement_date = parse_required_yyyymmdd(
        payload.get("ann_date") or payload.get("trade_date")
    )
    record_id = _optional_text(payload, "ann_id", "id", "source_record_id")
    fingerprint = canonical_payload_fingerprint(payload)
    return RawLifecycleAnnouncement(
        source="tushare.anns_d",
        source_record_id=record_id or fingerprint,
        instrument_id=_optional_text(payload, "ts_code"),
        announcement_date=announcement_date,
        announcement_time=_optional_text(payload, "ann_time", "rec_time"),
        title=_optional_text(payload, "title", "name") or "",
        source_url=_optional_text(payload, "url", "source_url"),
        raw_payload=pd.Series(payload).to_json(force_ascii=False, date_format="iso"),
        content_fingerprint=fingerprint,
    )


def stock_st_from_row(row: Mapping[str, Any]) -> StockSTStatus:
    payload = dict(row)
    record_id = _optional_text(payload, "id", "source_record_id")
    record_id = record_id or canonical_payload_fingerprint(payload)
    return StockSTStatus(
        instrument_id=str(payload["ts_code"]),
        trade_date=parse_required_yyyymmdd(payload.get("trade_date") or payload.get("ann_date")),
        name=_optional_text(payload, "name"),
        status=_optional_text(payload, "status", "type"),
        source_record_id=record_id,
    )


def suspension_from_row(row: Mapping[str, Any]) -> SuspensionRecord:
    payload = dict(row)
    record_id = _optional_text(payload, "id", "source_record_id")
    record_id = record_id or canonical_payload_fingerprint(payload)
    return SuspensionRecord(
        instrument_id=str(payload["ts_code"]),
        suspend_date=parse_required_yyyymmdd(
            payload.get("suspend_date") or payload.get("trade_date")
        ),
        resume_date=parse_yyyymmdd(payload.get("resume_date")),
        suspend_reason=_optional_text(payload, "suspend_reason", "reason"),
        source_record_id=record_id,
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

    def get_adj_factors_by_date(self, trade_date: date) -> list[AdjFactor]:
        frame = self._pro.adj_factor(trade_date=format_yyyymmdd(trade_date))
        return [adj_factor_from_row(row) for row in frame.to_dict("records")]

    def get_daily_basic_by_date(self, trade_date: date) -> list[DailyBasic]:
        frame = self._pro.daily_basic(
            trade_date=format_yyyymmdd(trade_date),
            fields="ts_code,trade_date,turnover_rate,total_mv,circ_mv",
        )
        return [daily_basic_from_row(row) for row in frame.to_dict("records")]

    def get_lifecycle_announcements_by_date(
        self, announcement_date: date
    ) -> list[RawLifecycleAnnouncement]:
        frame = self._pro.anns_d(ann_date=format_yyyymmdd(announcement_date))
        return [lifecycle_announcement_from_row(row) for row in frame.to_dict("records")]

    def get_stock_st(self, start_date: date, end_date: date) -> list[StockSTStatus]:
        frame = self._pro.stock_st(
            start_date=format_yyyymmdd(start_date), end_date=format_yyyymmdd(end_date)
        )
        return [stock_st_from_row(row) for row in frame.to_dict("records")]

    def get_suspensions(self, start_date: date, end_date: date) -> list[SuspensionRecord]:
        rows: list[dict[str, Any]] = []
        current = start_date
        while current <= end_date:
            frame = self._pro.suspend_d(suspend_date=format_yyyymmdd(current))
            if len(frame) >= 5000:
                raise RuntimeError("suspend_d response reached provider page limit")
            rows.extend(frame.to_dict("records"))
            current = date.fromordinal(current.toordinal() + 1)
        return [suspension_from_row(row) for row in rows]

    def probe_lifecycle_capabilities(self, probe_date: date) -> dict[str, dict[str, object]]:
        """Perform minimal API calls and return only safe capability metadata.

        Provider error text can include operational details, so it is never
        retained or surfaced.  The result intentionally contains only endpoint,
        outcome, row count and exception class.
        """
        def probe(endpoint: str, call) -> dict[str, object]:
            try:
                frame = call()
                result: dict[str, object] = {
                    "endpoint": endpoint,
                    "status": "available",
                    "row_count": int(len(frame)),
                }
                if len(frame) >= 5000:
                    result["limit_warning"] = "response_reaches_known_page_limit"
                return result
            except Exception as exc:  # API-specific exception types are unstable.
                return {
                    "endpoint": endpoint,
                    "status": "unavailable",
                    "error_class": type(exc).__name__,
                }

        day = format_yyyymmdd(probe_date)
        return {
            "stock_basic": probe(
                "stock_basic",
                lambda: self._pro.stock_basic(
                    exchange="", list_status="L", fields="ts_code"
                ),
            ),
            "stock_st": probe("stock_st", lambda: self._pro.stock_st(start_date=day, end_date=day)),
            "suspend_d": probe("suspend_d", lambda: self._pro.suspend_d(suspend_date=day)),
            "anns_d": probe("anns_d", lambda: self._pro.anns_d(ann_date=day)),
            "namechange": probe(
                "namechange",
                lambda: self._pro.namechange(
                    ts_code="000001.SZ", start_date=day, end_date=day
                ),
            ),
        }
