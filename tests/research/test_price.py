from datetime import date

import pytest

from quantlab.data.models import AdjFactor, DailyBar, DataValidationError
from quantlab.research import build_prices, filter_point_in_time, prices_to_frame


def _bar(instrument_id, trade_date, close, **overrides):
    values = dict(
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
    values.update(overrides)
    return DailyBar(**values)


def _factor(instrument_id, trade_date, adj_factor):
    return AdjFactor(instrument_id=instrument_id, trade_date=trade_date, adj_factor=adj_factor)


def test_build_prices_computes_adj_close() -> None:
    d = date(2026, 1, 5)
    prices = build_prices([_bar("600519.SH", d, close=100.0)], [_factor("600519.SH", d, 2.5)])
    assert len(prices) == 1
    assert prices[0].adj_close == 250.0
    assert prices[0].adj_factor == 2.5


def test_build_prices_missing_factor_raises() -> None:
    d = date(2026, 1, 5)
    with pytest.raises(DataValidationError):
        build_prices([_bar("600519.SH", d, close=100.0)], [])


def test_build_prices_missing_factor_strict_false_drops() -> None:
    d = date(2026, 1, 5)
    assert build_prices([_bar("600519.SH", d, close=100.0)], [], strict=False) == []


def test_filter_point_in_time_unlisted() -> None:
    d = date(2010, 1, 4)
    prices = build_prices([_bar("600519.SH", d, close=100.0)], [_factor("600519.SH", d, 1.0)])
    list_dates = {"600519.SH": date(2010, 6, 1)}
    assert filter_point_in_time(prices, list_dates) == []


def test_filter_point_in_time_delisted() -> None:
    d = date(2026, 1, 5)
    prices = build_prices([_bar("600519.SH", d, close=100.0)], [_factor("600519.SH", d, 1.0)])
    list_dates = {"600519.SH": date(2001, 8, 27)}
    delist_dates = {"600519.SH": date(2025, 12, 31)}
    assert filter_point_in_time(prices, list_dates, delist_dates) == []


def test_filter_point_in_time_unknown_instrument_raises() -> None:
    d = date(2026, 1, 5)
    prices = build_prices([_bar("600519.SH", d, close=100.0)], [_factor("600519.SH", d, 1.0)])
    with pytest.raises(DataValidationError):
        filter_point_in_time(prices, {})


def test_prices_to_frame_sorted() -> None:
    d = date(2026, 1, 5)
    prices = build_prices(
        [_bar("600519.SH", d, close=100.0), _bar("000001.SZ", d, close=10.0)],
        [_factor("600519.SH", d, 1.0), _factor("000001.SZ", d, 1.0)],
    )
    df = prices_to_frame(prices)
    assert df["instrument_id"].tolist() == ["000001.SZ", "600519.SH"]
    assert list(df.columns) == ["instrument_id", "trade_date", "close", "adj_factor", "adj_close"]
