from datetime import date, timedelta

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.research import calculate_forward_returns, calculate_returns


def _frame(instrument_id, closes_by_date, adj_factor=1.0) -> pd.DataFrame:
    """Build a research price DataFrame from a list of (date, close)."""
    rows = []
    for d, close in closes_by_date:
        rows.append({
            "instrument_id": instrument_id,
            "trade_date": d,
            "close": close,
            "adj_factor": adj_factor,
            "adj_close": close * adj_factor,
        })
    return pd.DataFrame(rows)


def test_return_5d_on_calendar() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    df = calculate_returns(
        _frame("600519.SH", list(zip(days, closes, strict=True))), days, horizons=(5,)
    )
    last = df[df["trade_date"] == days[5]]
    assert abs(last["return_5d"].iloc[0] - (161.051 / 100.0 - 1)) < 1e-9
    assert df["return_5d"].iloc[:5].isna().all()


def test_suspended_stock_returns_nan() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    bars = [(days[0], 100.0), (days[2], 200.0)]  # 1/6 停牌
    df = calculate_returns(_frame("600519.SH", bars), days, horizons=(1,))
    assert df["return_1d"].isna().all()


def test_return_uses_global_calendar_session() -> None:
    days = [
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 9),
        date(2026, 1, 12),
    ]
    bars = [
        (days[0], 100.0),  # 1/5
        (days[2], 110.0),  # 1/7（1/6 停牌）
        (days[3], 121.0),  # 1/8
        (days[4], 133.1),  # 1/9
        (days[5], 146.41),  # 1/12
    ]
    df = calculate_returns(_frame("600519.SH", bars), days, horizons=(1,))
    r = dict(zip(df["trade_date"], df["return_1d"], strict=True))
    assert pd.isna(r[days[2]])  # 1/7 目标 1/6 停牌 -> NaN
    assert abs(r[days[3]] - (121.0 / 110.0 - 1)) < 1e-9  # 1/8 目标 1/7 有 bar
    assert abs(r[days[5]] - (146.41 / 133.1 - 1)) < 1e-9  # 1/12 目标 1/9 有 bar


def test_horizon_zero_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_returns(frame, days, horizons=(0,))


def test_horizon_negative_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_returns(frame, days, horizons=(-1,))


def test_horizon_non_integer_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_returns(frame, days, horizons=(1.5,))


def test_no_cross_stock() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    a = _frame("600519.SH", [(d, 100.0) for d in days])
    b = _frame("000001.SZ", [(d, 50.0) for d in days])
    df = calculate_returns(pd.concat([a, b], ignore_index=True), days, horizons=(1,))
    assert (df["return_1d"].fillna(0.0) == 0.0).all()


def test_sort_order_independent() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    frame = _frame("600519.SH", list(zip(days, closes, strict=True)))
    reversed_frame = frame.iloc[::-1].reset_index(drop=True)
    df1 = calculate_returns(frame, days, horizons=(5,))
    df2 = calculate_returns(reversed_frame, days, horizons=(5,))
    pd.testing.assert_frame_equal(df1, df2)


def test_no_future_data() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    df = calculate_returns(
        _frame("600519.SH", list(zip(days, closes, strict=True))), days, horizons=(1,)
    )
    assert pd.isna(df["return_1d"].iloc[0])
    for i in range(1, 6):
        assert abs(df["return_1d"].iloc[i] - (closes[i] / closes[i - 1] - 1)) < 1e-9


def test_duplicate_open_dates_normalized() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    closes = [100.0, 110.0, 121.0]
    frame = _frame("600519.SH", list(zip(days, closes, strict=True)))
    duplicated = [d for d in days for _ in range(2)]
    df1 = calculate_returns(frame, days, horizons=(1,))
    df2 = calculate_returns(frame, duplicated, horizons=(1,))
    pd.testing.assert_frame_equal(df1, df2)


def test_price_date_not_in_calendar_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(date(2026, 1, 7), 100.0)])
    with pytest.raises(DataValidationError):
        calculate_returns(frame, days, horizons=(1,))


def test_nonempty_prices_empty_calendar_raises() -> None:
    frame = _frame("600519.SH", [(date(2026, 1, 5), 100.0)])
    with pytest.raises(DataValidationError):
        calculate_returns(frame, [], horizons=(1,))


def test_forward_return_1d() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
    closes = [100.0, 110.0, 121.0]
    df = calculate_forward_returns(
        _frame("600519.SH", list(zip(days, closes, strict=True))), days, horizons=(1,)
    )
    r = dict(zip(df["trade_date"], df["future_return_1d"], strict=True))
    assert abs(r[days[0]] - (110.0 / 100.0 - 1)) < 1e-9
    assert abs(r[days[1]] - (121.0 / 110.0 - 1)) < 1e-9
    assert pd.isna(r[days[2]])


def test_forward_return_5d() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    df = calculate_forward_returns(
        _frame("600519.SH", list(zip(days, closes, strict=True))), days, horizons=(5,)
    )
    assert abs(df["future_return_5d"].iloc[0] - (161.051 / 100.0 - 1)) < 1e-9
    assert df["future_return_5d"].iloc[1:].isna().all()


def test_forward_target_suspended_returns_nan() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]
    bars = [(days[0], 100.0), (days[1], 110.0)]  # 1/7 停牌
    df = calculate_forward_returns(_frame("600519.SH", bars), days, horizons=(1,))
    r = dict(zip(df["trade_date"], df["future_return_1d"], strict=True))
    assert abs(r[days[0]] - (110.0 / 100.0 - 1)) < 1e-9
    assert pd.isna(r[days[1]])


def test_forward_tail_insufficient_nan() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
    closes = [100.0, 110.0, 121.0]
    df = calculate_forward_returns(
        _frame("600519.SH", list(zip(days, closes, strict=True))), days, horizons=(5,)
    )
    assert df["future_return_5d"].isna().all()


def test_forward_no_cross_stock() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    a = _frame("600519.SH", [(d, 100.0) for d in days])
    b = _frame("000001.SZ", [(d, 50.0) for d in days])
    df = calculate_forward_returns(pd.concat([a, b], ignore_index=True), days, horizons=(1,))
    assert (df["future_return_1d"].fillna(0.0) == 0.0).all()


def test_forward_sort_order_independent() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    frame = _frame("600519.SH", list(zip(days, closes, strict=True)))
    reversed_frame = frame.iloc[::-1].reset_index(drop=True)
    df1 = calculate_forward_returns(frame, days, horizons=(5,))
    df2 = calculate_forward_returns(reversed_frame, days, horizons=(5,))
    pd.testing.assert_frame_equal(df1, df2)


def test_forward_horizon_zero_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_forward_returns(frame, days, horizons=(0,))


def test_forward_horizon_negative_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_forward_returns(frame, days, horizons=(-1,))


def test_forward_horizon_non_integer_raises() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    frame = _frame("600519.SH", [(days[0], 100.0), (days[1], 110.0)])
    with pytest.raises(ValueError):
        calculate_forward_returns(frame, days, horizons=(1.5,))
