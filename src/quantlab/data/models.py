"""Canonical market data models and conversion helpers.

Field names here are the project's own vocabulary; provider-specific fields
(such as Tushare's ``ts_code``) never leak past this layer.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# Market codes used as the instrument_id suffix (e.g. "600519.SH").
SHANGHAI = "SH"
SHENZHEN = "SZ"
BEIJING = "BJ"

# Exchange identifiers (e.g. "SSE"/"SZSE"/"BSE").
SSE = "SSE"
SZSE = "SZSE"
BSE = "BSE"

EXCHANGE_BY_MARKET: dict[str, str] = {
    SHANGHAI: SSE,
    SHENZHEN: SZSE,
    BEIJING: BSE,
}

_SHANGHAI_PREFIXES = ("60", "68", "90")
_SHENZHEN_PREFIXES = ("00", "20", "30")
_BEIJING_PREFIXES = ("43", "83", "87", "92")


@dataclass(frozen=True)
class Security:
    """A-share security master entry.

    ``market`` is the market code ("SH"/"SZ"/"BJ") used as the instrument_id
    suffix, ``board`` is the listing board (e.g. 主板/创业板/科创板/北交所), and
    ``list_status`` is "L" (listed), "D" (delisted), or "P" (suspended).
    """

    instrument_id: str
    symbol: str
    name: str
    exchange: str
    market: str
    board: str
    list_status: str
    list_date: date
    delist_date: date | None


@dataclass(frozen=True)
class TradingCalendar:
    exchange: str
    trade_date: date
    is_open: bool


@dataclass(frozen=True)
class DailyBar:
    """A single daily bar.

    ``volume`` is in shares and ``amount`` is in Chinese yuan (CNY).
    """

    instrument_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class IndexDailyBar:
    """A single index daily bar (e.g. 000300.SH).

    ``instrument_id`` reuses the stock-id format ("code.market"); index codes
    never collide with A-share stock codes. ``volume`` is in shares and
    ``amount`` is in Chinese yuan (CNY), converted at the provider boundary.
    """

    instrument_id: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    pre_close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class AdjFactor:
    """A single cumulative adjustment factor (Tushare's raw cumulative factor)."""

    instrument_id: str
    trade_date: date
    adj_factor: float


@dataclass(frozen=True)
class SecurityCodeChange:
    """A historical security code change (old code -> new code).

    The old code is valid on ``[original_list_date, effective_date - 1]`` and
    the new code from ``effective_date`` onward. This describes instrument code
    validity, not an actual delisting event.
    """

    old_instrument_id: str
    new_instrument_id: str
    effective_date: date
    old_name: str
    original_list_date: date


@dataclass(frozen=True)
class DailyBasic:
    """Daily basic trading metrics for one instrument on one date.

    ``turnover_rate`` is a decimal fraction (Tushare percent -> decimal, e.g.
    5.2% -> 0.052). ``total_mv`` and ``circ_mv`` are in CNY (Tushare 万元 ->
    CNY, e.g. 123456 万元 -> 1,234,560,000).
    """

    instrument_id: str
    trade_date: date
    turnover_rate: float
    total_mv: float
    circ_mv: float


@dataclass(frozen=True)
class DailyPriceLimit:
    """Provider-reported daily price limits; never a percentage inference."""

    instrument_id: str
    trade_date: date
    pre_close: float | None
    up_limit: float
    down_limit: float
    exchange: str | None
    source: str
    source_record_id: str


@dataclass(frozen=True)
class FinancialIndicatorObservation:
    """A financial-indicator version first observed by QuantLab.

    Tushare exposes ``ann_date`` and ``update_flag`` but no revision timestamp.
    Consequently these rows are prospective evidence from ``available_from``;
    they must not be retroactively joined to historical features.
    """

    instrument_id: str
    announcement_date: date
    period_end: date
    update_flag: str | None
    roe: float | None
    roa: float | None
    gross_profit_margin: float | None
    net_profit_margin: float | None
    revenue_growth_yoy: float | None
    net_profit_growth_yoy: float | None
    operating_cashflow_to_revenue: float | None
    debt_to_assets: float | None
    observed_at: datetime
    available_from: date
    pit_status: str
    source: str
    source_record_id: str


@dataclass(frozen=True)
class DividendObservation:
    """A provider dividend/corporate-action row first observed by QuantLab."""

    instrument_id: str
    period_end: date | None
    announcement_date: date | None
    process_status: str | None
    stock_dividend_per_share: float | None
    stock_bonus_rate: float | None
    stock_conversion_rate: float | None
    cash_dividend_after_tax: float | None
    cash_dividend_before_tax: float | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    share_listing_date: date | None
    implementation_announcement_date: date | None
    observed_at: datetime
    available_from: date
    source: str
    source_record_id: str


@dataclass(frozen=True)
class RawLifecycleAnnouncement:
    """Provider-neutral, date-partitioned announcement-index record.

    The index is deliberately raw: classification is performed by the
    lifecycle service, never at the provider boundary.  ``raw_payload`` is a
    stable JSON string retained for audit and replay rather than a provider
    object that could change shape between client versions.
    """

    source: str
    source_record_id: str
    instrument_id: str | None
    announcement_date: date
    announcement_time: str | None
    title: str
    source_url: str | None
    raw_payload: str
    content_fingerprint: str


@dataclass(frozen=True)
class SecurityLifecycleEvent:
    """Immutable canonical lifecycle event usable by PIT consumers.

    v0 only emits ``termination_decision``.  Other lifecycle categories remain
    classification context and never drive the risk overlay.
    """

    event_id: str
    instrument_id: str
    event_type: str
    event_date: date
    event_time: str | None
    available_from: date
    effective_date: date | None
    source: str
    source_record_id: str
    source_url: str | None
    raw_title: str
    verification_status: str
    classification_reason: str
    content_fingerprint: str


@dataclass(frozen=True)
class StockSTStatus:
    """Raw ST status context; it is never itself a liquidation trigger."""

    instrument_id: str
    trade_date: date
    name: str | None
    status: str | None
    type_name: str | None
    source_record_id: str


@dataclass(frozen=True)
class SuspensionRecord:
    """A raw daily Tushare ``suspend_d`` fact, never an inferred interval."""

    instrument_id: str
    trade_date: date
    suspend_type: str
    suspend_timing: str | None
    source_record_id: str


@dataclass(frozen=True)
class NameChangeRecord:
    """Provider-supplied name history retained solely as lifecycle context."""

    instrument_id: str
    start_date: date
    end_date: date | None
    name: str | None
    change_reason: str | None
    source_record_id: str


def canonical_payload_fingerprint(payload: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 fingerprint for a provider payload."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def market_from_symbol(symbol: str) -> str:
    """Return the market code ("SH"/"SZ"/"BJ") for a 6-digit A-share symbol."""
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"Invalid A-share symbol: {symbol!r}")
    prefix = symbol[:2]
    if prefix in _SHANGHAI_PREFIXES:
        return SHANGHAI
    if prefix in _SHENZHEN_PREFIXES:
        return SHENZHEN
    if prefix in _BEIJING_PREFIXES:
        return BEIJING
    raise ValueError(f"Unsupported A-share symbol: {symbol!r}")


def to_instrument_id(symbol: str, market: str | None = None) -> str:
    """Build a canonical instrument_id such as "600519.SH"."""
    resolved_market = market or market_from_symbol(symbol)
    if resolved_market not in EXCHANGE_BY_MARKET:
        raise ValueError(f"Invalid market code: {resolved_market!r}")
    return f"{symbol}.{resolved_market}"


def parse_instrument_id(instrument_id: str) -> tuple[str, str]:
    """Split "600519.SH" into ("600519", "SH")."""
    symbol, separator, market = instrument_id.partition(".")
    if not separator or not symbol or market not in EXCHANGE_BY_MARKET:
        raise ValueError(f"Invalid instrument_id: {instrument_id!r}")
    return symbol, market


class DataValidationError(ValueError):
    """Raised when a required field is missing or invalid."""


def parse_yyyymmdd(value: Any) -> date | None:
    """Parse an optional "YYYYMMDD" date; return None for missing/empty values."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return datetime.strptime(text, "%Y%m%d").date()
    raise ValueError(f"Cannot parse date value: {value!r}")


def parse_required_yyyymmdd(value: Any) -> date:
    """Parse a required "YYYYMMDD" date; raise if missing or invalid."""
    try:
        parsed = parse_yyyymmdd(value)
    except (ValueError, TypeError) as exc:
        raise DataValidationError(f"Invalid required date: {value!r}") from exc
    if parsed is None:
        raise DataValidationError(f"Required date is missing: {value!r}")
    return parsed


def format_yyyymmdd(value: date) -> str:
    """Format a date as "YYYYMMDD" (the Tushare API date format)."""
    return value.strftime("%Y%m%d")
