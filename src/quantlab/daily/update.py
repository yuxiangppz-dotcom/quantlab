"""Explicit, resumable daily Canonical update orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from quantlab.data.enrichment import (
    sync_daily_price_limits,
    sync_dividend_observations,
    sync_financial_indicator_observation,
)
from quantlab.data.models import DataValidationError
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage
from quantlab.data.sync import (
    filter_unlisted_placeholders,
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
    enrichment_status: dict
    stopped_at: str | None
    stop_reason: str | None
    data_window_start: date | None
    calendar_requested_through: date
    older_missing_partition_sessions: int
    older_missing_partition_examples: tuple[str, ...]

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
    if storage.securities_exists():
        dates = {x.instrument_id: x.list_date for x in storage.load_securities()}
        bars, _ = filter_unlisted_placeholders(bars, dates, trade_date)
        # Valuation history can also be backfilled under successor codes that
        # were not listed yet; such rows are not valid on this session either.
        basics, _ = filter_unlisted_placeholders(basics, dates, trade_date)
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
    # BSE (including its NEEQ heritage codes) is outside the SH/SZ mandate of
    # this pipeline; vendor valuation coverage there is not required. Vendor
    # artifacts go both ways otherwise (renamed-code backfills, delisting-period
    # gaps); small residuals are receipted by the caller, systemic ones raise.
    def _in_scope(instrument_id: str) -> bool:
        return not instrument_id.endswith(".BJ")

    missing_basics = {x for x in daily_ids - basic_ids if _in_scope(x)}
    if len(missing_basics) > max(50, len(daily_ids) // 100):
        raise DataValidationError(
            f"daily rows without daily_basic on {trade_date}: "
            f"{sorted(missing_basics)[:5]}"
        )
    return bars, factors, basics, sorted(missing_basics)


def run_incremental_update(
    provider: DataProvider,
    storage: ParquetStorage,
    through: date,
    *,
    include_context: bool = True,
    index_instruments: tuple[str, ...] = DEFAULT_INDICES,
    include_enrichment: bool = False,
    financial_period: date | None = None,
    dividend_instruments: tuple[str, ...] = (),
    lookback_sessions: int = 5,
    calendar_lookahead_days: int = 35,
) -> IncrementalUpdateResult:
    """Update missing daily partitions through a requested local date.

    For each session the three model-critical payloads are fetched and
    cross-validated before any of them is written.  A provider that has not yet
    published a complete close stops the run cleanly; the common complete date
    therefore never advances on partial data.  Existing partitions are reused.
    """
    if type(lookback_sessions) is not int or not 1 <= lookback_sessions <= 60:
        raise DataValidationError("lookback_sessions must be an integer in [1, 60]")
    if type(calendar_lookahead_days) is not int or not 0 <= calendar_lookahead_days <= 366:
        raise DataValidationError("calendar_lookahead_days must be an integer in [0, 366]")
    if type(through) is not date or through > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise DataValidationError("daily update through must be a date no later than today")
    # Calendar publication and daily-bar publication progress independently.
    # Refresh a bounded calendar range, then resume from actual missing files.
    calendar_start = through - timedelta(days=max(45, lookback_sessions * 4))
    calendar_end = through + timedelta(days=calendar_lookahead_days)
    fresh_calendar = provider.get_trading_calendar(calendar_start, calendar_end)
    if fresh_calendar:
        storage.upsert_trading_calendar(fresh_calendar)
    securities = provider.get_securities()
    if securities:
        storage.upsert_securities(securities)

    calendar = storage.load_trading_calendar()
    all_open_dates = sorted(
        {
            item.trade_date
            for item in calendar
            if item.is_open and item.trade_date <= through
        }
    )
    open_dates = all_open_dates[-lookback_sessions:]
    exists = (
        storage.daily_bars_exists, storage.adj_factor_exists,
        storage.daily_basic_exists, storage.index_daily_exists,
    )
    older_missing = [
        day for day in all_open_dates[:-lookback_sessions]
        if not all(check(day) for check in exists)
    ]
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
            bars, factors, basics, _ = _load_or_fetch_core(provider, storage, trade_date)
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
            try:
                validate_index_daily_bars(index_rows, trade_date)
                if {item.instrument_id for item in index_rows} != set(index_instruments):
                    raise DataValidationError("required index instrument coverage is incomplete")
            except DataValidationError as exc:
                stopped_at = trade_date.isoformat()
                stop_reason = f"provider index close not complete or invalid: {exc}"
                break
            storage.save_index_daily_by_date(index_rows, trade_date)
            index_synced.append(trade_date.isoformat())

    context_status: dict = {"requested": include_context, "datasets": {}}
    completed_dates = [date.fromisoformat(value) for value in (*skipped, *synced)]
    if include_context and completed_dates:
        context = sync_lifecycle_context(
            provider, storage, min(completed_dates), max(completed_dates)
        )
        context_status["datasets"] = {name: asdict(result) for name, result in context.items()}

    enrichment_status: dict = {"requested": include_enrichment, "datasets": {}}
    if include_enrichment and completed_dates:
        for completed_date in completed_dates:
            result = sync_daily_price_limits(provider, storage, completed_date)
            enrichment_status["datasets"][f"stk_limit:{completed_date}"] = result.to_dict()
        if financial_period is not None:
            result = sync_financial_indicator_observation(provider, storage, financial_period)
            enrichment_status["datasets"]["fina_indicator_vip"] = result.to_dict()
        if dividend_instruments:
            result = sync_dividend_observations(provider, storage, dividend_instruments)
            enrichment_status["datasets"]["dividend"] = result.to_dict()

    return IncrementalUpdateResult(
        requested_through=through,
        calendar_start=calendar_start,
        candidate_open_sessions=len(open_dates),
        core_synced_sessions=tuple(synced),
        core_skipped_sessions=tuple(skipped),
        index_synced_sessions=tuple(index_synced),
        context_status=context_status,
        enrichment_status=enrichment_status,
        stopped_at=stopped_at,
        stop_reason=stop_reason,
        data_window_start=open_dates[0] if open_dates else None,
        calendar_requested_through=calendar_end,
        older_missing_partition_sessions=len(older_missing),
        older_missing_partition_examples=tuple(day.isoformat() for day in older_missing[:10]),
    )
