from datetime import date

import pandas as pd
import pytest

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    IndexDailyBar,
    Security,
    TradingCalendar,
)
from quantlab.data.storage import DuplicateDataError, ParquetStorage, find_duplicates


def _make_security(**overrides) -> Security:
    values = dict(
        instrument_id="600519.SH",
        symbol="600519",
        name="贵州茅台",
        exchange="SSE",
        market="SH",
        board="主板",
        list_status="L",
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


def _make_factor(**overrides) -> AdjFactor:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        adj_factor=1.5,
    )
    values.update(overrides)
    return AdjFactor(**values)


def _make_daily_basic(**overrides) -> DailyBasic:
    values = dict(
        instrument_id="600519.SH",
        trade_date=date(2026, 1, 2),
        turnover_rate=0.052,
        total_mv=1_230_000.0,
        circ_mv=1_000_000.0,
    )
    values.update(overrides)
    return DailyBasic(**values)


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
    trade_date = date(2026, 1, 2)
    expected = [_make_bar()]
    storage.save_daily_bars_by_date(expected, trade_date)
    loaded = storage.load_daily_bars_by_date(trade_date)
    assert loaded == expected
    assert isinstance(loaded[0].trade_date, date)


def test_daily_bars_path(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    path = storage.daily_bars_path(date(2026, 9, 1))
    assert str(path).endswith("daily/year=2026/month=09/2026-09-01.parquet")


def test_daily_bars_sorted_by_instrument_id(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    bars = [
        _make_bar(instrument_id="600519.SH"),
        _make_bar(instrument_id="000001.SZ"),
    ]
    storage.save_daily_bars_by_date(bars, trade_date)
    loaded = storage.load_daily_bars_by_date(trade_date)
    assert [item.instrument_id for item in loaded] == ["000001.SZ", "600519.SH"]


def test_daily_bars_duplicate_instrument_raises(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    with pytest.raises(DuplicateDataError):
        storage.save_daily_bars_by_date([_make_bar(), _make_bar()], trade_date)


def test_atomic_write_failure_leaves_no_file(tmp_path, monkeypatch) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    path = storage.daily_bars_path(trade_date)

    def _fail(self, *args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pandas.DataFrame.to_parquet", _fail)
    with pytest.raises(OSError):
        storage.save_daily_bars_by_date([_make_bar()], trade_date)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_atomic_write_no_temp_leftover(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    path = storage.daily_bars_path(trade_date)
    storage.save_daily_bars_by_date([_make_bar()], trade_date)
    assert path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_adj_factors_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    expected = [_make_factor()]
    storage.save_adj_factors_by_date(expected, trade_date)
    loaded = storage.load_adj_factors_by_date(trade_date)
    assert loaded == expected
    assert isinstance(loaded[0].trade_date, date)


def test_adj_factor_path(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    path = storage.adj_factor_path(date(2026, 9, 1))
    assert str(path).endswith("adj_factor/year=2026/month=09/2026-09-01.parquet")


def test_adj_factors_atomic_write(tmp_path, monkeypatch) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    path = storage.adj_factor_path(trade_date)

    def _fail(self, *args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pandas.DataFrame.to_parquet", _fail)
    with pytest.raises(OSError):
        storage.save_adj_factors_by_date([_make_factor()], trade_date)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []


def test_find_duplicates() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "a", "b"],
        "trade_date": ["2026-01-01", "2026-01-01", "2026-01-02"],
    })
    duplicates = find_duplicates(frame, ["instrument_id", "trade_date"])
    assert len(duplicates) == 2


def _cal(exchange, trade_date, is_open=True) -> TradingCalendar:
    return TradingCalendar(exchange=exchange, trade_date=trade_date, is_open=is_open)


def test_upsert_calendar_first_write(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    entry = _cal("SSE", date(2026, 1, 5))
    storage.upsert_trading_calendar([entry])
    assert storage.load_trading_calendar() == [entry]


def test_upsert_calendar_keeps_old_dates(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5))])
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 6))])
    loaded = storage.load_trading_calendar()
    assert {(c.exchange, c.trade_date) for c in loaded} == {
        ("SSE", date(2026, 1, 5)),
        ("SSE", date(2026, 1, 6)),
    }


def test_upsert_calendar_incoming_wins(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5), is_open=True)])
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5), is_open=False)])
    loaded = storage.load_trading_calendar()
    assert len(loaded) == 1
    assert loaded[0].is_open is False


def test_upsert_calendar_sse_szse_separate(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([
        _cal("SSE", date(2026, 1, 5)),
        _cal("SZSE", date(2026, 1, 5)),
    ])
    assert len(storage.load_trading_calendar()) == 2


def test_upsert_calendar_no_duplicate_key(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5))])
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5))])
    assert len(storage.load_trading_calendar()) == 1


