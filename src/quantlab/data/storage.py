"""Local Parquet storage for market data."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.data.models import AdjFactor, DailyBar, DataValidationError, Security, TradingCalendar


class DuplicateDataError(Exception):
    """Raised when duplicate rows are detected for a dataset's key columns."""


def find_duplicates(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Return rows whose values on ``keys`` appear more than once."""
    return frame[frame.duplicated(subset=keys, keep=False)]


def _ensure_unique(frame: pd.DataFrame, keys: list[str]) -> None:
    duplicates = find_duplicates(frame, keys)
    if not duplicates.empty:
        sample = duplicates[keys].drop_duplicates().head(5).to_dict("records")
        raise DuplicateDataError(f"Duplicate rows detected on {keys}: {sample}")


def _to_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _to_required_date(value: Any) -> date:
    result = _to_date(value)
    if result is None:
        raise DataValidationError(f"Required date is missing: {value!r}")
    return result


_SECURITY_COLUMNS = [
    "instrument_id",
    "symbol",
    "name",
    "exchange",
    "market",
    "board",
    "list_status",
    "list_date",
    "delist_date",
]
_CALENDAR_COLUMNS = ["exchange", "trade_date", "is_open"]
_BAR_COLUMNS = [
    "instrument_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
]
_ADJ_COLUMNS = ["instrument_id", "trade_date", "adj_factor"]


def _securities_to_frame(securities: list[Security]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in securities], columns=_SECURITY_COLUMNS)
    frame["list_date"] = pd.to_datetime(frame["list_date"], errors="coerce")
    frame["delist_date"] = pd.to_datetime(frame["delist_date"], errors="coerce")
    return frame


def _frame_to_securities(frame: pd.DataFrame) -> list[Security]:
    return [
        Security(
            instrument_id=row["instrument_id"],
            symbol=row["symbol"],
            name=row["name"],
            exchange=row["exchange"],
            market=row["market"],
            board=row["board"],
            list_status=row["list_status"],
            list_date=_to_required_date(row["list_date"]),
            delist_date=_to_date(row["delist_date"]),
        )
        for row in frame.to_dict("records")
    ]


def _calendar_to_frame(calendar: list[TradingCalendar]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in calendar], columns=_CALENDAR_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _frame_to_calendar(frame: pd.DataFrame) -> list[TradingCalendar]:
    return [
        TradingCalendar(
            exchange=row["exchange"],
            trade_date=_to_required_date(row["trade_date"]),
            is_open=bool(row["is_open"]),
        )
        for row in frame.to_dict("records")
    ]


def _merge_calendar(
    existing: list[TradingCalendar],
    incoming: list[TradingCalendar],
) -> list[TradingCalendar]:
    by_key = {(item.exchange, item.trade_date): item for item in existing}
    for item in incoming:
        by_key[(item.exchange, item.trade_date)] = item  # incoming wins
    return list(by_key.values())


def _bars_to_frame(bars: list[DailyBar]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in bars], columns=_BAR_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _frame_to_bars(frame: pd.DataFrame) -> list[DailyBar]:
    return [
        DailyBar(
            instrument_id=row["instrument_id"],
            trade_date=_to_required_date(row["trade_date"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            pre_close=float(row["pre_close"]),
            volume=float(row["volume"]),
            amount=float(row["amount"]),
        )
        for row in frame.to_dict("records")
    ]


def _group_bars_by_date(bars: list[DailyBar]) -> list[tuple[date, list[DailyBar]]]:
    grouped: dict[date, list[DailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.trade_date, []).append(bar)
    return sorted(grouped.items())


def _adj_factors_to_frame(factors: list[AdjFactor]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in factors], columns=_ADJ_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _frame_to_adj_factors(frame: pd.DataFrame) -> list[AdjFactor]:
    return [
        AdjFactor(
            instrument_id=row["instrument_id"],
            trade_date=_to_required_date(row["trade_date"]),
            adj_factor=float(row["adj_factor"]),
        )
        for row in frame.to_dict("records")
    ]


class ParquetStorage:
    """Store canonical market data as local Parquet files.

    Layout::

        data/canonical/securities/securities.parquet
        data/canonical/calendar/calendar.parquet
        data/canonical/daily/year={year}/month={month}/{trade_date}.parquet
        data/canonical/adj_factor/year={year}/month={month}/{trade_date}.parquet

    Daily bars and adj factors are stored one file per trading date, sorted
    by instrument_id. Dates are stored as ``datetime64[ns]`` and returned as
    ``datetime.date``.
    """

    def __init__(self, base_dir: str | Path = "data/canonical") -> None:
        self.base_dir = Path(base_dir)

    @property
    def securities_path(self) -> Path:
        return self.base_dir / "securities" / "securities.parquet"

    @property
    def calendar_path(self) -> Path:
        return self.base_dir / "calendar" / "calendar.parquet"

    def daily_bars_path(self, trade_date: date) -> Path:
        return (
            self.base_dir / "daily"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def daily_bars_exists(self, trade_date: date) -> bool:
        return self.daily_bars_path(trade_date).exists()

    def save_securities(self, securities: list[Security]) -> Path:
        frame = _securities_to_frame(securities)
        _ensure_unique(frame, ["instrument_id"])
        return self._write(frame, self.securities_path)

    def load_securities(self) -> list[Security]:
        if not self.securities_path.exists():
            return []
        return _frame_to_securities(pd.read_parquet(self.securities_path))

    def securities_exists(self) -> bool:
        return self.securities_path.exists()

    def save_trading_calendar(self, calendar: list[TradingCalendar]) -> Path:
        frame = _calendar_to_frame(calendar)
        _ensure_unique(frame, ["exchange", "trade_date"])
        return self._write(frame, self.calendar_path)

    def upsert_trading_calendar(self, calendar: list[TradingCalendar]) -> Path:
        """Merge incoming calendar into the existing one (incoming wins per key).

        Keys are (exchange, trade_date); the result is always sorted by
        trade_date then exchange and written atomically.
        """
        merged = (
            calendar
            if not self.calendar_path.exists()
            else _merge_calendar(self.load_trading_calendar(), calendar)
        )
        frame = _calendar_to_frame(merged)
        _ensure_unique(frame, ["exchange", "trade_date"])
        frame = frame.sort_values(["trade_date", "exchange"])
        return self._write(frame, self.calendar_path)

    def load_trading_calendar(self) -> list[TradingCalendar]:
        if not self.calendar_path.exists():
            return []
        return _frame_to_calendar(pd.read_parquet(self.calendar_path))

    def trading_calendar_exists(self) -> bool:
        return self.calendar_path.exists()

    def save_daily_bars(self, bars: list[DailyBar]) -> list[Path]:
        """Group bars by trade_date and write one file per date."""
        paths: list[Path] = []
        for trade_date, group in _group_bars_by_date(bars):
            paths.append(self.save_daily_bars_by_date(group, trade_date))
        return paths

    def save_daily_bars_by_date(self, bars: list[DailyBar], trade_date: date) -> Path:
        frame = _bars_to_frame(bars)
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        frame = frame.sort_values("instrument_id")
        return self._write(frame, self.daily_bars_path(trade_date))

    def load_daily_bars_by_date(self, trade_date: date) -> list[DailyBar]:
        path = self.daily_bars_path(trade_date)
        if not path.exists():
            return []
        return _frame_to_bars(pd.read_parquet(path))

    def adj_factor_path(self, trade_date: date) -> Path:
        return (
            self.base_dir / "adj_factor"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def adj_factor_exists(self, trade_date: date) -> bool:
        return self.adj_factor_path(trade_date).exists()

    def save_adj_factors_by_date(self, factors: list[AdjFactor], trade_date: date) -> Path:
        frame = _adj_factors_to_frame(factors)
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        frame = frame.sort_values("instrument_id")
        return self._write(frame, self.adj_factor_path(trade_date))

    def load_adj_factors_by_date(self, trade_date: date) -> list[AdjFactor]:
        path = self.adj_factor_path(trade_date)
        if not path.exists():
            return []
        return _frame_to_adj_factors(pd.read_parquet(path))

    @staticmethod
    def _write(frame: pd.DataFrame, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".tmp"
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            frame.to_parquet(temp_path, index=False)
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return path
