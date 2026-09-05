#!/usr/bin/env python3
"""Post-baseline robustness: size / turnover buckets for momentum_20d."""

from __future__ import annotations

import bisect
import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.data import ParquetStorage
from quantlab.research import (
    build_research_dataset,
    daily_rank_ic,
    filter_v1_universe,
    quantile_returns,
    summarize_ic,
)
from quantlab.research.characteristics import load_market_characteristics

PROJECT_ROOT = Path(__file__).resolve().parents[1]

CONFIG = {
    "analysis_type": "post_baseline_robustness",
    "parent_experiment": "momentum_20d",
    "parent_baseline_commit": "c2981b1",
    "alpha_definition": "return_20d",
    "lookback": 20,
    "characteristics": ["circ_mv", "turnover_rate"],
    "data_start": "2010-01-04",
    "data_end": "2026-09-04",
    "label_horizons": [5, 20],
    "discovery": ["2010-01-04", "2019-12-31"],
    "validation": ["2020-01-01", "2024-12-31"],
    "test": ["2025-01-01", "2026-09-04"],
    "test_observed": True,
    "label_period_policy": "target_session_must_be_within_period",
    "quantile_tie_policy": "average_rank_keep_ties",
}


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


def _eligible_signal_end(open_dates: list[date], period_end: date, horizon: int) -> date | None:
    dates = sorted(set(open_dates))
    last_idx = bisect.bisect_right(dates, period_end) - 1
    if last_idx < horizon:
        return None
    return dates[last_idx - horizon]


def _add_bucket(df: pd.DataFrame, value_col: str, n: int = 5) -> pd.Series:
    def _b(series: pd.Series) -> pd.Series:
        pct = series.rank(method="average", pct=True)
        return np.ceil(pct * n) - 1

    return df.groupby("trade_date")[value_col].transform(_b)


def _run_year(storage: ParquetStorage, year: int, data_end: date) -> pd.DataFrame:
    end = data_end if year == data_end.year else date(year, 12, 31)
    df = build_research_dataset(storage, date(year, 1, 1), end)
    universe = filter_v1_universe(df)
    alpha = calculate_momentum_alpha(universe, lookback=CONFIG["lookback"])
    chars = load_market_characteristics(storage, date(year, 1, 1), end)
    return (
        alpha.merge(
            universe[["instrument_id", "trade_date", "future_return_5d", "future_return_20d"]],
            on=["instrument_id", "trade_date"],
        )
        .merge(chars, on=["instrument_id", "trade_date"], how="left")
    )


def _bucket_summary(
    merged: pd.DataFrame,
    bucket_col: str,
    bucket_labels: list[str],
    open_dates: list[date],
    period: list[str],
) -> list[dict]:
    start = date.fromisoformat(period[0])
    end = date.fromisoformat(period[1])
    period_df = merged[(merged["trade_date"] >= start) & (merged["trade_date"] <= end)]

    rows = []
    for horizon in CONFIG["label_horizons"]:
        eligible_end = _eligible_signal_end(open_dates, end, horizon)
        eligible = period_df[period_df["trade_date"] <= eligible_end]
        for b in range(5):
            sub = eligible[eligible[bucket_col] == b]
            ic = daily_rank_ic(sub, f"future_return_{horizon}d")
            q = quantile_returns(sub, f"future_return_{horizon}d")
            s = summarize_ic(ic)
            spread = q["Q5_minus_Q1"].mean() if not q.empty else float("nan")
            rows.append({
                "period": ".".join(period),
                "horizon": f"{horizon}d",
                "bucket": bucket_labels[b],
                "mean_rank_ic": s["mean_rank_ic"],
                "median_rank_ic": s["median_rank_ic"],
                "positive_ratio": s["positive_ratio"],
                "valid_days": s["valid_days"],
                "q5_minus_q1": spread,
            })
    return rows


