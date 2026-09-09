from __future__ import annotations

from datetime import date
from pathlib import Path

from quantlab.daily.update import DEFAULT_INDICES, run_incremental_update
from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
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
