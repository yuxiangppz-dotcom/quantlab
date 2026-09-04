"""20D momentum alpha."""

from __future__ import annotations

import pandas as pd


def calculate_momentum_alpha(
    research_df: pd.DataFrame,
    lookback: int = 20,
) -> pd.DataFrame:
    """Return ``alpha_score = return_{lookback}d`` per instrument/date.

    The score only depends on information available at (and before) ``t``.
    ``future_return_*`` columns are never used.
    """
    column = f"return_{lookback}d"
    if column not in research_df.columns:
        raise ValueError(f"research_df has no '{column}' column")
    result = research_df[["instrument_id", "trade_date"]].copy()
    result["alpha_score"] = research_df[column].to_numpy()
    return result
