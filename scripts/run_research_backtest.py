#!/usr/bin/env python3
"""Run the v0.2 research backtest (dual-ledger, self-financing cost).

Fixed engineering configuration: momentum_20d (lower_is_better), weekly
rebalance, V1 universe, 20% selection, 10 bps transaction cost. Gross and net
ledgers are simulated independently with self-financing cost and
freeze_held_no_price for held positions missing a price.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.backtest import (
    MISSING_PRICE_POLICY,
    BacktestConfig,
    compute_metrics,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.data import ParquetStorage
from quantlab.data.security_history import load_security_code_changes
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCHEMA_VERSION = "v0.2"

PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)
LOOKBACK = 20


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except Exception:
        return None


def _git_dirty() -> bool:
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
        )
        return bool(out.strip())
    except Exception:
        return True


def _code_fingerprint(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(path.name.encode())
        h.update(_sha256_file(path).encode())
    return h.hexdigest()


def _build_manifest(storage: ParquetStorage, calendar_path: Path, padded_dates: list[date]) -> dict:
    files = [
        storage.securities_path,
        storage.calendar_path,
        calendar_path,
    ]
    for d in padded_dates:
        files.append(storage.daily_bars_path(d))
        files.append(storage.adj_factor_path(d))

    entries = []
    h = hashlib.sha256()
    for path in sorted(set(files)):
        rel = str(path.relative_to(PROJECT_ROOT))
        size = path.stat().st_size
        digest = _sha256_file(path)
        entries.append({"path": rel, "size_bytes": size, "sha256": digest})
        h.update(rel.encode())
        h.update(digest.encode())
    return {"combined_sha256": h.hexdigest(), "files": entries}


def _main() -> None:
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar = storage.load_trading_calendar()
    if not calendar:
        raise RuntimeError("trading calendar is empty")

    open_dates = sorted({c.trade_date for c in calendar if c.is_open})
    signal_dates = [
        d for d in weekly_signal_dates(open_dates) if PERIOD_START <= d <= PERIOD_END
    ]

    in_range = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    first_idx = open_dates.index(in_range[0])
    last_idx = open_dates.index(in_range[-1])
    padded_dates = open_dates[max(0, first_idx - LOOKBACK) : last_idx + 1]

    code_changes_path = PROJECT_ROOT / "config" / "security_code_changes.csv"
    code_changes = load_security_code_changes(code_changes_path)
    securities = storage.load_securities()

    manifest = _build_manifest(storage, code_changes_path, padded_dates)

    t0 = time.perf_counter()

    df = build_research_dataset(
        storage, PERIOD_START, PERIOD_END, return_horizons=(LOOKBACK,), forward_horizons=()
    )
    price_frame = df[["instrument_id", "trade_date", "adj_close"]]
    universe = filter_v1_universe(df)
    alpha_df = calculate_momentum_alpha(universe, lookback=LOOKBACK)

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
    bt_open_dates = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    result = run_backtest(
        price_frame, bt_open_dates, targets, bt_config, execution_lag_sessions=1
    )
    metrics = compute_metrics(result.records, result.rebalances, bt_config)
    runtime = time.perf_counter() - t0

    # lifecycle diagnostics
    delist_map = {
        s.instrument_id: s.delist_date for s in securities if s.delist_date is not None
    }
    code_change_map = {cc.old_instrument_id: cc.effective_date for cc in code_changes}

    lifecycle_events = []
    seen: set[str] = set()
    for snap in result.books:
        if snap.book != "net":
            continue
        for pos in snap.positions:
            if not pos.missing_price or pos.instrument_id in seen:
                continue
            event_type = None
            event_date = None
            if pos.instrument_id in delist_map and delist_map[pos.instrument_id] <= snap.trade_date:
                event_type = "delist"
                event_date = delist_map[pos.instrument_id]
            elif (
                pos.instrument_id in code_change_map
                and code_change_map[pos.instrument_id] <= snap.trade_date
            ):
                event_type = "code_change"
                event_date = code_change_map[pos.instrument_id]
            if event_type is not None:
                seen.add(pos.instrument_id)
                lifecycle_events.append({
                    "instrument_id": pos.instrument_id,
                    "event_type": event_type,
                    "event_date": event_date.isoformat(),
                    "first_observed_held": snap.trade_date.isoformat(),
                })

    last_net = next(b for b in reversed(result.books) if b.book == "net")
    stale_end = [
        {
            "instrument_id": p.instrument_id,
            "value": p.value,
            "weight": p.weight,
            "last_mark_date": p.last_mark_date.isoformat() if p.last_mark_date else None,
        }
        for p in last_net.positions
        if p.missing_price
    ]

    run_status = "blocked_by_unsupported_event" if lifecycle_events else "complete"

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "experiments" / "research_backtest_v0_2" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    sim_start = bt_open_dates[0].isoformat() if bt_open_dates else PERIOD_START.isoformat()
    sim_end = bt_open_dates[-1].isoformat() if bt_open_dates else PERIOD_END.isoformat()

    code_paths = sorted((PROJECT_ROOT / "src" / "quantlab" / "backtest").glob("*.py"))
    code_paths.append(PROJECT_ROOT / "scripts" / "run_research_backtest.py")

    summary = {
        "engine_schema_version": ENGINE_SCHEMA_VERSION,
        "analysis_type": "portfolio_engineering_backtest",
        "code_version": _git_sha(),
        "workspace_dirty": _git_dirty(),
        "code_content_sha256": _code_fingerprint(code_paths),
        "run_time": datetime.now().isoformat(),
        "run_id": run_id,
        "period_start": PERIOD_START.isoformat(),
        "period_end": PERIOD_END.isoformat(),
        "simulation_start": sim_start,
        "simulation_end": sim_end,
        "config": {
            "signal_definition": f"return_{LOOKBACK}d",
            "score_direction": "lower_is_better",
            "selection_fraction": 0.20,
            "rebalance_schedule": "weekly_last_open_session",
            "execution_lag": "next_market_session_close",
            "execution_price_assumption": "next_session_close",
            "transaction_cost_bps": 10.0,
            "initial_nav": bt_config.initial_nav,
            "annualization": bt_config.annualization,
        },
        "accounting": {
            "dual_ledger": "gross (cost_rate=0) and net (config.cost_rate)",
            "cost_model": "self_financing_proportional",
            "missing_price_policy": MISSING_PRICE_POLICY,
            "return_basis": "adjusted_close",
            "cash_return": 0.0,
            "positions_are_value_amounts_not_shares": True,
            "sharpe_rf": 0.0,
            "variance_ddof": 0,
        },
        "performance_claim": False,
        "test_observed": True,
        "data_manifest": manifest,
        "run_status": run_status,
        "n_records": len(result.records),
        "n_return_intervals": len(result.records) - 1,
        "max_conservation_residual": result.max_conservation_residual,
        "frozen_position_count": sum(rb.frozen_count for rb in result.rebalances),
        "unavailable_target_count": sum(rb.unavailable_target_count for rb in result.rebalances),
        "skipped_execution_count": len(result.skipped_executions),
        "lifecycle_events": lifecycle_events,
        "end_of_period_stale_positions": stale_end,
        "metrics": metrics,
        "total_runtime_seconds": runtime,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    pd.DataFrame([r.__dict__ for r in result.records]).to_csv(
        out_dir / "daily_records.csv", index=False
    )
    pd.DataFrame([r.__dict__ for r in result.rebalances]).to_csv(
        out_dir / "rebalance_log.csv", index=False
    )
    pd.DataFrame([t.__dict__ for t in result.trades]).to_csv(
        out_dir / "trade_details.csv", index=False
    )
    pd.DataFrame([s.__dict__ for s in result.skipped_executions]).to_csv(
        out_dir / "skipped_executions.csv", index=False
    )

    book_rows = []
    position_rows = []
    for b in result.books:
        book_rows.append({
            "trade_date": b.trade_date,
            "book": b.book,
            "nav": b.nav,
            "daily_return": b.daily_return,
            "cash": b.cash,
            "market_pnl": b.market_pnl,
            "fee": b.fee,
            "gross_exposure": b.gross_exposure,
            "net_exposure": b.net_exposure,
            "cash_weight": b.cash_weight,
            "holdings_count": b.holdings_count,
        })
        for p in b.positions:
            position_rows.append({
                "trade_date": b.trade_date,
                "book": b.book,
                "instrument_id": p.instrument_id,
                "value": p.value,
                "weight": p.weight,
                "last_price": p.last_price,
                "last_mark_date": p.last_mark_date,
                "missing_price": p.missing_price,
            })
    pd.DataFrame(book_rows).to_csv(out_dir / "daily_books.csv", index=False)
    pd.DataFrame(position_rows).to_csv(out_dir / "daily_positions.csv", index=False)

    if lifecycle_events:
        pd.DataFrame(lifecycle_events).to_csv(out_dir / "lifecycle_events.csv", index=False)

    print(f"=== research backtest {ENGINE_SCHEMA_VERSION} ===")
    print(f"run_status: {run_status}")
    print(f"simulation: {sim_start} .. {sim_end} "
          f"({len(result.records)} records, {len(result.records) - 1} intervals)")
    print(f"total_return gross={metrics['total_return_gross']:.4f} "
          f"net={metrics['total_return_net']:.4f}")
    print(f"cagr gross={metrics['cagr_gross']:.4f} net={metrics['cagr_net']:.4f}")
    print(f"annualized_vol gross={metrics['annualized_volatility_gross']:.4f} "
          f"net={metrics['annualized_volatility_net']:.4f}")
    print(f"sharpe gross={metrics['sharpe_gross']:.4f} net={metrics['sharpe_net']:.4f}")
    print(f"max_drawdown gross={metrics['max_drawdown_gross']:.4f} "
          f"net={metrics['max_drawdown_net']:.4f}")
    print(f"total_turnover net={metrics['total_turnover']:.4f} "
          f"gross={metrics['gross_book_total_turnover']:.4f}")
    print(f"avg_rebalance_turnover net={metrics['average_rebalance_turnover']:.4f} "
          f"gross={metrics['gross_book_average_rebalance_turnover']:.4f}")
    print(f"annualized_turnover net={metrics['annualized_turnover']:.4f} "
          f"gross={metrics['gross_book_annualized_turnover']:.4f}")
    print(f"total_transaction_cost={metrics['total_transaction_cost']:.4f}")
    print(f"cumulative_cost_paid_vs_initial_nav="
          f"{metrics['cumulative_cost_paid_vs_initial_nav']:.4f}")
    print(f"terminal_return_cost_drag={metrics['terminal_return_cost_drag']:.4f} "
          f"cagr_cost_drag={metrics['cagr_cost_drag']:.4f}")
    print(f"annualized_traded_notional={metrics['annualized_traded_notional']:.4f} "
          f"implied_annual_cost_rate={metrics['implied_annual_cost_rate']:.4f}")
    print(f"average_holdings net={metrics['average_holdings']:.2f} "
          f"gross={metrics['gross_book_average_holdings']:.2f}")
    print(f"average_gross_exposure net={metrics['average_gross_exposure']:.4f} "
          f"gross={metrics['gross_book_average_gross_exposure']:.4f}")
    print(f"rebalance_count={metrics['rebalance_count']} "
          f"unavailable_target_count={metrics['unavailable_execution_count']}")
    print(f"frozen_position_count={summary['frozen_position_count']} "
          f"skipped_execution_count={summary['skipped_execution_count']}")
    print(f"lifecycle_events={len(lifecycle_events)} "
          f"stale_end_positions={len(stale_end)}")
    print(f"max_conservation_residual={result.max_conservation_residual:.3e}")
    print(f"output dir: {out_dir}")
    print(f"runtime: {runtime:.1f}s")


if __name__ == "__main__":
    _main()
