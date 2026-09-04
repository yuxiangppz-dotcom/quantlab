"""Historical daily-bar sync service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import DailyBar, DataValidationError
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync run."""

    total: int
    synced: int
    skipped: int
    filtered: int


def validate_daily_bars(bars: list[DailyBar], expected_date: date) -> None:
    """Validate one trading day's bars; raise :class:`DataValidationError` on failure."""
    if not bars:
        raise DataValidationError(f"No daily bars for {expected_date}")

    seen: set[tuple[str, date]] = set()
    for bar in bars:
        if bar.trade_date != expected_date:
            raise DataValidationError(
                f"Unexpected trade_date {bar.trade_date} (expected {expected_date}) "
                f"for {bar.instrument_id}"
            )
        key = (bar.instrument_id, bar.trade_date)
        if key in seen:
            raise DataValidationError(
                f"Duplicate bar for {bar.instrument_id} on {bar.trade_date}"
            )
        seen.add(key)

        for field_name, value in (
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
            ("pre_close", bar.pre_close),
        ):
            if value is None or not math.isfinite(value) or value <= 0:
                raise DataValidationError(
                    f"Invalid {field_name} for {bar.instrument_id} on {bar.trade_date}"
                )

        for field_name, value in (("volume", bar.volume), ("amount", bar.amount)):
            if value is None or not math.isfinite(value) or value < 0:
                raise DataValidationError(
                    f"Invalid {field_name} for {bar.instrument_id} on {bar.trade_date}"
                )

        if bar.high < bar.open or bar.high < bar.close or bar.high < bar.low:
            raise DataValidationError(
                f"Invalid high for {bar.instrument_id} on {bar.trade_date}"
            )
        if bar.low > bar.open or bar.low > bar.close:
            raise DataValidationError(
                f"Invalid low for {bar.instrument_id} on {bar.trade_date}"
            )


def _open_trade_dates(provider: DataProvider, start_date: date, end_date: date) -> list[date]:
    calendar = provider.get_trading_calendar(start_date, end_date)
    return sorted({entry.trade_date for entry in calendar if entry.is_open})


def _build_list_dates(provider: DataProvider) -> dict[str, date]:
    securities = provider.get_securities()
    return {security.instrument_id: security.list_date for security in securities}


def filter_unlisted_placeholders(
    bars: list[DailyBar],
    list_dates: dict[str, date],
    trade_date: date,
) -> tuple[list[DailyBar], int]:
    """Filter out bars for securities not yet listed on ``trade_date``.

    A bar is filtered only when its ``instrument_id`` is present in
    ``list_dates`` and ``trade_date < list_date``. Missing ``pre_close`` is an
    anomaly signal, not a filtering criterion. Returns ``(kept, filtered)``.
    """
    kept: list[DailyBar] = []
    filtered = 0
    for bar in bars:
        list_date = list_dates.get(bar.instrument_id)
        if list_date is not None and trade_date < list_date:
            filtered += 1
        else:
            kept.append(bar)
    return kept, filtered


def sync_daily_history(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    force: bool = False,
) -> SyncResult:
    """Download and store full-market daily bars for every open trading day.

    Existing files are skipped unless ``force`` is true. A failure on any day
    raises immediately but leaves already-synced days on disk, so a re-run
    resumes from the missing dates.
    """
    open_dates = _open_trade_dates(provider, start_date, end_date)
    list_dates = _build_list_dates(provider)
    synced = 0
    skipped = 0
    filtered = 0
    for trade_date in open_dates:
        if not force and storage.daily_bars_exists(trade_date):
            skipped += 1
            continue
        try:
            bars = provider.get_daily_bars_by_date(trade_date)
        except Exception as exc:
            raise RuntimeError(f"Failed to download daily bars for {trade_date}") from exc
        bars, day_filtered = filter_unlisted_placeholders(bars, list_dates, trade_date)
        filtered += day_filtered
        validate_daily_bars(bars, trade_date)
        storage.save_daily_bars_by_date(bars, trade_date)
        synced += 1
    return SyncResult(total=len(open_dates), synced=synced, skipped=skipped, filtered=filtered)
