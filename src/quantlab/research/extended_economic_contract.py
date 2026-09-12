"""Fixed, label-independent economic translation of four saved prediction policies."""

from datetime import date
from itertools import product

import numpy as np
import pandas as pd

from quantlab.backtest.audit import fingerprint_targets
from quantlab.data.models import DataValidationError
from quantlab.portfolio.constructor import (
    FixedCountPortfolioConfig,
    construct_fixed_count_portfolio,
)
from quantlab.portfolio.models import TargetPortfolio, TargetWeight

START = "2025-09-01"
SIGNAL_END = "2026-08-31"
END = "2026-09-07"
CONTROL = "alpha158_prediction_cohort_equal_weight_80"
STRATEGIES = ("ridge_weekly", "ridge_monthly", "lightgbm_weekly", "lightgbm_monthly")
POLICIES = (CONTROL, *STRATEGIES)
HORIZONS = (5, 10, 20)
FRICTIONS = (0, 5, 10, 20)
CAPITALS = (50000, 200000, 1000000)
KEYS = ["instrument_id", "trade_date"]
OUT = "data/products/alpha158_economic/alpha158_economic_20260912"
RUNTIME = "data/runtime/research/alpha158_economic_20260912"
SOURCE_CONFIG = "config/alpha158_economic_sources_v1.json"
CONTRACT = {
    "schema": "alpha158_economic_proxy_v1",
    "start": START,
    "signal_end": SIGNAL_END,
    "end": END,
    "policies": list(POLICIES),
    "horizons": list(HORIZONS),
    "additional_per_side_friction_bps": list(FRICTIONS),
    "capital_scalings": list(CAPITALS),
    "unique_paths": 60,
    "top_count": 20,
    "gross_exposure": 0.8,
    "name_cap": 0.04,
    "execution_lag_sessions": 1,
    "mark": "raw_close_times_adj_factor",
    "value_transfer": "next_market_session_adjusted_close",
    "lifecycle_mode": "legacy_delist_date_inclusive",
    "risk_policy": "exit_after_termination_decision_v1",
    "risk_facts_path": "config/delisting_facts.json",
    "settlement": None,
    "mode": "strict",
    "initial_nav": 1.0,
    "no_terminal_liquidation": True,
    "history_already_observed": True,
    "historical_market_coverage_complete": False,
    "complete_user_fee_accounting": False,
    "execution_authority": False,
    "new_fit_budget": 0,
    "provider_calls": 0,
    "max_rss_bytes": 8 * 1024**3,
    "minimum_available_memory_bytes": 8 * 1024**3,
    "max_generated_bytes": 2 * 1024**3,
    "reserve_host_D_bytes": 8 * 1024**3,
    "max_wakeup_seconds": 2700,
    "next_partition_time_reserve_seconds": 420,
    "threads": 2,
}


def scenarios():
    return [
        {"id": f"{p}_h{h}_bps{b}", "policy": p, "horizon": h, "friction_bps": b}
        for p, h, b in product(POLICIES, HORIZONS, FRICTIONS)
    ]


def schedule(sessions, horizon, *, start=START, signal_end=SIGNAL_END, end=END):
    if horizon not in HORIZONS or isinstance(horizon, bool):
        raise DataValidationError("undeclared economic horizon")
    days = [str(x) for x in sessions if start <= str(x) <= end]
    if days != sorted(set(days)) or not days or days[0] != start or days[-1] != end:
        raise DataValidationError("economic market calendar changed or incomplete")
    signal_days = [d for d in days if d <= signal_end]
    if not signal_days or signal_days[-1] != signal_end:
        raise DataValidationError("economic signal boundary missing from calendar")
    return [date.fromisoformat(d) for d in signal_days[::horizon]]


def target_for_cross(cross, day, *, control=False):
    """None is absent signal; a present all-unscored cross-section is explicit cash."""
    if cross is None or cross.empty:
        return None
    cross = cross[[*KEYS, "score"]].copy()
    if cross[KEYS].isna().any().any() or cross.duplicated(KEYS).any():
        raise DataValidationError("economic cross-section has invalid identities")
    cross["trade_date"] = pd.to_datetime(cross.trade_date).dt.date
    if not cross.trade_date.eq(day).all():
        raise DataValidationError("economic cross-section date differs")
    if not cross.instrument_id.map(lambda x: isinstance(x, str) and bool(x)).all():
        raise DataValidationError("economic instrument identifiers must be nonempty strings")
    if control:
        ids = sorted(cross.instrument_id)
        return TargetPortfolio(day, tuple(TargetWeight(i, 0.8 / len(ids)) for i in ids), 0.2)
    cross["score"] = cross.score.where(np.isfinite(cross.score))
    return construct_fixed_count_portfolio(
        cross.rename(columns={"score": "alpha_score"}),
        day,
        FixedCountPortfolioConfig(20, "higher_is_better", 0.8, 0.04),
    )


def make_targets(policies, sessions):
    if set(policies) != set(STRATEGIES):
        raise DataValidationError("all four fixed policies are required")
    reference = None
    compare = [*KEYS, "complete_features", "label_end_date", "future_return_5d", "label_reason"]
    groups = {}
    for name in STRATEGIES:
        f = policies[name].sort_values(KEYS).reset_index(drop=True)
        if f.duplicated(KEYS).any() or not np.isfinite(f.score).all():
            raise DataValidationError("saved policy scores or row identities changed")
        if reference is None:
            reference = f[compare]
        elif not reference.equals(f[compare]):
            raise DataValidationError("four policies differ in members or labels")
        groups[name] = {day.date(): g for day, g in f.groupby("trade_date", sort=True)}
    output = {}
    for h in HORIZONS:
        dates = schedule(sessions, h)
        for name in POLICIES:
            rows = groups[STRATEGIES[0] if name == CONTROL else name]
            targets = {}
            for day in dates:
                if day not in rows:
                    raise DataValidationError("saved policy lacks a planned market signal date")
                targets[day] = target_for_cross(rows[day], day, control=name == CONTROL)
            output[f"{name}_h{h}"] = targets
    return output


def target_manifest(targets):
    return {
        name: {
            "fingerprint": fingerprint_targets(t),
            "signal_days": len(t),
            "target_rows": sum(len(x.positions) for x in t.values()),
            "dates": [d.isoformat() for d in t],
        }
        for name, t in sorted(targets.items())
    }
