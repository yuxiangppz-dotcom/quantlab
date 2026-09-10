from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantlab.daily.service import inspect_data_status
from quantlab.daily.update import DEFAULT_INDICES, run_incremental_update
from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DataValidationError,
    IndexDailyBar,
    Security,
    TradingCalendar,
)
from quantlab.data.provider import DataProvider
from quantlab.data.storage import ParquetStorage


class FakeProvider(DataProvider):
    def __init__(self, trade_date: date, *, complete: bool) -> None:
        self.trade_date = trade_date
        self.complete = complete
        self.daily_calls = 0

    def get_securities(self):
        return [
            Security(
                "000001.SZ",
                "000001",
                "平安银行",
                "SZSE",
                "SZ",
                "主板",
                "L",
                date(1991, 4, 3),
                None,
            )
        ]

    def get_trading_calendar(self, start_date, end_date):
        return [TradingCalendar("SSE", self.trade_date, True)]

    def get_daily_bars(self, instrument_ids, start_date, end_date):
        return self.get_daily_bars_by_date(start_date)

    def get_daily_bars_by_date(self, trade_date):
        self.daily_calls += 1
        if not self.complete:
            return []
        return [DailyBar("000001.SZ", trade_date, 10, 11, 9, 10, 9, 1000, 10000)]

    def get_adj_factors_by_date(self, trade_date):
        return [AdjFactor("000001.SZ", trade_date, 1.0)] if self.complete else []

    def get_daily_basic_by_date(self, trade_date):
        return (
            [DailyBasic("000001.SZ", trade_date, 0.01, 100_000_000, 80_000_000)]
            if self.complete
            else []
        )

    def get_index_daily(self, instrument_id, start_date, end_date):
        return [IndexDailyBar(instrument_id, start_date, 10, 11, 9, 10, 9, 1, 1)]

    def get_stock_st_by_date(self, trade_date):
        return []

    def get_suspensions_by_date(self, trade_date):
        return []

    def get_lifecycle_announcements_by_date(self, announcement_date):
        return []

    def get_name_changes(self, instrument_id, start_date, end_date):
        return []


def test_incomplete_close_stops_without_writing_core(tmp_path: Path) -> None:
    day = date(2026, 9, 10)
    storage = ParquetStorage(tmp_path / "canonical")
    result = run_incremental_update(
        FakeProvider(day, complete=False), storage, day, include_context=False
    )
    assert result.stopped_at == day.isoformat()
    assert not storage.daily_bars_exists(day)
    assert not storage.adj_factor_exists(day)
    assert not storage.daily_basic_exists(day)


def test_complete_close_is_written_once_and_rerun_skips(tmp_path: Path) -> None:
    day = date(2026, 9, 10)
    storage = ParquetStorage(tmp_path / "canonical")
    provider = FakeProvider(day, complete=True)
    first = run_incremental_update(provider, storage, day, include_context=True)
    assert first.core_synced_sessions == (day.isoformat(),)
    assert storage.daily_bars_exists(day)
    assert storage.adj_factor_exists(day)
    assert storage.daily_basic_exists(day)
    assert storage.index_daily_exists(day)
    assert {item.instrument_id for item in storage.load_index_daily_by_date(day)} == set(
        DEFAULT_INDICES
    )
    calls = provider.daily_calls

    second = run_incremental_update(provider, storage, day, include_context=True)
    assert second.core_synced_sessions == ()
    assert second.core_skipped_sessions == (day.isoformat(),)
    assert provider.daily_calls == calls


class MultiDayProvider(FakeProvider):
    def __init__(self, days, *, incomplete=None):
        super().__init__(days[0], complete=True)
        self.days = days
        self.incomplete = incomplete
        self.bar_dates = []
        self.calendar_requests = []

    def get_trading_calendar(self, start_date, end_date):
        self.calendar_requests.append((start_date, end_date))
        return [TradingCalendar("SSE", day, True) for day in self.days
                if start_date <= day <= end_date]

    def get_daily_bars_by_date(self, trade_date):
        self.bar_dates.append(trade_date)
        return [] if trade_date == self.incomplete else super().get_daily_bars_by_date(trade_date)


