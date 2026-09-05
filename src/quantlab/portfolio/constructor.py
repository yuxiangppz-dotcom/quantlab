"""Rank-based target portfolio construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.portfolio.models import TargetPortfolio, TargetWeight

_VALID_DIRECTIONS = ("higher_is_better", "lower_is_better")
_REQUIRED_COLUMNS = ("instrument_id", "trade_date", "alpha_score")


@dataclass(frozen=True)
class RankPortfolioConfig:
    """Configuration for the long-only equal-weight rank constructor."""

    selection_fraction: float
    score_direction: str
    gross_exposure: float = 1.0
    max_weight_per_name: float | None = None

    def __post_init__(self) -> None:
        if not (0 < self.selection_fraction <= 1):
            raise ValueError(
                f"selection_fraction must be in (0, 1], got {self.selection_fraction}"
            )
        if self.score_direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"score_direction must be one of {_VALID_DIRECTIONS}, "
                f"got {self.score_direction!r}"
            )
        if self.gross_exposure <= 0:
            raise ValueError(f"gross_exposure must be > 0, got {self.gross_exposure}")
        if self.max_weight_per_name is not None and self.max_weight_per_name <= 0:
            raise ValueError(
                f"max_weight_per_name must be > 0, got {self.max_weight_per_name}"
            )


def _select_top(
    valid: pd.DataFrame,
    direction: str,
    selection_fraction: float,
) -> pd.DataFrame:
    """Select the top ``selection_fraction`` names, keeping ties together."""
    ascending = direction == "lower_is_better"
    ordered = valid.sort_values("alpha_score", ascending=ascending, kind="mergesort")
    n = len(ordered)
    k = max(1, int(n * selection_fraction))
    cutoff = ordered["alpha_score"].iloc[k - 1]
    if direction == "higher_is_better":
        return valid[valid["alpha_score"] >= cutoff]
    return valid[valid["alpha_score"] <= cutoff]


def construct_rank_portfolio(
    alpha_frame: pd.DataFrame,
    as_of: date,
    config: RankPortfolioConfig,
) -> TargetPortfolio:
    """Build an equal-weight TargetPortfolio from a single as-of cross-section.

    ``alpha_frame`` must have ``instrument_id``, ``trade_date`` and
    ``alpha_score`` columns, with exactly one trade date equal to ``as_of``.
    Future-return columns, if present, are ignored.
    """
    missing = [c for c in _REQUIRED_COLUMNS if c not in alpha_frame.columns]
    if missing:
        raise DataValidationError(f"alpha_frame missing columns: {missing}")

    frame = alpha_frame[["instrument_id", "trade_date", "alpha_score"]].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date

    dates = frame["trade_date"].unique()
    if len(dates) != 1 or dates[0] != as_of:
        raise DataValidationError(
            f"alpha_frame must contain exactly one trade_date equal to {as_of}, "
            f"got {sorted(dates)}"
        )

    if frame["instrument_id"].duplicated().any():
        dupes = frame.loc[frame["instrument_id"].duplicated(), "instrument_id"].unique()
        raise DataValidationError(f"duplicate instrument_id in alpha_frame: {list(dupes)}")

    valid = frame.dropna(subset=["alpha_score"])
    if valid.empty:
        return TargetPortfolio(as_of=as_of, positions=(), cash_weight=config.gross_exposure)

    selected = _select_top(valid, config.score_direction, config.selection_fraction)

    n = len(selected)
    cap = config.max_weight_per_name
    if cap is None:
        weight = config.gross_exposure / n
    else:
        weight = min(config.gross_exposure / n, cap)

    positions = tuple(
        TargetWeight(instrument_id=row.instrument_id, target_weight=weight)
        for row in selected.sort_values("instrument_id").itertuples(index=False)
    )
    cash = config.gross_exposure - n * weight
    return TargetPortfolio(
        as_of=as_of,
        positions=positions,
        cash_weight=cash,
        gross_exposure=config.gross_exposure,
    )


def portfolio_to_frame(portfolio: TargetPortfolio) -> pd.DataFrame:
    """Serialize a TargetPortfolio to a DataFrame (cash kept separate)."""
    return pd.DataFrame({
        "as_of": [portfolio.as_of] * len(portfolio.positions),
        "instrument_id": [p.instrument_id for p in portfolio.positions],
        "target_weight": [p.target_weight for p in portfolio.positions],
    })
