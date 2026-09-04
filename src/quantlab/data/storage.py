"""Local Parquet storage for market data."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from quantlab.data.models import DailyBar, Security, TradingCalendar


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


_SECURITY_COLUMNS = [
    "instrument_id",
    "symbol",
    "name",
    "exchange",
    "market",
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
            list_date=_to_date(row["list_date"]),
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
            trade_date=_to_date(row["trade_date"]),
            is_open=bool(row["is_open"]),
        )
        for row in frame.to_dict("records")
    ]


def _bars_to_frame(bars: list[DailyBar]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in bars], columns=_BAR_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _frame_to_bars(frame: pd.DataFrame) -> list[DailyBar]:
    return [
        DailyBar(
            instrument_id=row["instrument_id"],
            trade_date=_to_date(row["trade_date"]),
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


class ParquetStorage:
    """Store canonical market data as local Parquet files.

    Layout::

        data/raw/tushare/securities/securities.parquet
        data/raw/tushare/calendar/calendar.parquet
        data/raw/tushare/daily/{instrument_id}.parquet

    Dates are stored as ``datetime64[ns]`` and returned as ``datetime.date``.
    """

    def __init__(self, base_dir: str | Path = "data/raw/tushare") -> None:
        self.base_dir = Path(base_dir)

    @property
    def securities_path(self) -> Path:
        return self.base_dir / "securities" / "securities.parquet"

    @property
    def calendar_path(self) -> Path:
        return self.base_dir / "calendar" / "calendar.parquet"

    @property
    def daily_dir(self) -> Path:
        return self.base_dir / "daily"

    def save_securities(self, securities: list[Security]) -> Path:
        frame = _securities_to_frame(securities)
        _ensure_unique(frame, ["instrument_id"])
        return self._write(frame, self.securities_path)

    def load_securities(self) -> list[Security]:
        if not self.securities_path.exists():
            return []
        return _frame_to_securities(pd.read_parquet(self.securities_path))

    def save_trading_calendar(self, calendar: list[TradingCalendar]) -> Path:
        frame = _calendar_to_frame(calendar)
        _ensure_unique(frame, ["exchange", "trade_date"])
        return self._write(frame, self.calendar_path)

    def load_trading_calendar(self) -> list[TradingCalendar]:
        if not self.calendar_path.exists():
            return []
        return _frame_to_calendar(pd.read_parquet(self.calendar_path))

    def save_daily_bars(self, bars: list[DailyBar]) -> list[Path]:
        frame = _bars_to_frame(bars)
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        paths: list[Path] = []
        for instrument_id, group in frame.groupby("instrument_id", sort=False):
            path = self.daily_dir / f"{instrument_id}.parquet"
            if path.exists():
                existing = pd.read_parquet(path)
                merged = pd.concat([existing, group], ignore_index=True)
                merged = merged.drop_duplicates()
                _ensure_unique(merged, ["instrument_id", "trade_date"])
            else:
                merged = group
            self._write(merged, path)
            paths.append(path)
        return paths

    def load_daily_bars(self, instrument_ids: list[str] | None = None) -> list[DailyBar]:
        files = sorted(self.daily_dir.glob("*.parquet"))
        frames = [pd.read_parquet(path) for path in files]
        if not frames:
            return []
        frame = pd.concat(frames, ignore_index=True)
        if instrument_ids:
            frame = frame[frame["instrument_id"].isin(instrument_ids)]
        return _frame_to_bars(frame)

    @staticmethod
    def _write(frame: pd.DataFrame, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
        return path