def test_next_day_retry_repairs_close_even_after_calendar_has_advanced(tmp_path):
    first, second, future = (date(2026, 9, day) for day in (7, 8, 9))
    storage = ParquetStorage(tmp_path)
    provider = MultiDayProvider([first, second, future], incomplete=first)
    stopped = run_incremental_update(provider, storage, first, include_context=False)
    assert stopped.stopped_at == first.isoformat()
    assert max(item.trade_date for item in storage.load_trading_calendar()) == future
    provider.incomplete = None
    provider.bar_dates.clear()
    resumed = run_incremental_update(provider, storage, second, include_context=False)
    assert resumed.core_synced_sessions == (first.isoformat(), second.isoformat())
    assert provider.bar_dates == [first, second]
    assert not storage.daily_bars_exists(future)
    assert resumed.calendar_requested_through == second + timedelta(days=35)


def test_partial_partition_and_missing_index_are_repaired_without_overwrite(tmp_path):
    day = date(2026, 9, 7)
    storage = ParquetStorage(tmp_path)
    provider = MultiDayProvider([day])
    storage.save_daily_bars_by_date(provider.get_daily_bars_by_date(day), day)
    before = storage.daily_bars_path(day).read_bytes()
    provider.bar_dates.clear()
    run_incremental_update(provider, storage, day, include_context=False)
    assert provider.bar_dates == []
    assert storage.daily_bars_path(day).read_bytes() == before
    assert storage.adj_factor_exists(day) and storage.daily_basic_exists(day)
    assert storage.index_daily_exists(day)


def test_old_gaps_are_reported_outside_download_window(tmp_path):
    days = [date(2026, 9, day) for day in (1, 2, 3, 4, 7, 8, 9)]
    storage = ParquetStorage(tmp_path)
    provider = MultiDayProvider(days)
    result = run_incremental_update(provider, storage, days[-1], include_context=False)
    assert provider.bar_dates == days[-5:]
    assert result.older_missing_partition_sessions == 2
    assert result.older_missing_partition_examples == ("2026-09-01", "2026-09-02")
    assert not storage.daily_bars_exists(days[0])


def test_incomplete_index_is_an_explicit_stop(tmp_path):
    day = date(2026, 9, 7)
    storage = ParquetStorage(tmp_path)
    provider = MultiDayProvider([day])
    provider.get_index_daily = lambda *args: []
    result = run_incremental_update(provider, storage, day, include_context=False)
    assert result.stopped_at == day.isoformat()
    assert "index close" in result.stop_reason
    assert not storage.index_daily_exists(day)


@pytest.mark.parametrize("options", [
    {"lookback_sessions": 0}, {"lookback_sessions": 61}, {"lookback_sessions": True},
    {"calendar_lookahead_days": -1}, {"calendar_lookahead_days": 367},
    {"calendar_lookahead_days": 35.0},
])
def test_invalid_bounds_fail_before_any_provider_call(tmp_path, options):
    provider = MultiDayProvider([date(2026, 9, 7)])
    with pytest.raises(DataValidationError):
        run_incremental_update(provider, ParquetStorage(tmp_path), date(2026, 9, 7), **options)
    assert provider.calendar_requests == [] and provider.bar_dates == []


def test_future_daily_request_is_rejected_before_provider_access(tmp_path):
    tomorrow = datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    provider = MultiDayProvider([tomorrow])
    with pytest.raises(DataValidationError, match="no later than today"):
        run_incremental_update(provider, ParquetStorage(tmp_path), tomorrow)
    assert provider.calendar_requests == []


def test_calendar_hole_is_unknown_instead_of_closed(tmp_path):
    storage = ParquetStorage(tmp_path)
    days = [date(2026, 9, day) for day in (7, 9)]
    provider = MultiDayProvider(days)
    run_incremental_update(provider, storage, days[0], include_context=False)
    status = inspect_data_status(storage, date(2026, 9, 8))
    assert status["requested_session_status"] == "unknown_calendar_missing_date"
    assert status["status"] == "stale_calendar_unknown"
    assert status["stale_open_sessions"] is None
