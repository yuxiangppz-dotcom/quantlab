from dataclasses import replace
from datetime import date
from unittest.mock import Mock

import pytest

from quantlab.data.limit_backfill import backfill_price_limits
from quantlab.data.models import (
    DailyBar,
    DailyPriceLimit,
    DataValidationError,
    Security,
    TradingCalendar,
)
from quantlab.data.storage import ParquetStorage

DAYS = (date(2020, 1, 2), date(2020, 1, 3))


def _limit(day):
    return DailyPriceLimit("000001.SZ", day, 10, 11, 9, "SZSE", "tushare.stk_limit", str(day))


@pytest.fixture
def setup(tmp_path):
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_securities(
        [Security("000001.SZ", "000001", "样本", "SZSE", "SZ", "主板", "L", date(1991, 1, 1), None)]
    )
    storage.save_trading_calendar(
        [TradingCalendar(exchange, day, True) for exchange in ("SSE", "SZSE") for day in DAYS]
    )
    for day in DAYS:
        storage.save_daily_bars_by_date(
            [DailyBar("000001.SZ", day, 10, 10, 10, 10, 10, 100, 1000)], day
        )
    history = tmp_path / "history.csv"
    history.write_text(
        "old_instrument_id,new_instrument_id,effective_date,old_name,original_list_date\n"
    )
    provider = Mock()
    provider.get_daily_price_limits_by_date.side_effect = lambda day: [_limit(day)]
    return storage, history, provider, tmp_path / "receipts"


def _run(setup, **kwargs):
    storage, history, provider, receipts = setup
    return backfill_price_limits(
        lambda: provider,
        storage,
        history,
        receipts,
        **{
            "code_head": "a" * 40,
            "start": DAYS[0],
            "end": DAYS[1],
            "pause": lambda n: None,
            **kwargs,
        },
    )


def test_missing_only_limits_and_zero_call_reuse(setup):
    storage, _, provider, _ = setup
    storage.save_daily_price_limits_by_date([_limit(DAYS[0])], DAYS[0])
    before = storage.daily_price_limit_path(DAYS[0]).read_bytes()
    path, report = _run(setup)
    assert report["status"] == "complete"
    assert report["provider_calls"] == len(report["accepted"]) == 1
    assert len(list(path.parent.glob("request-*.json"))) == 1
    assert storage.daily_price_limit_path(DAYS[0]).read_bytes() == before
    provider.reset_mock()
    _, result = _run(setup, max_calls=0)
    assert result["status"] == "complete" and result["provider_calls"] == 0
    provider.get_daily_price_limits_by_date.assert_not_called()
    assert not result["full_tradability_verified"] and not result["performance_eligible"]


@pytest.mark.parametrize(
    "bad_rows",
    [
        lambda day: [replace(_limit(day), down_limit=0)],
        lambda day: [replace(_limit(day), up_limit=float("nan"))],
        lambda day: [_limit(day)] * 2,
        lambda day: [replace(_limit(day), trade_date=date(2020, 1, 6))],
        lambda day: [],
    ],
)
def test_rejected_responses_stay_diagnostic_and_can_resume(setup, bad_rows):
    storage, _, provider, _ = setup
    provider.get_daily_price_limits_by_date.side_effect = lambda day: (
        bad_rows(day) if day == DAYS[0] else [_limit(day)]
    )
    path, report = _run(setup)
    assert report["status"] == "complete_with_unresolved_dates"
    assert report["missing_after"] == [str(DAYS[0])]
    assert len(report["rejected"]) == len(report["accepted"]) == 1
    assert not storage.daily_price_limit_exists(DAYS[0])
    assert list(path.parent.glob("rejected-*.json"))
    provider.reset_mock()
    provider.get_daily_price_limits_by_date.side_effect = lambda day: [_limit(day)]
    _, resumed = _run(setup)
    assert resumed["status"] == "complete" and resumed["provider_calls"] == 1
    assert provider.get_daily_price_limits_by_date.call_args.args == (DAYS[0],)


def test_provider_error_stops_and_has_no_favorable_fallback(setup):
    storage, _, provider, _ = setup
    provider.get_daily_price_limits_by_date.side_effect = RuntimeError("sensitive synthetic error")
    path, report = _run(setup)
    assert report["status"] == "partial_failed" and report["provider_calls"] == 1
    assert len(report["missing_after"]) == 2
    assert "sensitive synthetic error" not in path.read_text()
    assert not storage.daily_price_limit_exists(DAYS[0])


def test_interrupted_provider_attempt_is_reported_as_interrupted(setup):
    setup[2].get_daily_price_limits_by_date.side_effect = KeyboardInterrupt
    _, report = _run(setup)
    assert report["status"] == "interrupted" and report["provider_calls"] == 1
    assert len(report["missing_after"]) == 2


@pytest.mark.parametrize(
    "options",
    [
        {"start": date(2019, 12, 31)},
        {"max_calls": 1},
        {"max_calls": 1619},
        {"max_calls": True},
        {"code_head": "short"},
    ],
)
def test_scope_budget_validation_precedes_provider(setup, options):
    with pytest.raises(DataValidationError):
        _run(setup, **options)
    setup[2].get_daily_price_limits_by_date.assert_not_called()


def test_new_output_drift_and_daily_drift_are_reported(setup):
    storage, _, provider, _ = setup

    def mutate(day):
        if day == DAYS[1]:
            storage.daily_price_limit_path(DAYS[0]).write_bytes(b"concurrent modification")
        return [_limit(day)]

    provider.get_daily_price_limits_by_date.side_effect = mutate
    _, report = _run(setup)
    assert report["status"] == "source_drift"
    assert str(storage.daily_price_limit_path(DAYS[0])) in report["changed_sources"]
