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

from collections.abc import Mapping, Sequence
from datetime import date

import pandas as pd

from quantlab.portfolio.models import TargetPortfolio, TargetWeight

CONTROL_PORTFOLIO_NAME = "equal_weight_v1_control"


def build_equal_weight_control_targets(
    universe_frame: pd.DataFrame,
    signal_dates: Sequence[date],
) -> dict[date, TargetPortfolio]:
    """Equal-weight 1/N over the full PIT-eligible cross-section per date.

    ``universe_frame`` must be the already point-in-time filtered V1 universe
    (columns ``instrument_id`` / ``trade_date``; a row means the instrument is
    inside its [list_date, delist_date] window AND traded on that date).
    Eligibility is evaluated strictly per signal date from that frame — no
    future rows are consulted, so newly listed / delisted / suspended
    instruments follow the same PIT rules as the strategy.

    A signal date whose cross-section is empty produces no target (the engine
    treats the missing date as a no-op, same as for the strategy). The result
    is keyed by signal date; the engine applies its usual T+1 execution.
    """
    required = {"instrument_id", "trade_date"}
    missing = required.difference(universe_frame.columns)
    if missing:
        raise ValueError(f"universe_frame missing columns: {sorted(missing)}")

    frame = universe_frame[["instrument_id", "trade_date"]].copy()
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


def strategy_control_symmetry_audit(
    strategy_run: Mapping[str, object],
    control_run: Mapping[str, object],
) -> dict[str, bool]:
    """Audit that strategy and control runs differ ONLY in target construction.

    Both mappings describe one ``run_backtest`` invocation. Checked keys:
    signal/execution calendar dates, transaction cost, run mode (lifecycle
    semantics), risk policy id, risk fact snapshot, settlement recovery
    scenario, execution lag and missing-price policy.
    """
    checks: dict[str, bool] = {
        # identical session calendar => identical signal/execution dates
        "same_dates": strategy_run["open_dates"] == control_run["open_dates"]
        and strategy_run["signal_dates"] == control_run["signal_dates"],
        "same_cost": strategy_run["cost_rate"] == control_run["cost_rate"],
        "same_lifecycle_mode": strategy_run["mode"] == control_run["mode"],
        "same_risk_policy": strategy_run["risk_policy"] == control_run["risk_policy"],
        "same_fact_snapshot": strategy_run["risk_facts"] is control_run["risk_facts"]
        or strategy_run["risk_facts"] == control_run["risk_facts"],
        "same_settlement_recovery": strategy_run["recovery_rate"]
        == control_run["recovery_rate"],
        "same_execution_lag": strategy_run["execution_lag_sessions"]
        == control_run["execution_lag_sessions"],
        "same_missing_price_policy": strategy_run["missing_price_policy"]
        == control_run["missing_price_policy"],
    }
    return checks
