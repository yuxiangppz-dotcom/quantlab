import numpy as np
import pandas as pd

from quantlab.research import daily_rank_ic, quantile_returns


def _frame(alpha, future, trade_date=1) -> pd.DataFrame:
    return pd.DataFrame({
        "instrument_id": [f"s{i}" for i in range(len(alpha))],
        "trade_date": [trade_date] * len(alpha),
        "alpha_score": list(alpha),
        "future_return_5d": list(future),
    })


def test_rank_ic_perfect_positive() -> None:
    ic = daily_rank_ic(_frame([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), "future_return_5d", min_count=3)
    assert ic.iloc[0] == 1.0


def test_rank_ic_perfect_negative() -> None:
    ic = daily_rank_ic(_frame([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]), "future_return_5d", min_count=3)
    assert ic.iloc[0] == -1.0


def test_rank_ic_ignores_nan() -> None:
    frame = pd.DataFrame({
        "instrument_id": ["a", "b", "c", "d"],
        "trade_date": [1, 1, 1, 1],
        "alpha_score": [1.0, 2.0, 3.0, 4.0],
        "future_return_5d": [1.0, 2.0, np.nan, 4.0],
    })
    ic = daily_rank_ic(frame, "future_return_5d", min_count=3)
    # valid rows: a(1,1), b(2,2), d(4,4) -> perfect positive
    assert ic.iloc[0] == 1.0


def test_rank_ic_insufficient_returns_nan() -> None:
    ic = daily_rank_ic(_frame([1.0, 2.0], [1.0, 2.0]), "future_return_5d", min_count=30)
    assert pd.isna(ic.iloc[0])


def test_quantile_spread() -> None:
    frame = _frame([1.0, 2.0, 3.0, 4.0, 5.0], [10.0, 20.0, 30.0, 40.0, 50.0])
    q = quantile_returns(frame, "future_return_5d", n_quantiles=5, min_count=5)
    assert q["Q1"].iloc[0] == 10.0
    assert q["Q5"].iloc[0] == 50.0
    assert q["Q5_minus_Q1"].iloc[0] == 40.0


def test_quantile_monotonic() -> None:
    frame = _frame(
        [1.0, 2.0, 3.0, 4.0, 5.0],
        [10.0, 20.0, 30.0, 40.0, 50.0],
    )
    q = quantile_returns(frame, "future_return_5d", n_quantiles=5, min_count=5)
    means = [q[f"Q{i}"].iloc[0] for i in range(1, 6)]
    assert means == sorted(means)


def test_cross_sectional_independent() -> None:
    frame = pd.concat([
        _frame([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], trade_date=1),
        _frame([1.0, 2.0, 3.0], [3.0, 2.0, 1.0], trade_date=2),
    ], ignore_index=True)
    ic = daily_rank_ic(frame, "future_return_5d", min_count=3)
    assert ic.loc[1] == 1.0
    assert ic.loc[2] == -1.0
