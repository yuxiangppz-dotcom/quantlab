#!/usr/bin/env python3
"""Run a pre-registered alpha research experiment.

Usage:
    uv run python scripts/run_alpha_research.py --alpha momentum_20d
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.data import ParquetStorage
from quantlab.research import (
    build_research_dataset,
    daily_rank_ic,
    filter_v1_universe,
    quantile_returns,
    summarize_ic,
    summarize_quantiles,
)

MOMENTUM_20D = {
    "experiment_name": "momentum_20d",
    "universe": "V1 SH/SZ A-share",
    "alpha_definition": "return_20d",
    "lookback": 20,
    "label_horizons": [5, 20],
    "discovery": ["2010-01-04", "2019-12-31"],
    "validation": ["2020-01-01", "2024-12-31"],
    "test": ["2025-01-01", "2026-09-04"],
    "test_observed": True,
}

_EXPERIMENTS = {"momentum_20d": MOMENTUM_20D}


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return None


def _run_year(storage: ParquetStorage, year: int, lookback: int) -> dict:
    end = date(2026, 9, 4) if year == 2026 else date(year, 12, 31)
    df = build_research_dataset(storage, date(year, 1, 1), end)
    universe = filter_v1_universe(df)
    alpha = calculate_momentum_alpha(universe, lookback=lookback)
    merged = alpha.merge(
        universe[["instrument_id", "trade_date", "future_return_5d", "future_return_20d"]],
        on=["instrument_id", "trade_date"],
    )
    return {
        "ic5": daily_rank_ic(merged, "future_return_5d"),
        "ic20": daily_rank_ic(merged, "future_return_20d"),
        "q5": quantile_returns(merged, "future_return_5d"),
        "q20": quantile_returns(merged, "future_return_20d"),
    }


def _period_metrics(data: dict, years: range) -> dict:
    ic5 = pd.concat([data[y]["ic5"] for y in years])
    ic20 = pd.concat([data[y]["ic20"] for y in years])
    q5 = pd.concat([data[y]["q5"] for y in years])
    q20 = pd.concat([data[y]["q20"] for y in years])
    return {
        "future_5d": {
            "rank_ic": summarize_ic(ic5),
            "quantiles": summarize_quantiles(q5),
        },
        "future_20d": {
            "rank_ic": summarize_ic(ic20),
            "quantiles": summarize_quantiles(q20),
        },
    }


def _print_period(label: str, metrics: dict) -> None:
    for horizon, m in metrics.items():
        ic = m["rank_ic"]
        q = m["quantiles"]
        print(f"=== {label} {horizon} ===")
        print(
            f"  RankIC mean={ic['mean_rank_ic']:.4f} median={ic['median_rank_ic']:.4f} "
            f"std={ic['std_rank_ic']:.4f} pos_ratio={ic['positive_ratio']:.3f} "
            f"valid_days={ic['valid_days']}"
        )
        print(
            f"  Q1={q['Q1_mean']:.5f} Q2={q['Q2_mean']:.5f} Q3={q['Q3_mean']:.5f} "
            f"Q4={q['Q4_mean']:.5f} Q5={q['Q5_mean']:.5f} spread={q['spread_mean']:.5f}"
        )


def run_momentum_20d(config: dict) -> Path:
    storage = ParquetStorage()
    t0 = time.perf_counter()
    years = list(range(2010, 2027))
    data = {}
    yearly_ic_rows = []
    for year in years:
        data[year] = _run_year(storage, year, config["lookback"])
        s5 = summarize_ic(data[year]["ic5"])
        s20 = summarize_ic(data[year]["ic20"])
        yearly_ic_rows.append({
            "year": year,
            "ic_5d": s5["mean_rank_ic"],
            "ic_20d": s20["mean_rank_ic"],
            "ic_5d_pos_ratio": s5["positive_ratio"],
            "ic_20d_pos_ratio": s20["positive_ratio"],
        })

    discovery = _period_metrics(data, range(2010, 2020))
    validation = _period_metrics(data, range(2020, 2025))
    test = _period_metrics(data, range(2025, 2027))
    runtime = time.perf_counter() - t0

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = Path("data/experiments") / config["experiment_name"] / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "experiment_name": config["experiment_name"],
        "run_time": datetime.now().isoformat(),
        "run_id": run_id,
        "code_version": _git_sha(),
        "data_start": "2010-01-04",
        "data_end": "2026-09-04",
        "universe": config["universe"],
        "alpha_definition": config["alpha_definition"],
        "lookback": config["lookback"],
        "label_horizons": config["label_horizons"],
        "discovery": config["discovery"],
        "validation": config["validation"],
        "test": config["test"],
        "test_observed": config["test_observed"],
        "discovery_metrics": discovery,
        "validation_metrics": validation,
        "test_metrics": test,
        "total_runtime_seconds": runtime,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    pd.DataFrame(yearly_ic_rows).to_csv(out_dir / "yearly_rank_ic.csv", index=False)

    quantile_rows = []
    for label, metrics in [
        ("discovery", discovery),
        ("validation", validation),
        ("test", test),
    ]:
        for horizon, m in metrics.items():
            row = {"period": label, "horizon": horizon}
            row.update(m["quantiles"])
            quantile_rows.append(row)
    pd.DataFrame(quantile_rows).to_csv(out_dir / "quantile_summary.csv", index=False)

    print("=== period summary ===")
    _print_period("discovery", discovery)
    _print_period("validation", validation)
    _print_period("test", test)
    print("\n=== yearly mean RankIC ===")
    for row in yearly_ic_rows:
        print(f"  {row['year']}: IC_5d={row['ic_5d']:.4f}, IC_20d={row['ic_20d']:.4f}")
    print(f"\noutput dir: {out_dir}")
    print(f"total runtime: {runtime:.1f}s")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a pre-registered alpha experiment.")
    parser.add_argument("--alpha", required=True, choices=sorted(_EXPERIMENTS))
    args = parser.parse_args()
    run_momentum_20d(_EXPERIMENTS[args.alpha])


if __name__ == "__main__":
    main()
