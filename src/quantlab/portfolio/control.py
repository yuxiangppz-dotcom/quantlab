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


_SYMMETRY_SPEC_FIELDS: tuple[tuple[str, str], ...] = (
    # (run-spec key, audit check name) — every field that can move performance
    ("open_dates", "same_open_dates"),
    ("signal_dates", "same_signal_schedule"),
    ("execution_lag_sessions", "same_execution_lag"),
    ("cost_bps", "same_cost_bps"),
    ("missing_price_policy", "same_missing_price_policy"),
    ("run_mode", "same_run_mode"),
    ("lifecycle_boundary_mode", "same_lifecycle_boundary_mode"),
    ("lifecycle_monitor_snapshot", "same_lifecycle_monitor_snapshot"),
    ("risk_policy_id", "same_risk_policy_id"),
    ("risk_fact_snapshot", "same_risk_fact_snapshot"),
    ("settlement_recovery_rate", "same_settlement_recovery_rate"),
    ("settlement_fee_bps", "same_settlement_fee_bps"),
    ("initial_nav", "same_initial_nav"),
    ("requested_period", "same_requested_period"),
    ("annualization", "same_annualization"),
)


def strategy_control_symmetry_audit(
    strategy_run: Mapping[str, object],
    control_run: Mapping[str, object],
) -> dict[str, bool]:
    """Audit that strategy and control runs differ ONLY in target construction.

    Both mappings describe one ``run_backtest`` invocation and must be taken
    from the actual run specification (not re-typed constants). Every field
    that can move performance is compared explicitly: open-session calendar,
    signal schedule, execution lag, transaction cost, missing-price policy,
    strict/diagnostic run mode, lifecycle boundary mode, lifecycle monitor
    snapshot (security-master + code-change + mode fingerprint), risk policy
    id, risk fact snapshot, settlement recovery rate and fee bps, initial
    NAV, requested period, and annualization. Target construction and target
    fingerprints are the ONLY allowed asymmetry.

    Raises ``ValueError`` when either run spec is missing a required field —
    a partial spec can never pass by accident.
    """
    required_keys = {key for key, _ in _SYMMETRY_SPEC_FIELDS}
    missing_strategy = required_keys.difference(strategy_run)
    missing_control = required_keys.difference(control_run)
    if missing_strategy or missing_control:
        raise ValueError(
            "run specs missing symmetry fields: "
            f"strategy={sorted(missing_strategy)}, "
            f"control={sorted(missing_control)}"
        )
    return {
        audit_name: strategy_run[spec_key] == control_run[spec_key]
        for spec_key, audit_name in _SYMMETRY_SPEC_FIELDS
    }
