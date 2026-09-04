from datetime import date, timedelta

import pandas as pd

from quantlab.research import calculate_returns


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


def test_return_5d_correct() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    df = calculate_returns(_frame("600519.SH", list(zip(days, closes, strict=True))), horizons=(5,))
    last = df[df["trade_date"] == days[5]]
    assert abs(last["return_5d"].iloc[0] - (161.051 / 100.0 - 1)) < 1e-9
    assert df["return_5d"].iloc[:5].isna().all()


def test_no_cross_stock() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    a = _frame("600519.SH", [(d, 100.0) for d in days])
    b = _frame("000001.SZ", [(d, 50.0) for d in days])
    df = calculate_returns(pd.concat([a, b], ignore_index=True), horizons=(1,))
    assert (df["return_1d"].fillna(0.0) == 0.0).all()


def test_missing_dates_no_forward_fill() -> None:
    days = [date(2026, 1, 5), date(2026, 1, 12)]
    df = calculate_returns(_frame("600519.SH", [(days[0], 100.0), (days[1], 200.0)]), horizons=(1,))
    assert abs(df["return_1d"].iloc[1] - 1.0) < 1e-9


def test_no_future_data() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    df = calculate_returns(_frame("600519.SH", list(zip(days, closes, strict=True))), horizons=(1,))
    assert pd.isna(df["return_1d"].iloc[0])
    for i in range(1, 6):
        assert abs(df["return_1d"].iloc[i] - (closes[i] / closes[i - 1] - 1)) < 1e-9


def test_sort_order_independent() -> None:
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(6)]
    closes = [100.0, 110.0, 121.0, 133.1, 146.41, 161.051]
    frame = _frame("600519.SH", list(zip(days, closes, strict=True)))
    reversed_frame = frame.iloc[::-1].reset_index(drop=True)
    df1 = calculate_returns(frame, horizons=(5,))
    df2 = calculate_returns(reversed_frame, horizons=(5,))
    pd.testing.assert_frame_equal(df1, df2)
