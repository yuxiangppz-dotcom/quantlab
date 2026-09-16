"""Buffered ranks based on actual marked holdings, with explicit cash and risk exits."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.ml.config import MLConfig


@dataclass(frozen=True)
class PortfolioDecision:
    target: TargetPortfolio
    planned_one_way_turnover: float
    risk_reduction_one_way_turnover: float
    initial_entry: bool


def _turnover(before, after):
    return sum(abs(after.get(k, 0) - before.get(k, 0)) for k in before.keys() | after.keys()) / 2


def buffered_target(as_of: date, cross, current, ages, config: MLConfig):
    """Unknown held eligibility/industry stops the decision instead of implying a sale.

    Hard risk reductions precede the normal turnover budget. Initial allocation
    is exempt from that budget. Slots left by unavailable candidates stay cash.
    """
    if cross.instrument_id.duplicated().any():
        raise ValueError("duplicate decision instrument")
    if not cross.eligible.map(lambda x: pd.isna(x) or isinstance(x, (bool, np.bool_))).all():
        raise ValueError("eligibility must be boolean or unknown")
    modern = any(k in cross for k in ("can_open", "must_exit", "soft_exit"))
    if modern:
        for column in ("can_open", "must_exit", "soft_exit"):
            if (
                column not in cross
                or not cross[column].map(lambda x: isinstance(x, (bool, np.bool_))).all()
            ):
                raise ValueError(f"explicit universe state required:{column}")
    if (
        any(not math.isfinite(w) or w < 0 for w in current.values())
        or sum(current.values()) > 1.000001
    ):
        raise ValueError("current weights must be unlevered, finite and nonnegative")
    current = {k: v for k, v in current.items() if v > 0}
    if len(current) > config.max_positions:
        raise ValueError("current holdings exceed position-count limit")
    lookup = cross.set_index("instrument_id")
    for code in current:
        if code not in lookup.index or pd.isna(lookup.at[code, "eligible"]):
            raise ValueError(f"held eligibility unknown:{code}")
        if code not in ages or type(ages[code]) is not int or ages[code] < 0:
            raise ValueError(f"held age unknown:{code}")
    industries = lookup.industry.to_dict()

    def industry(code):
        value = industries.get(code)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"industry unknown:{code}")
        return value

    for code in current:
        industry(code)

    tolerance = config.execution_weight_tolerance
    base = {
        k: (config.max_weight if v > config.max_weight + tolerance else v)
        for k, v in current.items()
        if (not bool(lookup.at[k, "must_exit"]) if modern else bool(lookup.at[k, "eligible"]))
    }
    for group in {industry(k) for k in base}:
        keys = [k for k in base if industry(k) == group]
        total = sum(base[k] for k in keys)
        if total > config.max_industry_weight + tolerance:
            for k in keys:
                base[k] *= config.max_industry_weight / total
    if sum(base.values()) > config.gross_exposure + tolerance:
        ratio = config.gross_exposure / sum(base.values())
        base = {k: v * ratio for k, v in base.items()}

    eligible = cross.eligible.fillna(False) & cross.score.map(
        lambda x: pd.notna(x) and math.isfinite(x)
    )
    ranked = cross.loc[eligible].sort_values(["score", "instrument_id"], ascending=[False, True])
    ranks = {code: i + 1 for i, code in enumerate(ranked.instrument_id)}
    desired = dict(base)
    exits = sorted(
        (
            k
            for k in base
            if (
                ranks.get(k, 0) > config.exit_rank
                or (modern and bool(lookup.at[k, "soft_exit"]) and eligible.any())
            )
            and ages[k] >= config.min_hold_sessions
        ),
        key=lambda k: (-ranks.get(k, len(cross) + 1), k),
    )[: config.max_replacements]
    for k in exits:
        del desired[k]
    slots = config.max_positions - len(desired)
    # After initial entry, additions themselves are also replacement-budgeted.
    if current:
        slots = min(slots, config.max_replacements)
    for code in ranked.instrument_id.head(config.entry_rank):
        if slots == 0:
            break
        if code in desired:
            continue
        if modern and (not bool(lookup.at[code, "can_open"]) or bool(lookup.at[code, "must_exit"])):
            continue
        group = industry(code)
        room = config.max_industry_weight - sum(
            w for k, w in desired.items() if industry(k) == group
        )
        weight = min(
            config.gross_exposure / config.max_positions,
            config.max_weight,
            config.gross_exposure - sum(desired.values()),
            room,
        )
        if weight > 1e-12:
            desired[code] = weight
            slots -= 1
    normal = _turnover(base, desired)
    if current and normal > config.max_one_way_turnover:
        # Scale all discretionary changes together; never dilute forced risk reductions.
        ratio = config.max_one_way_turnover / normal
        desired = {
            k: base.get(k, 0) + ratio * (desired.get(k, 0) - base.get(k, 0))
            for k in base.keys() | desired.keys()
        }
        # Fractional exits + entries can exceed the position count. Defer new names
        # until an exit can actually complete, rather than silently holding > K.
        if sum(w > 1e-12 for w in desired.values()) > config.max_positions:
            desired = {k: w for k, w in desired.items() if k in base}
    desired = {k: w for k, w in desired.items() if w > 1e-12}
    target = TargetPortfolio(
        as_of,
        tuple(TargetWeight(k, desired[k]) for k in sorted(desired)),
        max(0.0, 1 - sum(desired.values())),
    )
    return PortfolioDecision(
        target, _turnover(current, desired), _turnover(current, base), not bool(current)
    )
