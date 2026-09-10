"""Explicit mappings from a small QuantLab feature set to Qlib Alpha158.

This module deliberately does not claim that QuantLab implements Alpha158.
It records only expressions whose algebra can be checked exactly against the
installed Qlib 0.9.7 ``Alpha158DL`` definition.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


@dataclass(frozen=True)
class Alpha158Mapping:
    qlib_feature: str
    qlib_expression: str
    quantlab_feature: str
    quantlab_transform: str
    relation: str


ALPHA158_EXACT_SUBSET = (
    Alpha158Mapping(
        qlib_feature="KMID",
        qlib_expression="($close-$open)/$open",
        quantlab_feature="intraday_strength",
        quantlab_transform="identity",
        relation="exact_expression",
    ),
    Alpha158Mapping(
        qlib_feature="KLEN",
        qlib_expression="($high-$low)/$open",
        quantlab_feature="low_amplitude",
        quantlab_transform="negate",
        relation="exact_expression_after_declared_sign_transform",
    ),
)


def alpha158_subset_rows() -> list[dict[str, str]]:
    """Return the auditable two-expression subset registry."""
    return [asdict(item) for item in ALPHA158_EXACT_SUBSET]


def calculate_alpha158_exact_subset(frame: pd.DataFrame) -> pd.DataFrame:
    """Calculate exact Alpha158 expressions over raw, same-session OHLC data."""
    required = {"instrument_id", "trade_date", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Alpha158 subset input missing columns: {sorted(missing)}")
    result = frame[["instrument_id", "trade_date"]].copy()
    result["KMID"] = (frame["close"] - frame["open"]) / frame["open"]
    result["KLEN"] = (frame["high"] - frame["low"]) / frame["open"]
    return result
