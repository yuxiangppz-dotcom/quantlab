"""Bounded, transparent daily factor definitions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    definition: str
    direction: str
    dependencies: tuple[str, ...]
    version: str = "v1"
    alpha158_relation: str = "style_subset_not_exact_expression"


FACTOR_REGISTRY = (
    FactorDefinition("momentum_1d", "return_1d", "higher_is_better", ("daily", "adj_factor")),
    FactorDefinition("momentum_5d", "return_5d", "higher_is_better", ("daily", "adj_factor")),
    FactorDefinition("momentum_20d", "return_20d", "higher_is_better", ("daily", "adj_factor")),
    FactorDefinition("reversal_20d", "-return_20d", "higher_is_better", ("daily", "adj_factor")),
    FactorDefinition("intraday_strength", "(close-open)/open", "higher_is_better", ("daily",)),
    FactorDefinition("low_amplitude", "-(high-low)/open", "higher_is_better", ("daily",)),
    FactorDefinition("liquidity_log_amount", "log1p(amount_CNY)", "higher_is_better", ("daily",)),
    FactorDefinition("turnover_activity", "turnover_rate", "higher_is_better", ("daily_basic",)),
    FactorDefinition("small_size", "-log(circ_mv_CNY)", "higher_is_better", ("daily_basic",)),
    FactorDefinition("float_ratio", "circ_mv/total_mv", "higher_is_better", ("daily_basic",)),
)


def registry_rows() -> list[dict]:
    return [asdict(item) for item in FACTOR_REGISTRY]


def build_factor_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Create the registered PIT features from same-day or historical inputs."""
    required = {
        "return_1d", "return_5d", "return_20d", "open", "high", "low", "close",
        "amount", "turnover_rate", "circ_mv", "total_mv",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"factor input missing columns: {sorted(missing)}")
    result = frame.copy()
    result["momentum_1d"] = result["return_1d"]
    result["momentum_5d"] = result["return_5d"]
    result["momentum_20d"] = result["return_20d"]
    result["reversal_20d"] = -result["return_20d"]
    result["intraday_strength"] = (result["close"] - result["open"]) / result["open"]
    result["low_amplitude"] = -(result["high"] - result["low"]) / result["open"]
    result["liquidity_log_amount"] = result["amount"].map(
        lambda value: math.log1p(value) if value >= 0 else math.nan
    )
    result["turnover_activity"] = result["turnover_rate"]
    result["small_size"] = -result["circ_mv"].map(
        lambda value: math.log(value) if value > 0 else math.nan
    )
    result["float_ratio"] = result["circ_mv"] / result["total_mv"]
    return result


def add_transparent_combination(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Equal-weight cross-sectional winsorized z-score combination."""
    result = frame.copy()
    components = []
    for column in columns:
        def normalize(group: pd.Series) -> pd.Series:
            valid = group.dropna()
            if valid.empty:
                return pd.Series(float("nan"), index=group.index)
            lower, upper = valid.quantile([0.01, 0.99])
            clipped = group.clip(lower, upper)
            std = clipped.std(ddof=0)
            return (clipped - clipped.mean()) / std if std > 0 else clipped * 0

        normalized = result.groupby("trade_date", group_keys=False)[column].transform(normalize)
        components.append(normalized)
    result["transparent_combo_v1"] = pd.concat(components, axis=1).mean(axis=1)
    return result
