from datetime import date, timedelta

import pytest

from quantlab.data.models import AdjFactor, DailyBar, DataValidationError, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.research import build_research_dataset


def _security(instrument_id, list_date, delist_date=None) -> Security:
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id.split(".")[0],
        name=instrument_id,
        exchange="SSE",
        market="SH",
        board="主板",
        list_status="L",
        list_date=list_date,
        delist_date=delist_date,
    )


def _bar(instrument_id, trade_date, close) -> DailyBar:
    return DailyBar(
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=close,
        high=close,
        low=close,
        close=close,
        pre_close=close,
        volume=1000.0,
        amount=10000.0,
    )


def _factor(instrument_id, trade_date, adj_factor=1.0) -> AdjFactor:
    return AdjFactor(instrument_id=instrument_id, trade_date=trade_date, adj_factor=adj_factor)


def _cal(exchange, trade_date) -> TradingCalendar:
    return TradingCalendar(exchange=exchange, trade_date=trade_date, is_open=True)


def _make_storage(tmp_path, dates, instrument_ids, list_dates=None, delist_dates=None):
    storage = ParquetStorage(tmp_path)
    list_dates = list_dates or {}
    delist_dates = delist_dates or {}
    securities = [
        _security(sid, list_dates.get(sid, dates[0]), delist_dates.get(sid))
        for sid in instrument_ids
    ]
    storage.save_securities(securities)
    calendar = [c for d in dates for c in (_cal("SSE", d), _cal("SZSE", d))]
    storage.save_trading_calendar(calendar)
    for di, d in enumerate(dates):
        close = 100.0 + di
        storage.save_daily_bars_by_date(
            [_bar(sid, d, close) for sid in instrument_ids], d
        )
        storage.save_adj_factors_by_date(
            [_factor(sid, d) for sid in instrument_ids], d
        )
    return storage


def _days(n, start=date(2026, 1, 5)):
    return [start + timedelta(days=i) for i in range(n)]


def test_build_normal(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(storage, dates[2], dates[7])
    assert len(df) == 6
    assert set(df["instrument_id"]) == {"600519.SH"}
    assert set(df["trade_date"]) == set(dates[2:8])


def test_return_and_forward_columns(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(storage, dates[3], dates[6])
    for col in (
        "return_1d", "return_5d", "return_20d",
        "future_return_1d", "future_return_5d", "future_return_20d",
    ):
        assert col in df.columns


def test_start_boundary_padding(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(
        storage, dates[5], dates[7], return_horizons=(5,), forward_horizons=()
    )
    first = df[df["trade_date"] == dates[5]].iloc[0]
    assert abs(first["return_5d"] - (105.0 / 100.0 - 1)) < 1e-9


def test_end_boundary_padding(tmp_path) -> None:
    dates = _days(15)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(
        storage, dates[5], dates[9], return_horizons=(), forward_horizons=(5,)
    )
    last = df[df["trade_date"] == dates[9]].iloc[0]
    assert abs(last["future_return_5d"] - (114.0 / 109.0 - 1)) < 1e-9


def test_padding_uses_market_sessions(tmp_path) -> None:
    dates = [
        date(2026, 1, 5),
        date(2026, 1, 12),
        date(2026, 1, 19),
        date(2026, 1, 26),
        date(2026, 2, 2),
        date(2026, 2, 9),
        date(2026, 2, 16),
    ]
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(
        storage, dates[2], dates[4], return_horizons=(2,), forward_horizons=()
    )
    first = df[df["trade_date"] == dates[2]].iloc[0]
    assert abs(first["return_2d"] - (102.0 / 100.0 - 1)) < 1e-9


def test_output_trimmed(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(storage, dates[3], dates[6])
    assert df["trade_date"].min() == dates[3]
    assert df["trade_date"].max() == dates[6]


def test_unlisted_filtered(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(
        tmp_path, dates, ["600519.SH"], list_dates={"600519.SH": dates[5]}
    )
    df = build_research_dataset(storage, dates[0], dates[9])
    assert df["trade_date"].min() == dates[5]


def test_delisted_filtered(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(
        tmp_path, dates, ["600519.SH"], delist_dates={"600519.SH": dates[5]}
    )
    df = build_research_dataset(storage, dates[0], dates[9])
    assert df["trade_date"].max() == dates[5]


def test_missing_daily_raises(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    storage.daily_bars_path(dates[5]).unlink()
    with pytest.raises(DataValidationError):
        build_research_dataset(storage, dates[0], dates[9])


def test_missing_adj_factor_raises(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    storage.adj_factor_path(dates[5]).unlink()
    with pytest.raises(DataValidationError):
        build_research_dataset(storage, dates[0], dates[9])


def test_calendar_duplicate_ok(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])  # SSE+SZSE 双份 calendar
    df = build_research_dataset(storage, dates[2], dates[7])
    assert len(df) == 6
    assert df["trade_date"].nunique() == 6


def test_empty_range(tmp_path) -> None:
    dates = _days(10)
    storage = _make_storage(tmp_path, dates, ["600519.SH"])
    df = build_research_dataset(storage, date(2026, 2, 1), date(2026, 2, 28))
    assert df.empty
    assert list(df.columns) == [
        "instrument_id", "trade_date", "close", "adj_factor", "adj_close",
        "return_1d", "return_5d", "return_20d",
        "future_return_1d", "future_return_5d", "future_return_20d",
    ]
