"""Explicit, resumable daily Canonical update orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

from quantlab.data.models import DataValidationError
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import (
    sync_lifecycle_context,
    validate_adj_factors,
    validate_daily_bars,
    validate_daily_basic,
    validate_index_daily_bars,
)

DEFAULT_INDICES = ("000300.SH", "000905.SH", "000852.SH")


@dataclass(frozen=True)
class IncrementalUpdateResult:
    requested_through: date
    calendar_start: date
    candidate_open_sessions: int
    core_synced_sessions: tuple[str, ...]
    core_skipped_sessions: tuple[str, ...]
    index_synced_sessions: tuple[str, ...]
    context_status: dict
    stopped_at: str | None
    stop_reason: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _load_or_fetch_core(
    provider: DataProvider, storage: ParquetStorage, trade_date: date
) -> tuple[list, list, list]:
    bars = (
        storage.load_daily_bars_by_date(trade_date)
        if storage.daily_bars_exists(trade_date)
        else provider.get_daily_bars_by_date(trade_date)
    )
    factors = (
        storage.load_adj_factors_by_date(trade_date)
        if storage.adj_factor_exists(trade_date)
        else provider.get_adj_factors_by_date(trade_date)
    )
    basics = (
        storage.load_daily_basic_by_date(trade_date)
        if storage.daily_basic_exists(trade_date)
        else provider.get_daily_basic_by_date(trade_date)
    )
    validate_daily_bars(bars, trade_date)
    validate_adj_factors(factors, trade_date)
    validate_daily_basic(basics, trade_date)
    daily_ids = {item.instrument_id for item in bars}
    factor_ids = {item.instrument_id for item in factors}
    basic_ids = {item.instrument_id for item in basics}
    if not daily_ids.issubset(factor_ids):
        raise DataValidationError(
            f"daily rows without adjustment factor on {trade_date}: "
            f"{sorted(daily_ids - factor_ids)[:5]}"
        )
    if daily_ids != basic_ids:
        raise DataValidationError(f"daily/daily_basic instrument coverage mismatch on {trade_date}")
    return bars, factors, basics


def run_incremental_update(
    provider: DataProvider,
    storage: ParquetStorage,
    through: date,
    *,
    include_context: bool = True,
    index_instruments: tuple[str, ...] = DEFAULT_INDICES,
) -> IncrementalUpdateResult:
    """Update missing daily partitions through a requested local date.

    For each session the three model-critical payloads are fetched and
    cross-validated before any of them is written.  A provider that has not yet
    published a complete close stops the run cleanly; the common complete date
    therefore never advances on partial data.  Existing partitions are reused.
    """
    existing_calendar = storage.load_trading_calendar()
    calendar_start = (
        min(through, max(item.trade_date for item in existing_calendar) + timedelta(days=1))
        if existing_calendar
        else through - timedelta(days=45)
    )
    if calendar_start > through:
        calendar_start = through
    fresh_calendar = provider.get_trading_calendar(calendar_start, through)
    if fresh_calendar:
        storage.upsert_trading_calendar(fresh_calendar)
    securities = provider.get_securities()
    if securities:
        storage.upsert_securities(securities)

    calendar = storage.load_trading_calendar()
    open_dates = sorted(
        {
            item.trade_date
            for item in calendar
            if item.is_open and calendar_start <= item.trade_date <= through
        }
    )
    synced: list[str] = []
    skipped: list[str] = []
    index_synced: list[str] = []
    stopped_at: str | None = None
    stop_reason: str | None = None

    for trade_date in open_dates:
        complete_before = all(
            (
                storage.daily_bars_exists(trade_date),
                storage.adj_factor_exists(trade_date),
                storage.daily_basic_exists(trade_date),
            )
        )
        try:
            bars, factors, basics = _load_or_fetch_core(provider, storage, trade_date)
        except DataValidationError as exc:
            stopped_at = trade_date.isoformat()
            stop_reason = f"provider close not complete or invalid: {exc}"
            break
        if complete_before:
            skipped.append(trade_date.isoformat())
        else:
            if not storage.daily_bars_exists(trade_date):
                storage.save_daily_bars_by_date(bars, trade_date)
            if not storage.adj_factor_exists(trade_date):
                storage.save_adj_factors_by_date(factors, trade_date)
            if not storage.daily_basic_exists(trade_date):
                storage.save_daily_basic_by_date(basics, trade_date)
            synced.append(trade_date.isoformat())

        if not storage.index_daily_exists(trade_date):
            index_rows = []
            for instrument_id in index_instruments:
                index_rows.extend(provider.get_index_daily(instrument_id, trade_date, trade_date))
            validate_index_daily_bars(index_rows, trade_date)
            found = {item.instrument_id for item in index_rows}
            if found == set(index_instruments):
                storage.save_index_daily_by_date(index_rows, trade_date)
                index_synced.append(trade_date.isoformat())

    context_status: dict = {"requested": include_context, "datasets": {}}
    completed_dates = [date.fromisoformat(value) for value in (*skipped, *synced)]
    if include_context and completed_dates:
        context = sync_lifecycle_context(
            provider, storage, min(completed_dates), max(completed_dates)
        )
        context_status["datasets"] = {name: asdict(result) for name, result in context.items()}

    return IncrementalUpdateResult(
        requested_through=through,
        calendar_start=calendar_start,
        candidate_open_sessions=len(open_dates),
        core_synced_sessions=tuple(synced),
        core_skipped_sessions=tuple(skipped),
        index_synced_sessions=tuple(index_synced),
        context_status=context_status,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
    )
