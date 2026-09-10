"""Product-facing portfolio construction adapters.

This module keeps Daily configuration semantics in the portfolio layer instead
of letting UI or workflow code re-implement ranking and weighting rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pandas as pd

from quantlab.data.models import DataValidationError
from quantlab.portfolio.constructor import (
    FixedCountPortfolioConfig,
    construct_fixed_count_portfolio,
)
from quantlab.portfolio.models import TargetPortfolio

DAILY_FIXED_COUNT_TIE_POLICY = "alpha_score_then_instrument_id"


def fixed_count_config_from_daily(config: Mapping[str, object]) -> FixedCountPortfolioConfig:
    """Translate a validated Daily config into the core fixed-count contract.

    The adapter fails closed when the product declares a different tie policy.
    That prevents a historical audit or downstream portfolio path from silently
    claiming to reproduce Daily semantics while resolving equal scores another
    way.
    """
    required = {
        "target_count",
        "score_direction",
        "gross_exposure",
        "max_weight_per_name",
        "tie_policy",
    }
    missing = required - set(config)
    if missing:
        raise DataValidationError(f"Daily portfolio config missing fields: {sorted(missing)}")
    if config["tie_policy"] != DAILY_FIXED_COUNT_TIE_POLICY:
        raise DataValidationError(
            "Daily fixed-count portfolio requires tie_policy="
            f"{DAILY_FIXED_COUNT_TIE_POLICY!r}, got {config['tie_policy']!r}"
        )

    target_count = config["target_count"]
    direction = config["score_direction"]
    gross_exposure = config["gross_exposure"]
    cap = config["max_weight_per_name"]
    if isinstance(target_count, bool) or not isinstance(target_count, int):
        raise DataValidationError("Daily target_count must be a positive integer")
    if not isinstance(direction, str):
        raise DataValidationError("Daily score_direction must be a string")
    if isinstance(gross_exposure, bool) or not isinstance(gross_exposure, int | float):
        raise DataValidationError("Daily gross_exposure must be numeric")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int | float)):
        raise DataValidationError("Daily max_weight_per_name must be numeric or null")

    try:
        return FixedCountPortfolioConfig(
            target_count=target_count,
            score_direction=direction,
            gross_exposure=float(gross_exposure),
            max_weight_per_name=float(cap) if cap is not None else None,
        )
    except ValueError as exc:
        raise DataValidationError(f"invalid Daily fixed-count portfolio config: {exc}") from exc


def construct_daily_fixed_count_portfolio(
    alpha_frame: pd.DataFrame,
    as_of: date,
    config: Mapping[str, object],
) -> TargetPortfolio:
    """Construct a Daily target using the product's declared exact-count rules."""
    return construct_fixed_count_portfolio(
        alpha_frame,
        as_of,
        fixed_count_config_from_daily(config),
    )
