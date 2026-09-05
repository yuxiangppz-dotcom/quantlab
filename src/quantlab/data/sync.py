"""Historical daily-bar sync service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import AdjFactor, DailyBar, DailyBasic, DataValidationError
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync run."""

    total: int
    synced: int
    skipped: int
    filtered: int


@dataclass(frozen=True)
class CoverageResult:
    """Daily vs adj_factor coverage for a single trading date."""

    trade_date: date
    daily_count: int
    adj_factor_count: int
    overlap_count: int
    daily_missing_adj_count: int
    adj_extra_count: int
    missing_sample: tuple[str, ...]
    extra_sample: tuple[str, ...]


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


def validate_adj_factors(factors: list[AdjFactor], expected_date: date) -> None:
    """Validate one trading day's adj factors; raise on failure."""
    if not factors:
        raise DataValidationError(f"No adj factors for {expected_date}")

    seen: set[tuple[str, date]] = set()
    for factor in factors:
        if factor.trade_date != expected_date:
            raise DataValidationError(
                f"Unexpected trade_date {factor.trade_date} (expected {expected_date}) "
                f"for {factor.instrument_id}"
            )
        key = (factor.instrument_id, factor.trade_date)
        if key in seen:
            raise DataValidationError(
                f"Duplicate adj factor for {factor.instrument_id} on {factor.trade_date}"
            )
        seen.add(key)

        if (
            factor.adj_factor is None
            or not math.isfinite(factor.adj_factor)
            or factor.adj_factor <= 0
        ):
            raise DataValidationError(
                f"Invalid adj_factor for {factor.instrument_id} on {factor.trade_date}"
            )


def audit_daily_adj_coverage(storage: ParquetStorage, trade_date: date) -> CoverageResult:
    """Compare daily and adj_factor instrument sets for a single trading date."""
    daily_ids = {bar.instrument_id for bar in storage.load_daily_bars_by_date(trade_date)}
    adj_ids = {factor.instrument_id for factor in storage.load_adj_factors_by_date(trade_date)}
    missing = daily_ids - adj_ids
    extra = adj_ids - daily_ids
    return CoverageResult(
        trade_date=trade_date,
        daily_count=len(daily_ids),
        adj_factor_count=len(adj_ids),
        overlap_count=len(daily_ids & adj_ids),
        daily_missing_adj_count=len(missing),
        adj_extra_count=len(extra),
        missing_sample=tuple(sorted(missing)[:10]),
        extra_sample=tuple(sorted(extra)[:10]),
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


def sync_adj_factor_history(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    force: bool = False,
) -> SyncResult:
    """Download and store full-market adj factors for every open trading day.

    Existing files are skipped unless ``force`` is true. A failure on any day
    raises immediately but leaves already-synced days on disk, so a re-run
    resumes from the missing dates.
    """
    open_dates = _open_trade_dates(provider, start_date, end_date)
    synced = 0
    skipped = 0
    for trade_date in open_dates:
        if not force and storage.adj_factor_exists(trade_date):
            skipped += 1
            continue
        try:
            factors = provider.get_adj_factors_by_date(trade_date)
        except Exception as exc:
            raise RuntimeError(f"Failed to download adj factors for {trade_date}") from exc
        validate_adj_factors(factors, trade_date)
        storage.save_adj_factors_by_date(factors, trade_date)
        synced += 1
    return SyncResult(total=len(open_dates), synced=synced, skipped=skipped, filtered=0)


def validate_daily_basic(items: list[DailyBasic], expected_date: date) -> None:
    """Validate one trading day's daily basic metrics."""
    if not items:
        raise DataValidationError(f"No daily basic for {expected_date}")

    seen: set[tuple[str, date]] = set()
    for item in items:
        if not item.instrument_id:
            raise DataValidationError(f"Empty instrument_id in daily basic for {expected_date}")
        if item.trade_date != expected_date:
            raise DataValidationError(
                f"Unexpected trade_date {item.trade_date} (expected {expected_date}) "
                f"for {item.instrument_id}"
            )
        key = (item.instrument_id, item.trade_date)
        if key in seen:
            raise DataValidationError(
                f"Duplicate daily basic for {item.instrument_id} on {item.trade_date}"
            )
        seen.add(key)

        if (
            item.turnover_rate is None
            or not math.isfinite(item.turnover_rate)
            or item.turnover_rate < 0
        ):
            raise DataValidationError(
                f"Invalid turnover_rate for {item.instrument_id} on {item.trade_date}"
            )
        if item.total_mv is None or not math.isfinite(item.total_mv) or item.total_mv <= 0:
            raise DataValidationError(
                f"Invalid total_mv for {item.instrument_id} on {item.trade_date}"
            )
        if item.circ_mv is None or not math.isfinite(item.circ_mv) or item.circ_mv <= 0:
            raise DataValidationError(
                f"Invalid circ_mv for {item.instrument_id} on {item.trade_date}"
            )


def sync_daily_basic_history(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    force: bool = False,
) -> SyncResult:
    """Download and store full-market daily basic metrics for every open day."""
    open_dates = _open_trade_dates(provider, start_date, end_date)
    synced = 0
    skipped = 0
    for trade_date in open_dates:
        if not force and storage.daily_basic_exists(trade_date):
            skipped += 1
            continue
        try:
            items = provider.get_daily_basic_by_date(trade_date)
        except Exception as exc:
            raise RuntimeError(f"Failed to download daily basic for {trade_date}") from exc
        validate_daily_basic(items, trade_date)
        storage.save_daily_basic_by_date(items, trade_date)
        synced += 1
    return SyncResult(total=len(open_dates), synced=synced, skipped=skipped, filtered=0)
