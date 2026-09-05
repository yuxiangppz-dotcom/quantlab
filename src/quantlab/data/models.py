"""Canonical market data models and conversion helpers.

Field names here are the project's own vocabulary; provider-specific fields
(such as Tushare's ``ts_code``) never leak past this layer.
"""

from __future__ import annotations

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
