import json
from dataclasses import replace
from datetime import date, datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from quantlab.data import context_backfill as job
from quantlab.data.models import (
    DataValidationError,
    StockSTStatus,
    SuspensionRecord,
    TradingCalendar,
)
from quantlab.data.storage import ParquetStorage

DAYS = (date(2025, 1, 2), date(2025, 1, 3))


def _st(day):
    return StockSTStatus("000001.SZ", day, "ST样本", "ST", "风险警示", "st-" + str(day))


def _sr(day):
    return SuspensionRecord("000001.SZ", day, "S", None, "sr-" + str(day))


@pytest.fixture
def setup(tmp_path):
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_trading_calendar(
        [TradingCalendar(exchange, day, True) for exchange in ("SSE", "SZSE") for day in DAYS]
    )
    provider = Mock()
    provider.get_stock_st_by_date.side_effect = lambda day: [_st(day)]
    provider.get_suspensions_by_date.side_effect = lambda day: [_sr(day)]
    return storage, provider, tmp_path / "receipts"


def _run(setup, **kwargs):
    storage, provider, receipts = setup
    return job.backfill_missing_context(
        lambda: provider,
        storage,
        receipts,
        **{
            "code_head": "a" * 40,
            "start": DAYS[0],
            "end": DAYS[-1],
            "pause": lambda seconds: None,
            **kwargs,
        },
    )


def test_missing_only_acquisition_provenance_and_reuse(setup):
    storage, provider, _ = setup
    storage.save_stock_st_v1_by_date([_st(DAYS[0])], DAYS[0])
    original = storage.stock_st_v1_path(DAYS[0]).read_bytes()
    path, report = _run(setup)
    assert report["status"] == "complete"
    assert len(report["accepted"]) == report["planned_calls"] == 3
    assert report["missing_after"] == []
    assert storage.stock_st_v1_path(DAYS[0]).read_bytes() == original
    assert len(list(path.parent.glob("request-*.json"))) == 3
    assert len(list(path.parent.glob("accepted-*.json"))) == 3
    assert report["historical_revision_status"] == "unverified"
    assert not report["full_tradability_verified"] and not report["execution_authority"]
    for receipt in report["provider_calls"]:
        assert datetime.fromisoformat(receipt["requested_at"]).tzinfo
        assert receipt["requested_at"] <= receipt["observed_at"]
    provider.reset_mock()
    _, second = _run(setup, max_calls=0)
    assert second["planned_calls"] == 0 and second["accepted"] == []
    assert second["status"] == "complete"
    provider.get_stock_st_by_date.assert_not_called()
    provider.get_suspensions_by_date.assert_not_called()
    provider.get_trading_calendar.assert_not_called()


def test_provider_failure_records_partial_and_resume_only_missing(setup):
    storage, provider, _ = setup
    provider.get_suspensions_by_date.side_effect = RuntimeError("synthetic sensitive detail")
    path, report = _run(setup)
    assert report["status"] == "partial_failed"
    assert len(report["accepted"]) == 1  # ST succeeded before S/R failed on the same day.
    assert len(report["missing_after"]) == 3
    assert "synthetic sensitive detail" not in path.read_text()
    assert report["provider_calls"][-1]["status"] == "provider_failed"
    assert storage.stock_st_v1_exists(DAYS[0])
    provider.reset_mock()
    provider.get_suspensions_by_date.side_effect = lambda day: [_sr(day)]
    _, second = _run(setup)
    assert second["status"] == "complete"
    assert second["planned_calls"] == 3
    assert provider.get_stock_st_by_date.call_args_list[0].args == (DAYS[1],)


@pytest.mark.parametrize(
    "bad_rows",
    [
        lambda day: [_st(day)] * 1000,
        lambda day: [replace(_st(day), trade_date=date(2025, 1, 4))],
        lambda day: [_st(day), _st(day)],
    ],
)
def test_invalid_context_response_does_not_publish(setup, bad_rows):
    storage, provider, _ = setup
    provider.get_stock_st_by_date.side_effect = bad_rows
    _, report = _run(setup)
    assert report["status"] == "partial_failed"
    assert not storage.stock_st_v1_exists(DAYS[0])
    assert report["missing_after"]


def test_empty_success_keeps_provider_scope_unknown(setup):
    storage, provider, _ = setup
    provider.get_stock_st_by_date.side_effect = lambda day: []
    provider.get_suspensions_by_date.side_effect = lambda day: []
    _, report = _run(setup)
    assert report["status"] == "complete"
    assert all(item["rows"] == 0 for item in report["accepted"])
    assert storage.stock_st_v1_exists(DAYS[0])
    assert report["full_tradability_verified"] is False


@pytest.mark.parametrize(
    "options",
    [
        {"start": date(2024, 12, 31)},
        {"end": date(2026, 9, 4)},
        {"max_calls": 3},
        {"max_calls": 813},
        {"max_calls": True},
        {"code_head": "short"},
    ],
)
def test_scope_and_budget_fail_before_provider(setup, options):
    with pytest.raises(DataValidationError):
        _run(setup, **options)
    setup[1].get_stock_st_by_date.assert_not_called()
    assert not setup[2].exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda frame: frame.iloc[:-1],
        lambda frame: pd.concat([frame, frame.iloc[:1]]),
        lambda frame: frame.assign(is_open=None),
        lambda frame: frame.assign(is_open=frame.exchange.eq("SSE")),
        lambda frame: frame.assign(trade_date=frame.trade_date + pd.Timedelta(hours=1)),
    ],
)
def test_invalid_local_calendar_prevents_calls(setup, change):
    storage = setup[0]
    change(pd.read_parquet(storage.calendar_path)).to_parquet(storage.calendar_path, index=False)
    with pytest.raises(DataValidationError):
        _run(setup)
    setup[1].get_stock_st_by_date.assert_not_called()


def test_atomic_publication_does_not_replace_a_concurrent_file(tmp_path):
    path = tmp_path / "context.parquet"
    path.write_bytes(b"concurrent existing evidence")
    storage = job._ExclusiveStorage(tmp_path, Mock())
    with pytest.raises(FileExistsError):
        storage._write(pd.DataFrame({"a": [1]}), path)
    assert path.read_bytes() == b"concurrent existing evidence"
    storage.on_write.assert_not_called()
    assert not list(tmp_path.glob("*.tmp"))


def test_source_drift_is_not_success(setup):
    storage, provider, _ = setup

    def change(day):
        storage.calendar_path.write_bytes(b"concurrent drift")
        return [_st(day)]

    provider.get_stock_st_by_date.side_effect = change
    path, report = _run(setup)
    assert report["status"] == "source_drift"
    assert json.loads(path.read_text())["status"] == "source_drift"
    assert report["missing_after"]


def test_pacing_and_progress_are_bounded(setup):
    pause = Mock()
    _run(setup, pause=pause)
    assert pause.call_count == 4
    assert all(call.args == (0.4,) for call in pause.call_args_list)
