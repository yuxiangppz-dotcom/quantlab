"""Alpha evaluation: daily RankIC and quantile returns."""

from __future__ import annotations

import numpy as np
import pandas as pd

MIN_VALID_STOCKS = 30


def daily_rank_ic(
    frame: pd.DataFrame,
    future_column: str,
    min_count: int = MIN_VALID_STOCKS,
) -> pd.Series:
    """Per-trade-date Spearman rank correlation of alpha vs future return.

    Days with fewer than ``min_count`` valid stocks yield NaN (not an error).
    """
    def _ic(group: pd.DataFrame) -> float:
        valid = group[["alpha_score", future_column]].dropna()
        if len(valid) < min_count:
            return float("nan")
        # Spearman = Pearson correlation of ranks (no scipy dependency)
        alpha_rank = valid["alpha_score"].rank()
        future_rank = valid[future_column].rank()
        return alpha_rank.corr(future_rank)

    return frame.groupby("trade_date", sort=True).apply(_ic)


def summarize_ic(ic: pd.Series) -> dict[str, float]:
    valid = ic.dropna()
    if valid.empty:
        return {
            "mean_rank_ic": float("nan"),
            "median_rank_ic": float("nan"),
            "std_rank_ic": float("nan"),
            "positive_ratio": float("nan"),
            "valid_days": 0,
        }
    return {
        "mean_rank_ic": float(valid.mean()),
        "median_rank_ic": float(valid.median()),
        "std_rank_ic": float(valid.std()),
        "positive_ratio": float((valid > 0).mean()),
        "valid_days": int(len(valid)),
    }


def _assign_quantiles(frame: pd.DataFrame, n_quantiles: int) -> pd.Series:
    """Assign quantiles via average rank so ties share the same quantile.

    Uses ``rank(method='average', pct=True)``: identical alpha scores map to the
    same quantile and are never split by instrument_id or row order. As a
    result, groups are not necessarily equal-sized and some quantiles may be
    empty (an empty Q1 or Q5 makes the Q5-Q1 spread NaN).
    """
    pct = frame["alpha_score"].rank(method="average", pct=True)
    quantile = np.ceil(pct * n_quantiles).astype(int) - 1
    return quantile.clip(0, n_quantiles - 1)


def quantile_returns(
    frame: pd.DataFrame,
    future_column: str,
    n_quantiles: int = 5,
    min_count: int = MIN_VALID_STOCKS,
) -> pd.DataFrame:
    """Per-trade-date average future return by alpha quantile (Q1..Q5).

    Q1 is the lowest alpha quantile and Q5 the highest. A spread column
    ``Q{5}_minus_Q1`` is appended.
    """
    if n_quantiles <= 1:
        raise ValueError(f"n_quantiles must be > 1, got {n_quantiles}")
    if min_count <= 0:
        raise ValueError(f"min_count must be > 0, got {min_count}")

    rows = []
    for trade_date, group in frame.groupby("trade_date", sort=True):
        valid = group[["instrument_id", "alpha_score", future_column]].dropna()
        if len(valid) < min_count:
            continue
        valid = valid.copy()
        valid["q"] = _assign_quantiles(valid, n_quantiles)
        means = valid.groupby("q")[future_column].mean()
        row: dict = {"trade_date": trade_date}
        for q in range(n_quantiles):
            row[f"Q{q + 1}"] = means.get(q, np.nan)
        rows.append(row)

    result = pd.DataFrame(rows)
    if not result.empty:
        result[f"Q{n_quantiles}_minus_Q1"] = result[f"Q{n_quantiles}"] - result["Q1"]
    return result


def summarize_quantiles(qdf: pd.DataFrame, n_quantiles: int = 5) -> dict[str, float]:
    if qdf.empty:
        summary: dict[str, float] = {}
        for q in range(1, n_quantiles + 1):
            summary[f"Q{q}_mean"] = float("nan")
        summary["spread_mean"] = float("nan")
        return summary
    summary = {}
    for q in range(1, n_quantiles + 1):
        summary[f"Q{q}_mean"] = float(qdf[f"Q{q}"].mean())
    summary["spread_mean"] = float(qdf[f"Q{n_quantiles}_minus_Q1"].mean())
    return summary
