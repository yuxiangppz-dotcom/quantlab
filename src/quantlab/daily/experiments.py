"""Small user-facing views over existing formal research artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from quantlab.daily.service import PROJECT_ROOT

BASELINE_SCHEMA = "performance_baseline_benchmark_correctness_v0_1_3"


def latest_completed_baseline(
    experiments_root: Path = PROJECT_ROOT / "data" / "experiments",
) -> Path | None:
    root = experiments_root / BASELINE_SCHEMA
    if not root.exists():
        return None
    candidates = sorted(
        path
        for path in root.iterdir()
        if path.is_dir()
        and not path.name.endswith(".incomplete")
        and (path / "COMPLETED.json").exists()
    )
    return candidates[-1] if candidates else None


def _path_metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    returns = frame["daily_return_net"].iloc[1:]
    nav = frame["nav_net"]
    intervals = max(0, len(frame) - 1)
    years = intervals / 252 if intervals else math.nan
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1) if len(nav) else math.nan
    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else math.nan
    standard_deviation = returns.std(ddof=0)
    volatility = (
        float(standard_deviation * math.sqrt(252)) if len(returns) > 1 else math.nan
    )
    sharpe = (
        float(returns.mean() / standard_deviation * math.sqrt(252))
        if len(returns) > 1 and standard_deviation > 0
        else math.nan
    )
    drawdown = nav / nav.cummax() - 1
    return {
        "n_records": len(frame),
        "total_return_net": total_return,
        "cagr_net": cagr,
        "annualized_volatility_net": volatility,
        "sharpe_net": sharpe,
        "max_drawdown_net": float(drawdown.min()),
        "total_turnover": float(frame["turnover"].sum()),
        "total_transaction_cost": float(frame["transaction_cost"].sum()),
        "average_holdings": float(frame["holdings_count"].mean()),
    }


def load_baseline_view(run_dir: Path | None = None) -> dict | None:
    """Load only the compact daily paths needed by the local UI.

    The formal summary is intentionally not loaded: the historical artifact
    embeds a 97 MB audit projection.  Metrics shown here are transparently
    re-derived from its manifest-bound daily record files.
    """
    run_dir = run_dir or latest_completed_baseline()
    if run_dir is None:
        return None
    marker = json.loads((run_dir / "COMPLETED.json").read_text(encoding="utf-8"))
    files = {
        "strategy_r1": "primary_strategy_recovery_assumption_1_daily_records.csv",
        "strategy_r0": "primary_strategy_recovery_assumption_0_daily_records.csv",
        "control_r1": "equal_weight_v1_control_recovery_assumption_1_daily_records.csv",
    }
    frames = {name: pd.read_csv(run_dir / filename) for name, filename in files.items()}
    for frame in frames.values():
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    strategy = frames["strategy_r1"]
    control = frames["control_r1"]
    joined = strategy[["trade_date", "nav_net"]].merge(
        control[["trade_date", "nav_net"]],
        on="trade_date",
        suffixes=("_strategy", "_control"),
        validate="one_to_one",
    )
    relative = joined["nav_net_strategy"] / joined["nav_net_control"]
    active_drawdown = relative / relative.cummax() - 1
    curve = joined.rename(
        columns={"nav_net_strategy": "strategy", "nav_net_control": "control"}
    )
    return {
        "run_dir": str(run_dir),
        "schema": marker["schema"],
        "run_id": marker["run_id"],
        "head": marker["head"],
        "formal_run_valid": marker["formal_run_valid"],
        "strategy_recovery_1": _path_metrics(frames["strategy_r1"]),
        "strategy_recovery_0": _path_metrics(frames["strategy_r0"]),
        "control_recovery_1": _path_metrics(frames["control_r1"]),
        "active": {
            "cumulative_active_return": float(relative.iloc[-1] - 1),
            "active_max_drawdown": float(active_drawdown.min()),
        },
        "curve": curve,
        "disclosure": {
            "performance_claim": False,
            "test_observed": True,
            "index_basis": "price_index_close",
            "settlement": "held-delist recovery scenario; recovery 0 and 1 are assumptions",
            "fact_coverage": "limited; unknown is not safe",
        },
    }
