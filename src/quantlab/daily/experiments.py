"""Small user-facing views over existing formal research artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from quantlab.daily.service import PROJECT_ROOT

BASELINE_SCHEMA = "performance_baseline_benchmark_correctness_v0_1_3"
FACTOR_SCHEMA = "daily_factor_research_v1"
PORTFOLIO_AUDIT_SCHEMA = "portfolio_translation_audit_v1_1"
CADENCE_AUDIT_SCHEMA = "daily_weekly_cadence_audit_v1_1"


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
    volatility = float(standard_deviation * math.sqrt(252)) if len(returns) > 1 else math.nan
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
    curve = joined.rename(columns={"nav_net_strategy": "strategy", "nav_net_control": "control"})
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


def load_latest_factor_view(
    experiments_root: Path = PROJECT_ROOT / "data" / "experiments",
) -> dict | None:
    root = experiments_root / FACTOR_SCHEMA
    if not root.exists():
        return None
    candidates = sorted(
        path for path in root.iterdir() if path.is_dir() and (path / "summary.json").exists()
    )
    if not candidates:
        return None
    summary = json.loads((candidates[-1] / "summary.json").read_text(encoding="utf-8"))
    return {
        "run_dir": str(candidates[-1]),
        "run_id": summary["run_id"],
        "signal_end": summary["signal_end"],
        "registry": summary["registry"],
        "transparent_combination": summary["transparent_combination"],
        "lightgbm": summary["lightgbm"],
        "qlib": summary["qlib"],
        "performance_claim": summary["performance_claim"],
    }


def _latest_summary(schema: str, experiments_root: Path) -> tuple[Path, dict] | None:
    root = experiments_root / schema
    candidates = sorted(
        path for path in root.glob("*/summary.json") if not path.parent.name.endswith(".incomplete")
    )
    if not candidates:
        return None
    path = candidates[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def load_latest_portfolio_audit(
    experiments_root: Path = PROJECT_ROOT / "data" / "experiments",
) -> dict | None:
    item = _latest_summary(PORTFOLIO_AUDIT_SCHEMA, experiments_root)
    if item is None:
        return None
    path, summary = item
    rows = []
    for factor, result in summary["candidates"].items():
        metrics = result["metrics"]
        active = result["primary_attribution"]
        rows.append(
            {
                "factor": factor,
                "cagr_net": metrics["cagr_net"],
                "active_cagr": active["active_cagr"],
                "sharpe_net": metrics["sharpe_net"],
                "max_drawdown_net": metrics["max_drawdown_net"],
                "active_max_drawdown": active["active_max_drawdown"],
                "annualized_turnover": metrics["annualized_turnover"],
                "cagr_cost_drag": metrics["cagr_cost_drag"],
                "information_ratio": active["information_ratio"],
                "beta": active["beta"],
                "coverage": active["n_obs"] / (metrics["n_records"] - 1),
            }
        )
    return {
        "run_dir": str(path.parent),
        "run_id": summary["run_id"],
        "period": summary["period"],
        "history_status": summary["history_status"],
        "control_cagr": summary["control"]["metrics"]["cagr_net"],
        "rows": rows,
        "strategy_promoted": summary["strategy_promoted"],
    }


def load_latest_cadence_audit(
    experiments_root: Path = PROJECT_ROOT / "data" / "experiments",
) -> dict | None:
    item = _latest_summary(CADENCE_AUDIT_SCHEMA, experiments_root)
    if item is None:
        return None
    path, summary = item
    return {**summary, "run_dir": str(path.parent)}