def test_upsert_calendar_atomic_write(tmp_path, monkeypatch) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 5))])

    def _fail(self, *args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pandas.DataFrame.to_parquet", _fail)
    with pytest.raises(OSError):
        storage.upsert_trading_calendar([_cal("SSE", date(2026, 1, 6))])
    loaded = storage.load_trading_calendar()
    assert [(c.exchange, c.trade_date) for c in loaded] == [("SSE", date(2026, 1, 5))]


def test_upsert_calendar_first_write_sorted(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_trading_calendar([
        _cal("SSE", date(2026, 1, 6)),
        _cal("SZSE", date(2026, 1, 5)),
        _cal("SSE", date(2026, 1, 5)),
    ])
    loaded = storage.load_trading_calendar()
    keys = [(c.trade_date, c.exchange) for c in loaded]
    assert keys == sorted(keys)


def test_upsert_securities_first_write(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    entry = _make_security()
    storage.upsert_securities([entry])
    assert storage.load_securities() == [entry]


def test_upsert_securities_keeps_old(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_securities([_make_security()])
    storage.upsert_securities([_make_security(instrument_id="000001.SZ", symbol="000001")])
    loaded = storage.load_securities()
    assert {s.instrument_id for s in loaded} == {"600519.SH", "000001.SZ"}


def test_upsert_securities_incoming_wins(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_securities([_make_security(name="旧名")])
    storage.upsert_securities([_make_security(name="新名")])
    loaded = storage.load_securities()
    assert len(loaded) == 1
    assert loaded[0].name == "新名"


def test_upsert_securities_no_duplicate(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_securities([_make_security()])
    storage.upsert_securities([_make_security()])
    assert len(storage.load_securities()) == 1


def test_upsert_securities_sorted(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_securities([
        _make_security(instrument_id="600519.SH"),
        _make_security(instrument_id="000001.SZ", symbol="000001"),
    ])
    loaded = storage.load_securities()
    ids = [s.instrument_id for s in loaded]
    assert ids == sorted(ids)


def test_upsert_securities_atomic_write(tmp_path, monkeypatch) -> None:
    storage = ParquetStorage(tmp_path)
    storage.upsert_securities([_make_security()])

    def _fail(self, *args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pandas.DataFrame.to_parquet", _fail)
    with pytest.raises(OSError):
        storage.upsert_securities([_make_security(instrument_id="000001.SZ", symbol="000001")])
    loaded = storage.load_securities()
    assert [s.instrument_id for s in loaded] == ["600519.SH"]


def test_daily_basic_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    expected = [_make_daily_basic()]
    storage.save_daily_basic_by_date(expected, trade_date)
    loaded = storage.load_daily_basic_by_date(trade_date)
    assert loaded == expected
    assert isinstance(loaded[0].trade_date, date)


def test_daily_basic_duplicate_raises(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    with pytest.raises(DuplicateDataError):
        storage.save_daily_basic_by_date([_make_daily_basic(), _make_daily_basic()], trade_date)


def _make_index_bar(**overrides) -> IndexDailyBar:
    values = dict(
        instrument_id="000300.SH",
        trade_date=date(2026, 1, 2),
        open=3980.0,
        high=4010.0,
        low=3975.0,
        close=4000.0,
        pre_close=3990.0,
        volume=1_234_567_800.0,
        amount=123_456_789_000.0,
    )
    values.update(overrides)
    return IndexDailyBar(**values)


def test_index_daily_round_trip(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    expected = [_make_index_bar()]
    storage.save_index_daily_by_date(expected, trade_date)
    loaded = storage.load_index_daily_by_date(trade_date)
    assert loaded == expected
    assert isinstance(loaded[0].trade_date, date)
    assert storage.index_daily_exists(trade_date)
    assert not storage.index_daily_exists(date(2026, 1, 3))


def test_index_daily_path(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    path = storage.index_daily_path(date(2026, 9, 1))
    assert str(path).endswith("index_daily/year=2026/month=09/2026-09-01.parquet")


def test_index_daily_missing_day_loads_empty(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    assert storage.load_index_daily_by_date(date(2026, 1, 2)) == []


def test_index_daily_duplicate_raises(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    with pytest.raises(DuplicateDataError):
        storage.save_index_daily_by_date([_make_index_bar(), _make_index_bar()], trade_date)


def test_index_daily_sorted_by_instrument_id(tmp_path) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    bars = [
        _make_index_bar(instrument_id="000905.SH"),
        _make_index_bar(instrument_id="000300.SH"),
    ]
    storage.save_index_daily_by_date(bars, trade_date)
    loaded = storage.load_index_daily_by_date(trade_date)
    assert [item.instrument_id for item in loaded] == ["000300.SH", "000905.SH"]


def test_daily_basic_atomic_write(tmp_path, monkeypatch) -> None:
    storage = ParquetStorage(tmp_path)
    trade_date = date(2026, 1, 2)
    path = storage.daily_basic_path(trade_date)

    def _fail(self, *args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr("pandas.DataFrame.to_parquet", _fail)
    with pytest.raises(OSError):
        storage.save_daily_basic_by_date([_make_daily_basic()], trade_date)
    assert not path.exists()
    assert list(path.parent.glob("*.tmp")) == []
