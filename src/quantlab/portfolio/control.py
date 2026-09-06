"""V1 Equal-Weight Control Portfolio.

The formal control is a REAL self-financing portfolio, apples-to-apples with
the strategy: one equal-weight TargetPortfolio over the full PIT-eligible V1
cross-section per rebalance date, then executed by the SAME backtest engine
with the same execution lag, missing-price rule, lifecycle monitor, risk
policy, settlement scenario, transaction cost, initial NAV and requested
period. It is NOT the daily-free-rebalance mean of per-instrument returns
(kept separately as ``cross_sectional_equal_weight_return_diagnostic``).

Suspended held names stay in the book stale-marked (engine freeze rule);
unavailable new names stay cash (engine unavailable-new rule) — the control
never fabricates fills.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd

from quantlab.portfolio.models import TargetPortfolio, TargetWeight

CONTROL_PORTFOLIO_NAME = "equal_weight_v1_control"


def build_equal_weight_control_targets(
    eligibility_frame: pd.DataFrame,
    signal_dates: Sequence[date],
) -> dict[date, TargetPortfolio]:
    """Equal-weight 1/N over the full PIT-eligible cross-section per date.

    ``eligibility_frame`` must be the PIT **eligibility** cross-section
    (columns ``instrument_id`` / ``trade_date``; a row means the instrument is
    inside its listing window and not yet lifecycle-invalid under the frozen
    boundary semantics on that date — e.g. built by ``pit_eligibility_frame``
    from the security master). It is deliberately NOT a price-backed research
    universe: an instrument eligible but suspended on the signal date keeps
    its row (and its 1/N weight), instead of silently vanishing from the
    denominator. What happens to an unpriced target is decided by the engine
    — unavailable new names stay cash, held names stay frozen at their stale
    mark.

    A signal date whose cross-section is empty produces no target (the engine
    treats the missing date as a no-op, same as for the strategy). The result
    is keyed by signal date; the engine applies its usual T+1 execution.
    """
    required = {"instrument_id", "trade_date"}
    missing = required.difference(eligibility_frame.columns)
    if missing:
        raise ValueError(f"eligibility_frame missing columns: {sorted(missing)}")

    frame = eligibility_frame[["instrument_id", "trade_date"]].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date

    targets: dict[date, TargetPortfolio] = {}
    for signal_date in signal_dates:
        cross = frame[frame["trade_date"] == signal_date]
        if cross.empty:
            continue
        instruments = sorted(set(cross["instrument_id"].tolist()))
        if cross["instrument_id"].duplicated().any():
            raise ValueError(
                f"duplicate instrument_id in control cross-section on {signal_date}"
            )
        weight = 1.0 / len(instruments)
        targets[signal_date] = TargetPortfolio(
            as_of=signal_date,
            positions=tuple(
                TargetWeight(instrument_id=instrument_id, target_weight=weight)
                for instrument_id in instruments
            ),
            cash_weight=0.0,
        )
    return targets
