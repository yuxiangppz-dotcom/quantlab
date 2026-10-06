"""Bounded isolated history preparation with a synthetic provider, no network."""

from datetime import date, datetime

import pandas as pd
import pytest

from quantlab.data.models import AdjFactor, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.cloud_artifacts import initialize
from quantlab.scout.cloud_data import Fetcher, prepare_history, refresh_calendar, refresh_market
from quantlab.scout.models import SHANGHAI

NOW = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)


class SyntheticClient:
    def __init__(self):
        self.requests = []

    def query(self, api, **params):
        self.requests.append((api, params))
        if api == "trade_cal":
            return pd.DataFrame(
                [
                    {
                        "exchange": "SSE",
                        "cal_date": day.strftime("%Y%m%d"),
                        "is_open": int(
                            day.weekday() < 5
                            and not date(2026, 10, 1) <= day.date() <= date(2026, 10, 7)
                        ),
                    }
                    for day in pd.date_range(params["start_date"], params["end_date"])
                ]
            )
        if api == "stock_basic":
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "symbol": "000001",
                        "name": "synthetic",
                        "exchange": "SZSE",
                        "market": "主板",
                        "list_status": "L",
                        "list_date": "19900101",
                        "delist_date": None,
                    }
                ]
                if params["list_status"] == "L"
                else []
            )
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": params["trade_date"],
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "pre_close": 10,
                    "vol": 123,
                    "amount": 456,
                    "turnover_rate": 5,
                    "total_mv": 1000,
                    "circ_mv": 900,
                    "adj_factor": 1,
                    "up_limit": 11,
                    "down_limit": 9,
                    "exchange": "SZSE",
                }
            ]
        )


def initialize_all(volume, client):
    receipts = []
    for _ in range(7):
        fetcher = Fetcher(volume, NOW, client)
        receipt = prepare_history(volume, fetcher, NOW)
        receipts.append(receipt)
        assert receipt["provider_calls"] <= 44
        assert receipt["completed_this_batch"] <= 40
        if receipt["status"] == "ready":
            return receipts
    raise AssertionError("Synthetic cache failed to finish seven bounded batches")


def test_partial_resume_ready_then_zero_partition_requests(tmp_path):
    volume = initialize(tmp_path / "scout")
    client = SyntheticClient()
    receipts = initialize_all(volume, client)
    assert receipts[0]["status"] == "pending"
    assert receipts[0]["remaining_partitions"] == 202
    assert receipts[-1]["status"] == "ready"
    assert receipts[-1]["required_partitions"] == 242  # 240 history + D0 basic/limits.
    assert receipts[-1]["remaining_partitions"] == 0
    assert len(list((volume / "data_preparation" / "batches").glob("*.json"))) == 7
    requests_before = len(client.requests)
    receipt = prepare_history(volume, Fetcher(volume, NOW, client), NOW)
    assert receipt["status"] == "ready" and receipt["provider_calls"] == 0
    assert len(client.requests) == requests_before
    assert len(list((volume / "market" / "daily").rglob("*.parquet"))) == 120


def test_120_morning_requires_bootstrap_then_downloads_only_new_day(tmp_path):
    volume = initialize(tmp_path / "scout")
    client = SyntheticClient()
    fetcher = Fetcher(volume, NOW, client)
    refresh_calendar(volume, fetcher, NOW)
    calls = fetcher.calls
    with pytest.raises(ValueError, match="history_bootstrap_required"):
        refresh_market(volume, fetcher, NOW, history_sessions=120)
    assert fetcher.calls == calls  # Do not initialize 240 files inside the morning claim.
    initialize_all(volume, client)
    requests_before = len(client.requests)
    tomorrow = datetime(2026, 10, 9, 8, tzinfo=SHANGHAI)
    increment = Fetcher(volume, tomorrow, client)
    refresh_calendar(volume, increment, tomorrow)
    result = refresh_market(volume, increment, tomorrow, history_sessions=120)
    assert result["status"] == "ready" and result["asof_session"] == "2026-10-08"
    history_requests = [
        params
        for api, params in client.requests[requests_before:]
        if api in {"daily", "adj_factor"}
    ]
    assert len(history_requests) == 2
    assert {row["trade_date"] for row in history_requests} == {"20261008"}


def test_calendar_365days_retains_older_existing_records(tmp_path):
    volume = initialize(tmp_path / "scout")
    storage = ParquetStorage(volume / "market")
    old = TradingCalendar("SSE", date(2024, 1, 2), True)
    storage.save_trading_calendar([old])
    client = SyntheticClient()
    refresh_calendar(volume, Fetcher(volume, NOW, client), NOW)
    assert client.requests[0][1]["start_date"] == "20251008"
    calendar = storage.load_trading_calendar()
    assert old in calendar
    assert sum(row.is_open and row.trade_date <= date(2026, 9, 30) for row in calendar) >= 120


def test_existing_factor_hole_blocks_ready_and_is_not_overwritten(tmp_path):
    volume = initialize(tmp_path / "scout")
    client = SyntheticClient()
    initialize_all(volume, client)
    storage = ParquetStorage(volume / "market")
    first_day = min(
        row.trade_date
        for row in storage.load_trading_calendar()
        if storage.daily_bars_path(row.trade_date).is_file()
    )
    storage.save_adj_factors_by_date([AdjFactor("600001.SH", first_day, 1)], first_day)
    original = storage.adj_factor_path(first_day).read_bytes()
    receipt = prepare_history(volume, Fetcher(volume, NOW, client), NOW)
    assert receipt["status"] == "blocked_coverage"
    assert receipt["remaining_partitions"] == 0
    assert receipt["coverage_issues"][0]["session"] == first_day.isoformat()
    assert storage.adj_factor_path(first_day).read_bytes() == original
    with pytest.raises(ValueError, match="history_bootstrap_required"):
        refresh_market(volume, Fetcher(volume, NOW, client), NOW, history_sessions=120)


def test_paginated_truncation_is_failed_not_complete(tmp_path, monkeypatch):
    from quantlab.scout.cloud_data import CAPS

    volume = initialize(tmp_path / "scout")
    monkeypatch.setitem(CAPS, "daily", 1)
    client = SyntheticClient()
    # Provider ignores offset and keeps returning the first row: explicit unknown.
    receipt = prepare_history(volume, Fetcher(volume, NOW, client), NOW)
    assert receipt["status"] == "failed"
    assert receipt["coverage_state"] == "unknown_not_complete"
    assert receipt["completed_this_batch"] == 0
    assert not list((volume / "market" / "daily").rglob("*.parquet"))
