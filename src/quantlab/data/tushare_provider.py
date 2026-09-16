"""Tushare-backed :class:`DataProvider` implementation."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import tushare as ts

from quantlab.data.models import (
    SSE,
    SZSE,
    AdjFactor,
    DailyBar,
    DailyBasic,
    DailyPriceLimit,
    DataValidationError,
    DividendObservation,
    FinancialIndicatorObservation,
    IndexDailyBar,
    NameChangeRecord,
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
_SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _scaled_float(value: Any, scale: str) -> float:
    """Scale the provider decimal value before returning the existing float schema."""
    if value is None or pd.isna(value):
        return float("nan")
    return float(Decimal(str(value)) * Decimal(scale))


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
        volume=_scaled_float(row["vol"], "100"),
        amount=_scaled_float(row["amount"], "1000"),
    )


def index_daily_from_row(row: Mapping[str, Any]) -> IndexDailyBar:
    """Map a Tushare ``index_daily`` row to an :class:`IndexDailyBar`.

    Units follow the daily-bar convention: ``vol`` hands -> shares (x100) and
    ``amount`` thousands of yuan -> CNY (x1000).
    """
    return IndexDailyBar(
        instrument_id=str(row["ts_code"]),
        trade_date=parse_required_yyyymmdd(row["trade_date"]),
        open=_to_float(row.get("open")),
        high=_to_float(row.get("high")),
        low=_to_float(row.get("low")),
        close=_to_float(row["close"]),
        pre_close=_to_float(row["pre_close"]),
        volume=_scaled_float(row.get("vol"), "100"),
        amount=_scaled_float(row.get("amount"), "1000"),
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
        turnover_rate=_scaled_float(row["turnover_rate"], "0.01"),
        total_mv=_scaled_float(row["total_mv"], "10000"),
        circ_mv=_scaled_float(row["circ_mv"], "10000"),
    )


def _optional_float(row: Mapping[str, Any], name: str) -> float | None:
    value = row.get(name)
    if value is None or pd.isna(value):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise DataValidationError(f"non-finite provider value for {name}")
    return result


def daily_price_limit_from_row(row: Mapping[str, Any]) -> DailyPriceLimit:
    payload = dict(row)
    fingerprint = canonical_payload_fingerprint(payload)
    return DailyPriceLimit(
        instrument_id=str(payload["ts_code"]),
        trade_date=parse_required_yyyymmdd(payload["trade_date"]),
        pre_close=_optional_float(payload, "pre_close"),
        up_limit=_to_float(payload["up_limit"]),
        down_limit=_to_float(payload["down_limit"]),
        exchange=_optional_text(payload, "exchange"),
        source="tushare.stk_limit",
        source_record_id=fingerprint,
    )


def financial_indicator_from_row(
    row: Mapping[str, Any], observed_at: datetime
) -> FinancialIndicatorObservation:
    payload = dict(row)
    announcement_date = parse_required_yyyymmdd(payload["ann_date"])
    # No historical revision timestamp is exposed.  First local observation is
    # therefore the earliest defensible availability boundary.
    available_from = max(announcement_date, observed_at.astimezone(_SHANGHAI).date())
    return FinancialIndicatorObservation(
        instrument_id=str(payload["ts_code"]),
        announcement_date=announcement_date,
        period_end=parse_required_yyyymmdd(payload["end_date"]),
        update_flag=_optional_text(payload, "update_flag"),
        roe=_optional_float(payload, "roe"),
        roa=_optional_float(payload, "roa"),
        gross_profit_margin=_optional_float(payload, "grossprofit_margin"),
        net_profit_margin=_optional_float(payload, "netprofit_margin"),
        revenue_growth_yoy=_optional_float(payload, "tr_yoy"),
        net_profit_growth_yoy=_optional_float(payload, "netprofit_yoy"),
        operating_cashflow_to_revenue=_optional_float(payload, "ocf_to_or"),
        debt_to_assets=_optional_float(payload, "debt_to_assets"),
        observed_at=observed_at,
        available_from=available_from,
        pit_status="prospective_from_first_local_observation",
        source="tushare.fina_indicator_vip",
        source_record_id=canonical_payload_fingerprint(payload),
    )


def dividend_from_row(row: Mapping[str, Any], observed_at: datetime) -> DividendObservation:
    payload = dict(row)
    known_date = (
        parse_yyyymmdd(payload.get("imp_ann_date"))
        or parse_yyyymmdd(payload.get("ann_date"))
        or observed_at.astimezone(_SHANGHAI).date()
    )
    return DividendObservation(
        instrument_id=str(payload["ts_code"]),
        period_end=parse_yyyymmdd(payload.get("end_date")),
        announcement_date=parse_yyyymmdd(payload.get("ann_date")),
        process_status=_optional_text(payload, "div_proc"),
        stock_dividend_per_share=_optional_float(payload, "stk_div"),
        stock_bonus_rate=_optional_float(payload, "stk_bo_rate"),
        stock_conversion_rate=_optional_float(payload, "stk_co_rate"),
        cash_dividend_after_tax=_optional_float(payload, "cash_div"),
        cash_dividend_before_tax=_optional_float(payload, "cash_div_tax"),
        record_date=parse_yyyymmdd(payload.get("record_date")),
        ex_date=parse_yyyymmdd(payload.get("ex_date")),
        pay_date=parse_yyyymmdd(payload.get("pay_date")),
        share_listing_date=parse_yyyymmdd(payload.get("div_listdate")),
        implementation_announcement_date=parse_yyyymmdd(payload.get("imp_ann_date")),
        observed_at=observed_at,
        available_from=max(known_date, observed_at.astimezone(_SHANGHAI).date()),
        source="tushare.dividend",
        source_record_id=canonical_payload_fingerprint(payload),
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
        trade_date=parse_required_yyyymmdd(payload["trade_date"]),
        name=_optional_text(payload, "name"),
        status=_optional_text(payload, "type"),
        type_name=_optional_text(payload, "type_name"),
        source_record_id=record_id,
    )


def suspension_from_row(row: Mapping[str, Any]) -> SuspensionRecord:
    payload = dict(row)
    record_id = _optional_text(payload, "id", "source_record_id")
    record_id = record_id or canonical_payload_fingerprint(payload)
    return SuspensionRecord(
        instrument_id=str(payload["ts_code"]),
        trade_date=parse_required_yyyymmdd(payload["trade_date"]),
        suspend_type=str(payload["suspend_type"]),
        suspend_timing=_optional_text(payload, "suspend_timing"),
        source_record_id=record_id,
    )


def name_change_from_row(row: Mapping[str, Any]) -> NameChangeRecord:
    payload = dict(row)
    record_id = _optional_text(payload, "id", "source_record_id")
    record_id = record_id or canonical_payload_fingerprint(payload)
    return NameChangeRecord(
        instrument_id=str(payload["ts_code"]),
        start_date=parse_required_yyyymmdd(payload["start_date"]),
        end_date=parse_yyyymmdd(payload.get("end_date")),
        name=_optional_text(payload, "name"),
        change_reason=_optional_text(payload, "change_reason"),
        source_record_id=record_id,
    )


class TushareProvider(DataProvider):
    """Data provider backed by the Tushare HTTP API."""

    def __init__(self, token: str | None = None, *, archive=None, interval=0.3, attempts=3) -> None:
        self._token = token or os.environ.get("TUSHARE_TOKEN")
        if not self._token:
            raise RuntimeError(
                "TUSHARE_TOKEN is not set. Set the Tushare API token via the "
                "TUSHARE_TOKEN environment variable, e.g. `export TUSHARE_TOKEN=...`."
            )
        from quantlab.data.transport import ObservedClient

        self._pro = ObservedClient(
            ts.pro_api(self._token), archive=archive, interval=interval, attempts=attempts
        )

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
        merged = merged.drop_duplicates()
        if merged.duplicated("ts_code").any():
            raise DataValidationError("conflicting stock_basic identity rows")
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
        if len(frame) >= 6000:
            raise DataValidationError("daily reached response cap; partition the request")
        return [daily_bar_from_row(row) for row in frame.to_dict("records")]

    def get_adj_factors_by_date(self, trade_date: date) -> list[AdjFactor]:
        frame = self._pro.adj_factor(trade_date=format_yyyymmdd(trade_date))
        if len(frame) >= 6000:
            raise DataValidationError("adj_factor reached response cap; partition the request")
        return [adj_factor_from_row(row) for row in frame.to_dict("records")]

    def get_daily_basic_by_date(self, trade_date: date) -> list[DailyBasic]:
        frame = self._pro.daily_basic(
            trade_date=format_yyyymmdd(trade_date),
            fields="ts_code,trade_date,turnover_rate,total_mv,circ_mv",
        )
        if len(frame) >= 6000:
            raise DataValidationError("daily_basic reached response cap; partition the request")
        return [daily_basic_from_row(row) for row in frame.to_dict("records")]

    def get_lifecycle_announcements_by_date(
        self, announcement_date: date
    ) -> list[RawLifecycleAnnouncement]:
        frame = self._pro.anns_d(ann_date=format_yyyymmdd(announcement_date))
        return [lifecycle_announcement_from_row(row) for row in frame.to_dict("records")]

    def get_stock_st_by_date(self, trade_date: date) -> list[StockSTStatus]:
        frame = self._pro.stock_st(trade_date=format_yyyymmdd(trade_date))
        return [stock_st_from_row(row) for row in frame.to_dict("records")]

    def get_suspensions_by_date(self, trade_date: date) -> list[SuspensionRecord]:
        frame = self._pro.suspend_d(trade_date=format_yyyymmdd(trade_date))
        return [suspension_from_row(row) for row in frame.to_dict("records")]

    def get_name_changes(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[NameChangeRecord]:
        frame = self._pro.namechange(
            ts_code=instrument_id,
            start_date=format_yyyymmdd(start_date),
            end_date=format_yyyymmdd(end_date),
        )
        return [name_change_from_row(row) for row in frame.to_dict("records")]

    def get_index_daily(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[IndexDailyBar]:
        frame = self._pro.index_daily(
            ts_code=instrument_id,
            start_date=format_yyyymmdd(start_date),
            end_date=format_yyyymmdd(end_date),
        )
        return [index_daily_from_row(row) for row in frame.to_dict("records")]

    def get_index_metadata(self, instrument_id: str) -> list[dict[str, Any]]:
        """Read a bounded SSE index identity response without inferring its suffix."""
        frame = self._pro.index_basic(
            ts_code=instrument_id,
            market="SSE",
            fields="ts_code,name,fullname,market,publisher,base_date,list_date",
        )
        return frame.to_dict("records")

    def get_daily_price_limits_by_date(self, trade_date: date) -> list[DailyPriceLimit]:
        frame = self._pro.stk_limit(
            trade_date=format_yyyymmdd(trade_date),
            fields="ts_code,trade_date,pre_close,up_limit,down_limit,asset_type,exchange",
        )
        if len(frame) >= 5800:
            raise RuntimeError("stk_limit response reached documented row limit")
        if "asset_type" in frame:
            frame = frame[frame["asset_type"].astype(str).eq("STK")]
        return [daily_price_limit_from_row(row) for row in frame.to_dict("records")]

    def get_financial_indicators_by_period(
        self, period_end: date
    ) -> list[FinancialIndicatorObservation]:
        observed_at = datetime.now(UTC)
        frame = self._pro.fina_indicator_vip(
            period=format_yyyymmdd(period_end),
            fields=(
                "ts_code,ann_date,end_date,roe,roa,grossprofit_margin,"
                "netprofit_margin,tr_yoy,netprofit_yoy,ocf_to_or,debt_to_assets,update_flag"
            ),
        )
        return [financial_indicator_from_row(row, observed_at) for row in frame.to_dict("records")]

    def get_dividends(self, instrument_id: str) -> list[DividendObservation]:
        observed_at = datetime.now(UTC)
        frame = self._pro.dividend(
            ts_code=instrument_id,
            fields=(
                "ts_code,end_date,ann_date,div_proc,stk_div,stk_bo_rate,stk_co_rate,"
                "cash_div,cash_div_tax,record_date,ex_date,pay_date,div_listdate,"
                "imp_ann_date"
            ),
        )
        if len(frame) >= 2000:
            raise RuntimeError("dividend response reached documented row limit")
        return [dividend_from_row(row, observed_at) for row in frame.to_dict("records")]

    def probe_lifecycle_capabilities(self, probe_date: date) -> dict[str, dict[str, object]]:
        """Perform minimal API calls and return only safe capability metadata.

        Provider error text can include operational details, so it is never
        retained or surfaced.  The result intentionally contains only endpoint,
        outcome, row count and exception class.
        """

        def probe(
            endpoint: str, call, date_column: str | None, limit: int | None
        ) -> dict[str, object]:
            try:
                frame = call()
                result: dict[str, object] = {
                    "endpoint": endpoint,
                    "status": "available",
                    "row_count": int(len(frame)),
                }
                if date_column is not None:
                    if date_column not in frame.columns:
                        result["status"] = "unexpected_schema"
                        return result
                    values = [str(value) for value in frame[date_column].dropna()]
                    result["requested_date"] = format_yyyymmdd(probe_date)
                    result["response_min_date"] = min(values) if values else None
                    result["response_max_date"] = max(values) if values else None
                    result["scope_valid"] = all(
                        value == result["requested_date"] for value in values
                    )
                    if not result["scope_valid"]:
                        result["status"] = "parameter_filter_mismatch"
                if limit is not None:
                    result["limit_status"] = (
                        "potentially_truncated" if len(frame) >= limit else "below_limit"
                    )
                    if len(frame) >= limit and result["status"] == "available":
                        result["status"] = "potentially_truncated"
                return result
            except Exception as exc:  # API-specific exception types are unstable.
                message = str(exc).lower()
                if "permission" in message or "积分" in message or "权限" in message:
                    status = "permission_denied"
                else:
                    status = "error"
                return {
                    "endpoint": endpoint,
                    "status": status,
                    "error_class": type(exc).__name__,
                }

        day = format_yyyymmdd(probe_date)
        return {
            "stock_basic": probe(
                "stock_basic",
                lambda: self._pro.stock_basic(exchange="", list_status="L", fields="ts_code"),
                None,
                None,
            ),
            "stock_st": probe(
                "stock_st", lambda: self._pro.stock_st(trade_date=day), "trade_date", 1000
            ),
            "suspend_d": probe(
                "suspend_d", lambda: self._pro.suspend_d(trade_date=day), "trade_date", 5000
            ),
            "anns_d": probe("anns_d", lambda: self._pro.anns_d(ann_date=day), "ann_date", 2000),
            "namechange": probe(
                "namechange",
                lambda: self._pro.namechange(ts_code="000001.SZ", start_date=day, end_date=day),
                None,
                None,
            ),
        }