def main() -> None:
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar = storage.load_trading_calendar()
    open_dates = sorted({c.trade_date for c in calendar if c.is_open})

    data_start = date.fromisoformat(CONFIG["data_start"])
    data_end = date.fromisoformat(CONFIG["data_end"])
    years = list(range(data_start.year, data_end.year + 1))

    t0 = time.perf_counter()
    parts = []
    for year in years:
        parts.append(_run_year(storage, year, data_end))
    merged = pd.concat(parts, ignore_index=True)

    merged["size_bucket"] = _add_bucket(merged, "circ_mv")
    merged["turnover_bucket"] = _add_bucket(merged, "turnover_rate")

    size_labels = ["S1_smallest", "S2", "S3", "S4", "S5_largest"]
    turnover_labels = ["T1_lowest", "T2", "T3", "T4", "T5_highest"]

    size_rows = []
    turnover_rows = []
    coverage_rows = []
    for label, period in [
        ("discovery", CONFIG["discovery"]),
        ("validation", CONFIG["validation"]),
        ("test", CONFIG["test"]),
    ]:
        start = date.fromisoformat(period[0])
        end = date.fromisoformat(period[1])
        p = merged[(merged["trade_date"] >= start) & (merged["trade_date"] <= end)]
        coverage_rows.append({
            "period": label,
            "research_rows": len(p),
            "matched_circ_mv": int(p["circ_mv"].notna().sum()),
            "matched_turnover": int(p["turnover_rate"].notna().sum()),
            "coverage_ratio_circ_mv": float(p["circ_mv"].notna().mean()),
            "coverage_ratio_turnover": float(p["turnover_rate"].notna().mean()),
        })
        size_rows.extend(_bucket_summary(merged, "size_bucket", size_labels, open_dates, period))
        turnover_rows.extend(
            _bucket_summary(merged, "turnover_bucket", turnover_labels, open_dates, period)
        )

    runtime = time.perf_counter() - t0

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "experiments" / "momentum_20d_robustness" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "analysis_type": CONFIG["analysis_type"],
        "parent_experiment": CONFIG["parent_experiment"],
        "parent_baseline_commit": CONFIG["parent_baseline_commit"],
        "code_version": _git_sha(),
        "run_time": datetime.now().isoformat(),
        "run_id": run_id,
        "characteristics": CONFIG["characteristics"],
        "data_start": CONFIG["data_start"],
        "data_end": CONFIG["data_end"],
        "alpha_definition": CONFIG["alpha_definition"],
        "lookback": CONFIG["lookback"],
        "label_horizons": CONFIG["label_horizons"],
        "discovery": CONFIG["discovery"],
        "validation": CONFIG["validation"],
        "test": CONFIG["test"],
        "test_observed": CONFIG["test_observed"],
        "label_period_policy": CONFIG["label_period_policy"],
        "quantile_tie_policy": CONFIG["quantile_tie_policy"],
        "total_runtime_seconds": runtime,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    pd.DataFrame(size_rows).to_csv(out_dir / "size_bucket_metrics.csv", index=False)
    pd.DataFrame(turnover_rows).to_csv(out_dir / "turnover_bucket_metrics.csv", index=False)
    pd.DataFrame(coverage_rows).to_csv(out_dir / "coverage.csv", index=False)

    print("=== SIZE buckets (mean RankIC, Q5-Q1) ===")
    for r in size_rows:
        print(
            f"  {r['period']} {r['horizon']} {r['bucket']}: "
            f"IC={r['mean_rank_ic']:.4f} (pos={r['positive_ratio']:.3f}), "
            f"Q5-Q1={r['q5_minus_q1']:.5f}"
        )
    print("=== TURNOVER buckets ===")
    for r in turnover_rows:
        print(
            f"  {r['period']} {r['horizon']} {r['bucket']}: "
            f"IC={r['mean_rank_ic']:.4f} (pos={r['positive_ratio']:.3f}), "
            f"Q5-Q1={r['q5_minus_q1']:.5f}"
        )
    print("=== coverage ===")
    for r in coverage_rows:
        print(
            f"  {r['period']}: rows={r['research_rows']}, "
            f"circ_mv={r['coverage_ratio_circ_mv']:.3f}, "
            f"turnover={r['coverage_ratio_turnover']:.3f}"
        )
    print(f"\noutput dir: {out_dir}")
    print(f"total runtime: {runtime:.1f}s")


if __name__ == "__main__":
    main()
