#!/usr/bin/env python3
"""Run the v0 research backtest (idealized portfolio simulation).

Fixed engineering configuration: momentum_20d (lower_is_better), weekly
rebalance, V1 universe, 20% selection, 10 bps transaction cost.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.backtest import BacktestConfig, compute_metrics, run_backtest, weekly_signal_dates
from quantlab.data import ParquetStorage
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


def main() -> None:
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar = storage.load_trading_calendar()
    open_dates = sorted({c.trade_date for c in calendar if c.is_open})

    signal_dates = [
        d for d in weekly_signal_dates(open_dates) if PERIOD_START <= d <= PERIOD_END
    ]
    last_exec_date = open_dates[open_dates.index(signal_dates[-1]) + 1]
    data_end = last_exec_date + timedelta(days=7)

    t0 = time.perf_counter()

    df = build_research_dataset(
        storage, PERIOD_START, data_end, return_horizons=(20,), forward_horizons=()
    )
    universe = filter_v1_universe(df)
    alpha_df = calculate_momentum_alpha(universe, lookback=20)
    price_frame = universe[["instrument_id", "trade_date", "adj_close"]]

    portfolio_config = RankPortfolioConfig(
        selection_fraction=0.20,
        score_direction="lower_is_better",
        gross_exposure=1.0,
        max_weight_per_name=None,
    )
    targets = {}
    for signal_date in signal_dates:
        cross = alpha_df[alpha_df["trade_date"] == signal_date]
        if cross.empty:
            continue
        targets[signal_date] = construct_rank_portfolio(cross, signal_date, portfolio_config)

    bt_config = BacktestConfig(initial_nav=1.0, transaction_cost_bps=10.0, annualization=252)
    bt_open_dates = [d for d in open_dates if signal_dates[0] <= d <= last_exec_date]
    records, rebalance_log = run_backtest(
        price_frame, bt_open_dates, targets, bt_config, execution_lag_sessions=1
    )
    metrics = compute_metrics(records, rebalance_log, bt_config)
    runtime = time.perf_counter() - t0

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "experiments" / "research_backtest_v0" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "analysis_type": "portfolio_engineering_backtest",
        "code_version": _git_sha(),
        "run_time": datetime.now().isoformat(),
        "run_id": run_id,
        "data_start": PERIOD_START.isoformat(),
        "data_end": PERIOD_END.isoformat(),
        "signal_definition": "return_20d",
        "score_direction": "lower_is_better",
        "selection_fraction": 0.20,
        "rebalance_schedule": "weekly_last_open_session",
        "execution_lag": "next_market_session_close",
        "execution_price_assumption": "next_session_close",
        "transaction_cost_bps": 10.0,
        "performance_claim": False,
        "test_observed": True,
        "metrics": metrics,
        "total_runtime_seconds": runtime,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    pd.DataFrame([r.__dict__ for r in records]).to_csv(out_dir / "daily_nav.csv", index=False)
    pd.DataFrame([r.__dict__ for r in rebalance_log]).to_csv(
        out_dir / "rebalance_log.csv", index=False
    )

    print("=== research backtest v0 ===")
    print(f"signal dates (weekly): {len(signal_dates)}")
    print(f"total_return gross={metrics['total_return_gross']:.4f} "
          f"net={metrics['total_return_net']:.4f}")
    print(f"cagr gross={metrics['cagr_gross']:.4f} net={metrics['cagr_net']:.4f}")
    print(f"annualized_vol net={metrics['annualized_volatility_net']:.4f}")
    print(f"sharpe gross={metrics['sharpe_gross']:.4f} net={metrics['sharpe_net']:.4f}")
    print(f"max_drawdown gross={metrics['max_drawdown_gross']:.4f} "
          f"net={metrics['max_drawdown_net']:.4f}")
    print(f"average_turnover={metrics['average_turnover']:.4f} "
          f"annualized={metrics['annualized_turnover']:.4f}")
    print(f"total_transaction_cost={metrics['total_transaction_cost']:.4f} "
          f"cost_drag={metrics['cost_drag']:.4f}")
    print(f"average_holdings={metrics['average_holdings']:.2f}")
    print(f"unavailable_execution_count={metrics['unavailable_execution_count']}")
    print(f"output dir: {out_dir}")
    print(f"runtime: {runtime:.1f}s")


if __name__ == "__main__":
    main()
