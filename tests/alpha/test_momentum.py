import pandas as pd
import pytest

from quantlab.alpha import calculate_momentum_alpha


def _frame(return_20d, future_return_20d) -> pd.DataFrame:
    return pd.DataFrame({
        "instrument_id": ["a", "b"],
        "trade_date": ["2026-01-05", "2026-01-05"],
        "return_20d": return_20d,
        "future_return_20d": future_return_20d,
    })


def test_momentum_equals_return_20d() -> None:
    result = calculate_momentum_alpha(_frame([0.1, -0.2], [0.5, 0.6]), lookback=20)
    assert list(result["alpha_score"]) == [0.1, -0.2]
    assert list(result.columns) == ["instrument_id", "trade_date", "alpha_score"]


def test_momentum_ignores_future() -> None:
    a = calculate_momentum_alpha(_frame([0.1, -0.2], [0.5, 0.6]))
    b = calculate_momentum_alpha(_frame([0.1, -0.2], [999.0, -999.0]))
    assert a.equals(b)


def test_momentum_missing_column_raises() -> None:
    frame = _frame([0.1, -0.2], [0.5, 0.6]).drop(columns=["return_20d"])
    with pytest.raises(ValueError):
        calculate_momentum_alpha(frame, lookback=20)


def test_momentum_nan_passthrough() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "b"],
        "trade_date": ["2026-01-05", "2026-01-05"],
        "return_20d": [0.1, float("nan")],
        "future_return_20d": [0.5, 0.6],
    })
    result = calculate_momentum_alpha(frame)
    assert result["alpha_score"].isna().tolist() == [False, True]
