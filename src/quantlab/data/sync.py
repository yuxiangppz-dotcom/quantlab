"""Historical daily-bar sync service."""

from __future__ import annotations

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
            ("volume", bar.volume),
            ("amount", bar.amount),
        ):
            if value is None or value != value:  # NaN check
                raise DataValidationError(
                    f"Missing {field_name} for {bar.instrument_id} on {bar.trade_date}"
                )

        if bar.volume < 0 or bar.amount < 0:
            raise DataValidationError(
                f"Negative volume/amount for {bar.instrument_id} on {bar.trade_date}"
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
    synced = 0
    skipped = 0
    for trade_date in open_dates:
        if not force and storage.daily_bars_exists(trade_date):
            skipped += 1
            continue
        try:
            bars = provider.get_daily_bars_by_date(trade_date)
        except Exception as exc:
            raise RuntimeError(f"Failed to download daily bars for {trade_date}") from exc
        validate_daily_bars(bars, trade_date)
        storage.save_daily_bars_by_date(bars, trade_date)
        synced += 1
    return SyncResult(total=len(open_dates), synced=synced, skipped=skipped)
