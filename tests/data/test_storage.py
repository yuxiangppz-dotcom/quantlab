from datetime import date

import pandas as pd
import pytest

from quantlab.data.models import DailyBar, Security, TradingCalendar
from quantlab.data.storage import DuplicateDataError, ParquetStorage, find_duplicates


def _make_security(**overrides) -> Security:
    values = dict(
        instrument_id="600519.SH",
        symbol="600519",
        name="贵州茅台",
        exchange="SSE",
        market="SH",
        list_date=date(2001, 8, 27),
        delist_date=None,
    )
    values.update(overrides)
    return Security(**values)


def _make_bar(**overrides) -> DailyBar:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        pre_close=99.5,
        volume=10000.0,
        amount=100000.0,
    )
    values.update(overrides)
    return DailyBar(**values)


def test_securities_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    expected = [_make_security()]
    storage.save_securities(expected)
    assert storage.load_securities() == expected


def test_calendar_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    expected = [TradingCalendar(exchange="SSE", trade_date=date(2026, 1, 1), is_open=True)]
    storage.save_trading_calendar(expected)
    assert storage.load_trading_calendar() == expected


def test_daily_bars_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    expected = [_make_bar()]
    storage.save_daily_bars(expected)
    loaded = storage.load_daily_bars()
    assert loaded == expected
    assert isinstance(loaded[0].trade_date, date)


def test_daily_bars_merge_drops_exact_duplicates(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.save_daily_bars([_make_bar()])
    storage.save_daily_bars([_make_bar()])
    assert len(storage.load_daily_bars()) == 1


def test_daily_bars_conflicting_duplicate_raises(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.save_daily_bars([_make_bar()])
    with pytest.raises(DuplicateDataError):
        storage.save_daily_bars([_make_bar(close=999.0)])


def test_find_duplicates() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "a", "b"],
        "trade_date": ["2026-01-01", "2026-01-01", "2026-01-02"],
    })
    duplicates = find_duplicates(frame, ["instrument_id", "trade_date"])
    assert len(duplicates) == 2
