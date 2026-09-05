"""Historical daily-bar sync service."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DataValidationError,
    IndexDailyBar,
)
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


@dataclass(frozen=True)
class ContextSyncResult:
    dataset: str
    requested_start: date
    requested_end: date
    expected_open_sessions: int
    completed_sessions: int
    missing_sessions: tuple[str, ...]
    truncated_sessions: tuple[str, ...]
    row_count: int
    unique_instruments: int
    complete: bool


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


def validate_index_daily_bars(bars: list[IndexDailyBar], expected_date: date) -> None:
    """Validate one trading day's index bars; raise on failure.

    Unlike stocks, an index may legitimately have no row for a date (e.g. an
    index launched later than the requested window), so an empty list is
    accepted; consistency of the rows that do exist is still enforced.
    """
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
                f"Duplicate index bar for {bar.instrument_id} on {bar.trade_date}"
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
                    f"Invalid {field_name} for {bar.instrument_id} "
                    f"on {bar.trade_date}"
                )

        for field_name, value in (("volume", bar.volume), ("amount", bar.amount)):
            # NaN volume/amount is tolerated (early index history may omit
            # turnover; benchmark returns use close only), but a negative
            # finite value is a data error.
            if math.isfinite(value) and value < 0:
                raise DataValidationError(
                    f"Invalid {field_name} for {bar.instrument_id} "
                    f"on {bar.trade_date}"
                )

        if bar.high < bar.open or bar.high < bar.close or bar.high < bar.low:
            raise DataValidationError(
                f"Invalid high for {bar.instrument_id} on {bar.trade_date}"
            )
        if bar.low > bar.open or bar.low > bar.close:
            raise DataValidationError(
                f"Invalid low for {bar.instrument_id} on {bar.trade_date}"
            )


def sync_index_daily_history(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    instrument_ids: list[str],
    force: bool = False,
) -> SyncResult:
    """Download and store index daily bars for the given index instruments.

    Each open trading day is stored in its own partition file, matching the
    daily-bar layout. Bars are fetched once per instrument over the full
    window and re-grouped by date, so missing dates resume without re-fetching
    covered instruments. A date with no rows is persisted as an empty file so
    a later run skips it instead of re-querying (same resumability convention
    as the lifecycle announcement index).
    """
    if not instrument_ids:
        raise DataValidationError("sync_index_daily_history requires instrument_ids")
    open_dates = _open_trade_dates(provider, start_date, end_date)
    missing_dates = [
        trade_date
        for trade_date in open_dates
        if force or not storage.index_daily_exists(trade_date)
    ]
    synced = 0
    skipped = len(open_dates) - len(missing_dates)
    if missing_dates:
        by_date: dict[date, list[IndexDailyBar]] = {d: [] for d in missing_dates}
        for instrument_id in instrument_ids:
            try:
                bars = provider.get_index_daily(instrument_id, start_date, end_date)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to download index daily bars for {instrument_id}"
                ) from exc
            for bar in bars:
                if bar.trade_date in by_date:
                    by_date[bar.trade_date].append(bar)
        for trade_date in missing_dates:
            day_bars = sorted(by_date[trade_date], key=lambda b: b.instrument_id)
            validate_index_daily_bars(day_bars, trade_date)
            storage.save_index_daily_by_date(day_bars, trade_date)
            synced += 1
    return SyncResult(total=len(open_dates), synced=synced, skipped=skipped, filtered=0)


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


def sync_lifecycle_announcement_index(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    force: bool = False,
    page_limit: int = 2000,
) -> SyncResult:
    """Synchronize the raw daily ``anns_d`` index safely and resumably.

    Empty successful days are persisted, so a later run does not repeatedly
    query them.  A full page is rejected rather than silently accepting a
    potentially truncated provider response.
    """
    total = synced = skipped = 0
    current = start_date
    while current <= end_date:
        total += 1
        if not force and storage.lifecycle_announcements_exists(current):
            skipped += 1
            current += timedelta(days=1)
            continue
        try:
            items = provider.get_lifecycle_announcements_by_date(current)
        except Exception as exc:
            raise RuntimeError("Failed to download lifecycle announcement index") from exc
        if len(items) >= page_limit:
            raise DataValidationError(
                f"lifecycle announcement response reaches page limit on {current}"
            )
        if any(item.announcement_date != current for item in items):
            raise DataValidationError(f"announcement date mismatch for {current}")
        storage.save_lifecycle_announcements_by_date(items, current)
        synced += 1
        current += timedelta(days=1)
    return SyncResult(total=total, synced=synced, skipped=skipped, filtered=0)


def _validate_context_rows(items, expected_date: date, dataset: str, limit: int) -> None:
    if len(items) >= limit:
        raise DataValidationError(f"{dataset} response reaches provider limit on {expected_date}")
    wrong = [item.instrument_id for item in items if item.trade_date != expected_date]
    if wrong:
        raise DataValidationError(
            f"{dataset} response scope mismatch on {expected_date}: {wrong[:5]}"
        )


def _context_result(
    dataset: str, start_date: date, end_date: date, expected: list[date],
    completed: list[date], truncated: list[date], rows,
) -> ContextSyncResult:
    completed_set = set(completed)
    missing = tuple(d.isoformat() for d in expected if d not in completed_set)
    return ContextSyncResult(
        dataset=dataset, requested_start=start_date, requested_end=end_date,
        expected_open_sessions=len(expected), completed_sessions=len(completed_set),
        missing_sessions=missing, truncated_sessions=tuple(d.isoformat() for d in truncated),
        row_count=len(rows), unique_instruments=len({row.instrument_id for row in rows}),
        complete=not missing and not truncated,
    )


def sync_lifecycle_context(
    provider: DataProvider,
    storage: ParquetStorage,
    start_date: date,
    end_date: date,
    force: bool = False,
) -> dict[str, ContextSyncResult]:
    """Sync date-scoped ST/S-R context with explicit completeness accounting."""
    expected = _open_trade_dates(provider, start_date, end_date)
    configs = (
        ("stock_st", 1000, storage.stock_st_v1_exists,
         provider.get_stock_st_by_date, storage.save_stock_st_v1_by_date,
         storage.load_stock_st_v1_by_date),
        ("suspend_d", 5000, storage.suspensions_v1_exists,
         provider.get_suspensions_by_date, storage.save_suspensions_v1_by_date,
         storage.load_suspensions_v1_by_date),
    )
    results: dict[str, ContextSyncResult] = {}
    for dataset, limit, exists, fetch, save, load in configs:
        completed: list[date] = []
        truncated: list[date] = []
        rows = []
        for trade_date in expected:
            if not force and exists(trade_date):
                items = load(trade_date)
                completed.append(trade_date)
                rows.extend(items)
                continue
            items = fetch(trade_date)
            try:
                _validate_context_rows(items, trade_date, dataset, limit)
            except DataValidationError:
                if len(items) >= limit:
                    truncated.append(trade_date)
                    continue
                raise
            save(items, trade_date)
            completed.append(trade_date)
            rows.extend(items)
        results[dataset] = _context_result(
            dataset, start_date, end_date, expected, completed, truncated, rows
        )
    return results
