"""Canonical records retained for Scout; compatible with QuantLab Parquet data."""

from dataclasses import dataclass
from datetime import date


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


class DataValidationError(ValueError):
    """Raised when a required field is missing or invalid."""
