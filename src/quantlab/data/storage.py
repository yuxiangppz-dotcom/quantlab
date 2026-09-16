"""Local Parquet storage for market data."""

from __future__ import annotations

import os
import tempfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.data.models import (
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
    SecurityLifecycleEvent,
    StockSTStatus,
    SuspensionRecord,
    TradingCalendar,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


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


def _none_if_na(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(value)


def _float_or_none(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


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
_INDEX_DAILY_COLUMNS = [
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
_DAILY_BASIC_COLUMNS = [
    "instrument_id",
    "trade_date",
    "turnover_rate",
    "total_mv",
    "circ_mv",
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


def _merge_securities(
    existing: list[Security],
    incoming: list[Security],
) -> list[Security]:
    by_id = {item.instrument_id: item for item in existing}
    for item in incoming:
        by_id[item.instrument_id] = item  # incoming wins
    return list(by_id.values())


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


def _daily_basic_to_frame(items: list[DailyBasic]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in items], columns=_DAILY_BASIC_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _index_daily_to_frame(bars: list[IndexDailyBar]) -> pd.DataFrame:
    frame = pd.DataFrame([asdict(item) for item in bars], columns=_INDEX_DAILY_COLUMNS)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _frame_to_index_daily(frame: pd.DataFrame) -> list[IndexDailyBar]:
    return [
        IndexDailyBar(
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


def _frame_to_daily_basic(frame: pd.DataFrame) -> list[DailyBasic]:
    return [
        DailyBasic(
            instrument_id=row["instrument_id"],
            trade_date=_to_required_date(row["trade_date"]),
            turnover_rate=float(row["turnover_rate"]),
            total_mv=float(row["total_mv"]),
            circ_mv=float(row["circ_mv"]),
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
        data/canonical/index_daily/year={year}/month={month}/{trade_date}.parquet

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
            self.base_dir
            / "daily"
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

    def upsert_securities(self, securities: list[Security]) -> Path:
        """Merge incoming securities into the existing ones (incoming wins per id).

        Historical securities not present in ``incoming`` are preserved; the
        result is sorted by instrument_id and written atomically.
        """
        merged = (
            securities
            if not self.securities_path.exists()
            else _merge_securities(self.load_securities(), securities)
        )
        frame = _securities_to_frame(merged)
        _ensure_unique(frame, ["instrument_id"])
        frame = frame.sort_values("instrument_id")
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
            self.base_dir
            / "adj_factor"
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

    def daily_basic_path(self, trade_date: date) -> Path:
        return (
            self.base_dir
            / "daily_basic"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def daily_price_limit_path(self, trade_date: date) -> Path:
        return (
            self.base_dir
            / "daily_price_limit"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def daily_price_limit_exists(self, trade_date: date) -> bool:
        return self.daily_price_limit_path(trade_date).exists()

    def save_daily_price_limits_by_date(
        self, items: list[DailyPriceLimit], trade_date: date
    ) -> Path:
        frame = pd.DataFrame(
            [asdict(item) for item in items], columns=list(DailyPriceLimit.__dataclass_fields__)
        )
        if frame.empty:
            raise DataValidationError("daily price-limit payload is empty")
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        if set(pd.to_datetime(frame["trade_date"]).dt.date) != {trade_date}:
            raise DataValidationError("daily price-limit rows do not match partition date")
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        frame = frame.sort_values("instrument_id").reset_index(drop=True)
        path = self.daily_price_limit_path(trade_date)
        if path.exists():
            existing = pd.read_parquet(path).sort_values("instrument_id").reset_index(drop=True)
            if existing.to_json(date_format="iso") != frame.to_json(date_format="iso"):
                raise DataValidationError(
                    "daily price-limit partition differs from immutable stored fact"
                )
            return path
        return self._write(frame, path)

    def load_daily_price_limits_by_date(self, trade_date: date) -> list[DailyPriceLimit]:
        path = self.daily_price_limit_path(trade_date)
        if not path.exists():
            return []
        return [
            DailyPriceLimit(
                instrument_id=row["instrument_id"],
                trade_date=_to_required_date(row["trade_date"]),
                pre_close=(None if pd.isna(row["pre_close"]) else float(row["pre_close"])),
                up_limit=float(row["up_limit"]),
                down_limit=float(row["down_limit"]),
                exchange=_none_if_na(row["exchange"]),
                source=row["source"],
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(path).to_dict("records")
        ]

    def _observation_snapshot_path(self, dataset: str, observed_on: date, fingerprint: str) -> Path:
        return (
            self.base_dir
            / dataset
            / f"observed_on={observed_on.isoformat()}"
            / f"snapshot={fingerprint}.parquet"
        )

    @staticmethod
    def _record_set_fingerprint(items: list[object]) -> str:
        import hashlib
        import json

        record_ids = sorted(str(item.source_record_id) for item in items)
        return hashlib.sha256(json.dumps(record_ids, separators=(",", ":")).encode()).hexdigest()

    def save_financial_indicator_observations(
        self, items: list[FinancialIndicatorObservation]
    ) -> Path:
        if not items:
            raise DataValidationError("financial indicator observation is empty")
        observed_on = min(item.observed_at.astimezone(_SHANGHAI).date() for item in items)
        if {item.observed_at.astimezone(_SHANGHAI).date() for item in items} != {observed_on}:
            raise DataValidationError("financial observation batch spans multiple dates")
        fingerprint = self._record_set_fingerprint(items)
        path = self._observation_snapshot_path("financial_indicator", observed_on, fingerprint)
        if path.exists():
            return path
        frame = pd.DataFrame(
            [asdict(item) for item in items],
            columns=list(FinancialIndicatorObservation.__dataclass_fields__),
        )
        _ensure_unique(frame, ["source_record_id"])
        frame["announcement_date"] = pd.to_datetime(frame["announcement_date"])
        frame["period_end"] = pd.to_datetime(frame["period_end"])
        frame["available_from"] = pd.to_datetime(frame["available_from"])
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        return self._write(frame.sort_values(["instrument_id", "announcement_date"]), path)

    def load_financial_indicator_observations(self) -> list[FinancialIndicatorObservation]:
        paths = sorted(self.base_dir.glob("financial_indicator/observed_on=*/snapshot=*.parquet"))
        if not paths:
            return []
        frame = pd.concat((pd.read_parquet(path) for path in paths), ignore_index=True)
        frame = frame.sort_values("observed_at").drop_duplicates("source_record_id", keep="first")
        return [
            FinancialIndicatorObservation(
                instrument_id=row["instrument_id"],
                announcement_date=_to_required_date(row["announcement_date"]),
                period_end=_to_required_date(row["period_end"]),
                update_flag=_none_if_na(row["update_flag"]),
                roe=_float_or_none(row["roe"]),
                roa=_float_or_none(row["roa"]),
                gross_profit_margin=_float_or_none(row["gross_profit_margin"]),
                net_profit_margin=_float_or_none(row["net_profit_margin"]),
                revenue_growth_yoy=_float_or_none(row["revenue_growth_yoy"]),
                net_profit_growth_yoy=_float_or_none(row["net_profit_growth_yoy"]),
                operating_cashflow_to_revenue=_float_or_none(row["operating_cashflow_to_revenue"]),
                debt_to_assets=_float_or_none(row["debt_to_assets"]),
                observed_at=pd.Timestamp(row["observed_at"]).to_pydatetime(),
                available_from=_to_required_date(row["available_from"]),
                pit_status=row["pit_status"],
                source=row["source"],
                source_record_id=row["source_record_id"],
            )
            for row in frame.to_dict("records")
        ]

    def save_dividend_observations(self, items: list[DividendObservation]) -> Path:
        if not items:
            raise DataValidationError("dividend observation is empty")
        observed_on = min(item.observed_at.astimezone(_SHANGHAI).date() for item in items)
        if {item.observed_at.astimezone(_SHANGHAI).date() for item in items} != {observed_on}:
            raise DataValidationError("dividend observation batch spans multiple dates")
        fingerprint = self._record_set_fingerprint(items)
        path = self._observation_snapshot_path("dividend", observed_on, fingerprint)
        if path.exists():
            return path
        frame = pd.DataFrame(
            [asdict(item) for item in items], columns=list(DividendObservation.__dataclass_fields__)
        )
        _ensure_unique(frame, ["source_record_id"])
        for column in (
            "period_end",
            "announcement_date",
            "record_date",
            "ex_date",
            "pay_date",
            "share_listing_date",
            "implementation_announcement_date",
            "available_from",
        ):
            frame[column] = pd.to_datetime(frame[column])
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        return self._write(frame.sort_values(["instrument_id", "announcement_date"]), path)

    def load_dividend_observations(self) -> list[DividendObservation]:
        paths = sorted(self.base_dir.glob("dividend/observed_on=*/snapshot=*.parquet"))
        if not paths:
            return []
        frame = pd.concat((pd.read_parquet(path) for path in paths), ignore_index=True)
        frame = frame.sort_values("observed_at").drop_duplicates("source_record_id", keep="first")
        return [
            DividendObservation(
                instrument_id=row["instrument_id"],
                period_end=_to_date(row["period_end"]),
                announcement_date=_to_date(row["announcement_date"]),
                process_status=_none_if_na(row["process_status"]),
                stock_dividend_per_share=_float_or_none(row["stock_dividend_per_share"]),
                stock_bonus_rate=_float_or_none(row["stock_bonus_rate"]),
                stock_conversion_rate=_float_or_none(row["stock_conversion_rate"]),
                cash_dividend_after_tax=_float_or_none(row["cash_dividend_after_tax"]),
                cash_dividend_before_tax=_float_or_none(row["cash_dividend_before_tax"]),
                record_date=_to_date(row["record_date"]),
                ex_date=_to_date(row["ex_date"]),
                pay_date=_to_date(row["pay_date"]),
                share_listing_date=_to_date(row["share_listing_date"]),
                implementation_announcement_date=_to_date(row["implementation_announcement_date"]),
                observed_at=pd.Timestamp(row["observed_at"]).to_pydatetime(),
                available_from=_to_required_date(row["available_from"]),
                source=row["source"],
                source_record_id=row["source_record_id"],
            )
            for row in frame.to_dict("records")
        ]

    def daily_basic_exists(self, trade_date: date) -> bool:
        return self.daily_basic_path(trade_date).exists()

    def lifecycle_announcements_path(self, announcement_date: date) -> Path:
        return (
            self.base_dir
            / "lifecycle_raw"
            / "announcements"
            / f"year={announcement_date.year}"
            / f"month={announcement_date.month:02d}"
            / f"{announcement_date.isoformat()}.parquet"
        )

    def lifecycle_announcements_exists(self, announcement_date: date) -> bool:
        return self.lifecycle_announcements_path(announcement_date).exists()

    def save_lifecycle_announcements_by_date(
        self, items: list[RawLifecycleAnnouncement], announcement_date: date
    ) -> Path:
        columns = list(RawLifecycleAnnouncement.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["announcement_date"] = pd.to_datetime(frame["announcement_date"])
            _ensure_unique(frame, ["source", "source_record_id"])
            frame = frame.sort_values(["source", "source_record_id"])
        return self._write(frame, self.lifecycle_announcements_path(announcement_date))

    def load_lifecycle_announcements_by_date(
        self, announcement_date: date
    ) -> list[RawLifecycleAnnouncement]:
        path = self.lifecycle_announcements_path(announcement_date)
        if not path.exists():
            return []
        return [
            RawLifecycleAnnouncement(
                source=row["source"],
                source_record_id=row["source_record_id"],
                instrument_id=_none_if_na(row["instrument_id"]),
                announcement_date=_to_required_date(row["announcement_date"]),
                announcement_time=_none_if_na(row["announcement_time"]),
                title=row["title"],
                source_url=_none_if_na(row["source_url"]),
                raw_payload=row["raw_payload"],
                content_fingerprint=row["content_fingerprint"],
            )
            for row in pd.read_parquet(path).to_dict("records")
        ]

    @property
    def lifecycle_events_path(self) -> Path:
        return self.base_dir / "lifecycle_events" / "events.parquet"

    def save_lifecycle_events(self, events: list[SecurityLifecycleEvent]) -> Path:
        columns = list(SecurityLifecycleEvent.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in events], columns=columns)
        if not frame.empty:
            for column in ("event_date", "available_from", "effective_date"):
                frame[column] = pd.to_datetime(frame[column])
            _ensure_unique(frame, ["event_id"])
            frame = frame.sort_values(["available_from", "event_id"])
        return self._write(frame, self.lifecycle_events_path)

    def load_lifecycle_events(self) -> list[SecurityLifecycleEvent]:
        if not self.lifecycle_events_path.exists():
            return []
        return [
            SecurityLifecycleEvent(
                event_id=row["event_id"],
                instrument_id=row["instrument_id"],
                event_type=row["event_type"],
                event_date=_to_required_date(row["event_date"]),
                event_time=_none_if_na(row["event_time"]),
                available_from=_to_required_date(row["available_from"]),
                effective_date=_to_date(row["effective_date"]),
                source=row["source"],
                source_record_id=row["source_record_id"],
                source_url=_none_if_na(row["source_url"]),
                raw_title=row["raw_title"],
                verification_status=row["verification_status"],
                classification_reason=row["classification_reason"],
                content_fingerprint=row["content_fingerprint"],
            )
            for row in pd.read_parquet(self.lifecycle_events_path).to_dict("records")
        ]

    @property
    def stock_st_path(self) -> Path:
        """Deprecated v0 path; never used as trusted v0.1.1 context input."""
        return self.base_dir / "lifecycle_context" / "stock_st.parquet"

    @property
    def suspensions_path(self) -> Path:
        """Deprecated v0 path; never used as trusted v0.1.1 context input."""
        return self.base_dir / "lifecycle_context" / "suspensions.parquet"

    def stock_st_v1_path(self, trade_date: date) -> Path:
        return (
            self.base_dir
            / "lifecycle_context_v1"
            / "stock_st"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def suspensions_v1_path(self, trade_date: date) -> Path:
        return (
            self.base_dir
            / "lifecycle_context_v1"
            / "suspensions"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def stock_st_v1_exists(self, trade_date: date) -> bool:
        return self.stock_st_v1_path(trade_date).exists()

    def suspensions_v1_exists(self, trade_date: date) -> bool:
        return self.suspensions_v1_path(trade_date).exists()

    def save_stock_st_v1_by_date(self, items: list[StockSTStatus], trade_date: date) -> Path:
        columns = list(StockSTStatus.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            _ensure_unique(frame, ["instrument_id", "trade_date", "source_record_id"])
            frame = frame.sort_values(["instrument_id", "source_record_id"])
        return self._write(frame, self.stock_st_v1_path(trade_date))

    def load_stock_st_v1_by_date(self, trade_date: date) -> list[StockSTStatus]:
        path = self.stock_st_v1_path(trade_date)
        if not path.exists():
            return []
        return [
            StockSTStatus(
                instrument_id=row["instrument_id"],
                trade_date=_to_required_date(row["trade_date"]),
                name=_none_if_na(row["name"]),
                status=_none_if_na(row["status"]),
                type_name=_none_if_na(row["type_name"]),
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(path).to_dict("records")
        ]

    def save_suspensions_v1_by_date(self, items: list[SuspensionRecord], trade_date: date) -> Path:
        columns = list(SuspensionRecord.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            _ensure_unique(frame, ["instrument_id", "trade_date", "source_record_id"])
            frame = frame.sort_values(["instrument_id", "source_record_id"])
        return self._write(frame, self.suspensions_v1_path(trade_date))

    def load_suspensions_v1_by_date(self, trade_date: date) -> list[SuspensionRecord]:
        path = self.suspensions_v1_path(trade_date)
        if not path.exists():
            return []
        return [
            SuspensionRecord(
                instrument_id=row["instrument_id"],
                trade_date=_to_required_date(row["trade_date"]),
                suspend_type=row["suspend_type"],
                suspend_timing=_none_if_na(row["suspend_timing"]),
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(path).to_dict("records")
        ]

    @property
    def name_changes_v1_path(self) -> Path:
        return self.base_dir / "lifecycle_context_v1" / "name_changes.parquet"

    def save_name_changes_v1(self, items: list[NameChangeRecord]) -> Path:
        columns = list(NameChangeRecord.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["start_date"] = pd.to_datetime(frame["start_date"])
            frame["end_date"] = pd.to_datetime(frame["end_date"])
            _ensure_unique(frame, ["instrument_id", "start_date", "source_record_id"])
            frame = frame.sort_values(["instrument_id", "start_date", "source_record_id"])
        return self._write(frame, self.name_changes_v1_path)

    def load_name_changes_v1(self) -> list[NameChangeRecord]:
        if not self.name_changes_v1_path.exists():
            return []
        return [
            NameChangeRecord(
                instrument_id=row["instrument_id"],
                start_date=_to_required_date(row["start_date"]),
                end_date=_to_date(row["end_date"]),
                name=_none_if_na(row["name"]),
                change_reason=_none_if_na(row["change_reason"]),
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(self.name_changes_v1_path).to_dict("records")
        ]

    def save_stock_st(self, items: list[StockSTStatus]) -> Path:
        columns = list(StockSTStatus.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            _ensure_unique(frame, ["instrument_id", "trade_date", "source_record_id"])
            frame = frame.sort_values(["instrument_id", "trade_date", "source_record_id"])
        return self._write(frame, self.stock_st_path)

    def load_stock_st(self) -> list[StockSTStatus]:
        if not self.stock_st_path.exists():
            return []
        return [
            StockSTStatus(
                instrument_id=row["instrument_id"],
                trade_date=_to_required_date(row["trade_date"]),
                name=_none_if_na(row["name"]),
                status=_none_if_na(row["status"]),
                type_name=None,
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(self.stock_st_path).to_dict("records")
        ]

    def save_suspensions(self, items: list[SuspensionRecord]) -> Path:
        columns = list(SuspensionRecord.__dataclass_fields__)
        frame = pd.DataFrame([asdict(item) for item in items], columns=columns)
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            _ensure_unique(frame, ["instrument_id", "trade_date", "source_record_id"])
            frame = frame.sort_values(["instrument_id", "trade_date", "source_record_id"])
        return self._write(frame, self.suspensions_path)

    def load_suspensions(self) -> list[SuspensionRecord]:
        if not self.suspensions_path.exists():
            return []
        return [
            SuspensionRecord(
                instrument_id=row["instrument_id"],
                trade_date=_to_required_date(row["trade_date"]),
                suspend_type=row["suspend_type"],
                suspend_timing=_none_if_na(row["suspend_timing"]),
                source_record_id=row["source_record_id"],
            )
            for row in pd.read_parquet(self.suspensions_path).to_dict("records")
        ]

    def save_daily_basic_by_date(self, items: list[DailyBasic], trade_date: date) -> Path:
        frame = _daily_basic_to_frame(items)
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        frame = frame.sort_values("instrument_id")
        return self._write(frame, self.daily_basic_path(trade_date))

    def load_daily_basic_by_date(self, trade_date: date) -> list[DailyBasic]:
        path = self.daily_basic_path(trade_date)
        if not path.exists():
            return []
        return _frame_to_daily_basic(pd.read_parquet(path))

    def index_daily_path(self, trade_date: date) -> Path:
        return (
            self.base_dir
            / "index_daily"
            / f"year={trade_date.year}"
            / f"month={trade_date.month:02d}"
            / f"{trade_date.isoformat()}.parquet"
        )

    def index_daily_exists(self, trade_date: date) -> bool:
        return self.index_daily_path(trade_date).exists()

    def save_index_daily_by_date(self, bars: list[IndexDailyBar], trade_date: date) -> Path:
        frame = _index_daily_to_frame(bars)
        _ensure_unique(frame, ["instrument_id", "trade_date"])
        frame = frame.sort_values("instrument_id")
        return self._write(frame, self.index_daily_path(trade_date))

    def load_index_daily_by_date(self, trade_date: date) -> list[IndexDailyBar]:
        path = self.index_daily_path(trade_date)
        if not path.exists():
            return []
        return _frame_to_index_daily(pd.read_parquet(path))

    @staticmethod
    def _write(frame: pd.DataFrame, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            frame.to_parquet(temp_path, index=False)
            with temp_path.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return path
