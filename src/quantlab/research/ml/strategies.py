"""Portfolio policies share risk admission and the same quantity/accounting path."""

from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.ml.config import MLConfig
from quantlab.research.ml.portfolio import PortfolioDecision, _turnover, buffered_target


class PortfolioPolicy(Protocol):
    name: str
    requires_scores: bool

    def decide(
        self,
        as_of: date,
        cross: pd.DataFrame,
        current: dict[str, float],
        ages: dict[str, int],
        config: MLConfig,
        *,
        scheduled: bool,
    ) -> PortfolioDecision: ...


class BufferedRankPolicy:
    name = "buffered_rank"
    requires_scores = True

    def decide(self, as_of, cross, current, ages, config, *, scheduled):
        if not scheduled:
            cross = cross.assign(score=float("nan"))
        return buffered_target(as_of, cross, current, ages, config)


class EligibleEqualWeightPolicy:
    """All entry-eligible names, no alpha selection; cash reflects unmet constraints.

    Existing illiquid holdings may remain but cannot be increased. Soft membership
    exits respect minimum holding age. Hard risk cuts run every session. The full
    pool deliberately has a different position count from a concentrated model.
    It keeps the same lot, fee, minimum trade, cash and turnover constraints.
    """

    name = "eligible_equal_weight"
    requires_scores = False

    def decide(self, as_of, cross, current, ages, config, *, scheduled):
        if {"can_open", "must_exit", "soft_exit"} - set(cross):
            raise ValueError("equal-weight baseline requires explicit historical universe states")
        risk = buffered_target(as_of, cross.assign(score=float("nan")), current, ages, config)
        if not scheduled:
            return risk
        base = {p.instrument_id: p.target_weight for p in risk.target.positions}
        lookup = cross.set_index("instrument_id")
        entries = set(
            cross.loc[cross.can_open & ~cross.must_exit & cross.eligible, "instrument_id"]
        )
        protected = {
            k
            for k in base
            if not bool(lookup.at[k, "soft_exit"]) or ages[k] < config.min_hold_sessions
        }
        members = entries | protected
        if len(members) > config.max_positions:
            raise ValueError("baseline pool exceeds declared position capacity")
        equal = min(config.max_weight, config.gross_exposure / len(members)) if members else 0
        desired = {}
        for code in sorted(members):
            industry = lookup.at[code, "industry"]
            if not isinstance(industry, str) or not industry.strip():
                raise ValueError(f"industry unknown:{code}")
            desired[code] = equal if code in entries else min(equal, base.get(code, 0))
        for industry in {lookup.at[k, "industry"] for k in desired}:
            codes = [k for k in desired if lookup.at[k, "industry"] == industry]
            total = sum(desired[k] for k in codes)
            if total > config.max_industry_weight:
                for k in codes:
                    desired[k] *= config.max_industry_weight / total
        normal = _turnover(base, desired)
        if current and normal > config.max_one_way_turnover:
            fraction = config.max_one_way_turnover / normal
            desired = {
                k: base.get(k, 0) + fraction * (desired.get(k, 0) - base.get(k, 0))
                for k in base.keys() | desired.keys()
            }
        desired = {k: w for k, w in desired.items() if w > 1e-12}
        return PortfolioDecision(
            TargetPortfolio(
                as_of,
                tuple(TargetWeight(k, desired[k]) for k in sorted(desired)),
                max(0.0, 1 - sum(desired.values())),
            ),
            _turnover(current, desired),
            risk.risk_reduction_one_way_turnover,
            not bool(current),
        )


def portfolio_policy(name: str) -> PortfolioPolicy:
    if name == "buffered_rank":
        return BufferedRankPolicy()
    if name == "eligible_equal_weight":
        return EligibleEqualWeightPolicy()
    raise ValueError(f"unknown portfolio policy:{name}")
