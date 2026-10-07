"""Cross-day limit recovery uses synthetic provider fixtures only, never network."""

from datetime import date, datetime

import pandas as pd

from quantlab.data.models import DailyBar, DailyPriceLimit, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.scout.cloud_artifacts import initialize
from quantlab.scout.cloud_data import Fetcher, refresh_observation_limits
from quantlab.scout.daily_runtime import atomic, read
from quantlab.scout.models import SHANGHAI, Candidate
from quantlab.scout.nextday_tracking import freeze_protocol

D1 = date(2026, 10, 8)
NOW = datetime(2026, 10, 12, 18, tzinfo=SHANGHAI)
CODE = "000001.SZ"


class Client:
    def __init__(self, *, fail=False):
        self.requests = []
        self.fail = fail

    def query(self, api, **params):
        self.requests.append((api, params))
        if self.fail:
            raise RuntimeError("synthetic provider outage")
        assert api == "stk_limit" and params["trade_date"] == "20261008"
        return pd.DataFrame(
            [
                {
                    "ts_code": CODE,
                    "trade_date": "20261008",
                    "pre_close": 10,
                    "up_limit": 11,
                    "down_limit": 9,
                    "exchange": "SZSE",
                }
            ]
        )


def setup(tmp_path):
    root = initialize(tmp_path / "scout")
    storage = ParquetStorage(root / "market")
    storage.save_trading_calendar(
        [TradingCalendar("SSE", day, True) for day in (D1, date(2026, 10, 9), NOW.date())]
    )
    storage.save_daily_bars_by_date([DailyBar(CODE, D1, 11, 11, 10.9, 11, 10, 1000, 10000)], D1)
    packet = {
        "timing": {"target_session": D1.isoformat()},
        "candidates": [{"instrument_id": CODE, "source_summary": {}}],
        "coverage": [],
    }
    report = {
        "run_id": "frozen",
        "status": "complete",
        "timing": packet["timing"],
        "selection_input_packet": packet,
        "nextday_freeze": freeze_protocol(
            [
                {
                    "instrument_id": CODE,
                    "final_status": "focus",
                    "rank": 1,
                    "primary_type": "trend_continuation",
                }
            ],
            packet,
            {CODE: Candidate(CODE, CODE, {"close": 10}, 1)},
            {CODE: "I"},
        ),
    }
    path = root / "runs/frozen/report.json"
    atomic(path, report)
    return root, storage, path


def test_cross_day_existing_bars_missing_d1_limits_are_recovered_and_reused(tmp_path):
    root, storage, path = setup(tmp_path)
    original_report, original_bars = path.read_bytes(), storage.daily_bars_path(D1).read_bytes()
    client = Client()
    result = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert result["status"] == "ready" and result["provider_calls"] == 1
    assert result["rows"][0]["target_session"] == "2026-10-08"
    assert storage.load_daily_price_limits_by_date(D1)[0].up_limit == 11
    saved_limits = storage.daily_price_limit_path(D1).read_bytes()
    again = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert again["status"] == "ready" and again["provider_calls"] == 0
    assert len(client.requests) == 1
    assert storage.daily_price_limit_path(D1).read_bytes() == saved_limits
    assert path.read_bytes() == original_report
    assert storage.daily_bars_path(D1).read_bytes() == original_bars
    assert len(list((root / "observations/data_supplements").glob("*.json"))) == 2


def test_provider_failure_is_persisted_and_same_day_not_requested_again(tmp_path):
    root, storage, _ = setup(tmp_path)
    client = Client(fail=True)
    result = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert result["status"] == "unknown"
    assert result["rows"][0]["status"] == "failed_preserved"
    assert not storage.daily_price_limit_path(D1).exists()
    assert read(next((root / "observations/data_supplements").glob("*.json"))) == result
    assert read(next((root / "source_updates").rglob("*.json")))["status"] == "failed"
    again = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert again["status"] == "unknown" and again["provider_calls"] == 0
    assert len(client.requests) == 1


def test_partial_existing_limits_are_unknown_and_never_overwritten(tmp_path):
    root, storage, _ = setup(tmp_path)
    storage.save_daily_price_limits_by_date(
        [DailyPriceLimit("000002.SZ", D1, 10, 11, 9, "SZSE", "synthetic", "fixture")], D1
    )
    original = storage.daily_price_limit_path(D1).read_bytes()
    client = Client()
    result = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert result["status"] == "unknown"
    assert result["rows"][0]["missing_instrument_ids"] == [CODE]
    assert not client.requests
    assert storage.daily_price_limit_path(D1).read_bytes() == original


def test_target_mismatching_frozen_timing_does_not_request_provider(tmp_path):
    root, storage, path = setup(tmp_path)
    report = read(path)
    report["timing"]["target_session"] = "2026-10-09"
    atomic(path, report)
    original = path.read_bytes()
    client = Client()
    result = refresh_observation_limits(root, Fetcher(root, NOW, client), NOW)
    assert result["status"] == "unknown" and result["report_issues"]
    assert not client.requests and not storage.daily_price_limit_path(D1).exists()
    assert path.read_bytes() == original


def test_cloud_catchup_supplements_target_even_when_bulk_history_is_unavailable(
    tmp_path, monkeypatch
):
    from quantlab.scout import free_cloud as free

    root, storage, _ = setup(tmp_path)
    client = Client()
    monkeypatch.setattr(free, "refresh_calendar", lambda *args: storage)

    def unavailable(*args, **kwargs):
        raise ValueError("history bootstrap missing")

    monkeypatch.setattr(free, "refresh_market", unavailable)
    seen = []

    def scan(*args, **kwargs):
        seen.append(storage.load_daily_price_limits_by_date(D1))
        return {"status": "complete", "model_calls": 0}

    result = free.observation_catchup(
        root, NOW, fetcher_factory=lambda root, now: Fetcher(root, now, client), scan=scan
    )
    assert result["status"] == "data_unknown" and result["model_calls"] == 0
    assert result["target_limits"]["status"] == "ready"
    assert len(seen[0]) == 1 and result["provider_calls"] == 1
