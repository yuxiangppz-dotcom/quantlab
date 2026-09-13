"""Adapt a risk cap decision onto an existing long-only target portfolio.

The adapter is a pure translation of a cap decision into a new intention. It
can only scale existing long weights down and hold the remainder as cash: it
never changes ranking or membership, never adds instruments, never leverages
and never upsizes a lower-risk target. An unknown decision returns a detectable
not-ready result instead of an all-cash target or a silently kept old one.

The result is still a :class:`~quantlab.portfolio.models.TargetPortfolio` — an
intention, not an order. Realized exposure may stay above the cap whenever the
execution ledger cannot sell (limit-down, suspension, T+1). De-risking intent,
target translation and actual execution are three separate facts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.market_risk import STATUS_OK, RiskDecision

# Float weights may drift by a few ulps when summed; far below the portfolio
# model's own 1e-6 conservation tolerance.
_WEIGHT_SUM_TOLERANCE = 1e-9

RESULT_READY = "ready"
RESULT_NOT_READY = "not_ready"


@dataclass(frozen=True)
class RiskTargetResult:
    """Either a translated target or a detectable not-ready outcome.

    ``status == "ready"`` always carries a ``target``. ``not_ready`` never
    carries one: it is neither an all-cash portfolio nor a silent copy of the
    previous target, and the caller must keep the decision pending.
    """

    status: str
    target: TargetPortfolio | None
    reason: str | None
    applied_cap: float | None
    scale_factor: float | None

    def __post_init__(self) -> None:
        if self.status not in (RESULT_READY, RESULT_NOT_READY):
            raise ValueError("status must be ready or not_ready")
        if self.status == RESULT_READY:
            if not isinstance(self.target, TargetPortfolio):
                raise ValueError("a ready result must carry a target portfolio")
            if self.reason is not None:
                raise ValueError("a ready result carries no reason")
        elif self.target is not None:
            raise ValueError("a not-ready result must not carry a target portfolio")


def apply_risk_cap(target: TargetPortfolio, decision: RiskDecision) -> RiskTargetResult:
    """Scale ``target`` down to ``decision``'s cap; unknown stays not-ready."""
    if decision.status != STATUS_OK or decision.cap is None:
        reason = ";".join(decision.reasons) if decision.reasons else "risk decision unknown"
        return RiskTargetResult(RESULT_NOT_READY, None, reason, None, None)
    cap = float(decision.cap)
    if not math.isfinite(cap) or cap < 0.0:
        raise ValueError("risk cap must be a finite non-negative fraction")
    if cap == 0.0:
        return RiskTargetResult(
            RESULT_READY,
            TargetPortfolio(as_of=target.as_of, positions=(), cash_weight=1.0),
            None,
            0.0,
            0.0,
        )
    for position in target.positions:
        if position.target_weight < 0:
            raise ValueError(
                f"risk adapter is long-only; negative weight for "
                f"{position.instrument_id}"
            )
    gross = target.gross_exposure
    if gross <= cap + _WEIGHT_SUM_TOLERANCE:
        return RiskTargetResult(RESULT_READY, target, None, cap, 1.0)
    factor = cap / gross
    scaled = tuple(
        TargetWeight(
            instrument_id=position.instrument_id,
            target_weight=position.target_weight * factor,
        )
        for position in target.positions
    )
    scaled_gross = sum(position.target_weight for position in scaled)
    if scaled_gross > cap + _WEIGHT_SUM_TOLERANCE:
        raise ValueError("scaling failed to respect the cap")
    return RiskTargetResult(
        RESULT_READY,
        TargetPortfolio(
            as_of=target.as_of, positions=scaled, cash_weight=1.0 - scaled_gross
        ),
        None,
        cap,
        factor,
    )
