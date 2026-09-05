#!/usr/bin/env python3
"""Run the v0.2.1 research backtest (strict + diagnostic, full provenance).

Fixed engineering configuration: momentum_20d (lower_is_better), weekly
rebalance, V1 universe, 20% selection, 10 bps transaction cost.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.backtest import (
    MISSING_PRICE_POLICY,
    RUN_MODE_DIAGNOSTIC,
    RUN_MODE_STRICT,
    BacktestConfig,
    LifecycleMonitor,
    build_report,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.backtest.admission import POLICY_NAME, POLICY_VERSION, shadow_admission
from quantlab.backtest.delisting_facts import (
    source_coverage,
    trusted_facts_available_as_of,
)
from quantlab.backtest.provenance import content_manifest, environment_info, sha256_file
from quantlab.data import ParquetStorage
from quantlab.data.security_history import load_security_code_changes
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCHEMA_VERSION = "v0.2.4"

PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)
LOOKBACK = 20


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


def _code_paths() -> list[Path]:
    paths = sorted((PROJECT_ROOT / "src" / "quantlab").rglob("*.py"))
    paths.append(PROJECT_ROOT / "scripts" / "run_research_backtest.py")
    paths.append(PROJECT_ROOT / "pyproject.toml")
    paths.append(PROJECT_ROOT / "uv.lock")
    paths.append(PROJECT_ROOT / "config" / "security_code_changes.csv")
    paths.append(PROJECT_ROOT / "config" / "delisting_facts.json")
    return paths


def _data_paths(storage: ParquetStorage, padded_dates: list[date]) -> list[Path]:
    paths = [
        storage.securities_path,
        storage.calendar_path,
        PROJECT_ROOT / "config" / "security_code_changes.csv",
    ]
    for d in padded_dates:
        paths.append(storage.daily_bars_path(d))
        paths.append(storage.adj_factor_path(d))
    return paths


def _load_inputs(storage: ParquetStorage):
    calendar = storage.load_trading_calendar()
    if not calendar:
        raise RuntimeError("trading calendar is empty")
    securities = storage.load_securities()
    code_changes = load_security_code_changes(
        PROJECT_ROOT / "config" / "security_code_changes.csv"
    )
    open_dates = sorted({c.trade_date for c in calendar if c.is_open})
    return calendar, securities, code_changes, open_dates


def _build_targets(storage: ParquetStorage, signal_dates: list[date]):
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
    return price_frame, targets


def _statistics(result) -> dict:
    stats = {}
    for book_name in ("gross", "net"):
        book_trades = [t for t in result.trades if t.book == book_name]
        frozen_trades = [
            t for t in book_trades
            if t.pre_value > 0 and t.reason in ("frozen_held_no_price", "lifecycle_blocked")
        ]
        daily_missing = 0
        last_snapshot = None
        for b in result.books:
            if b.book != book_name:
                continue
            daily_missing += len([p for p in b.positions if p.missing_price])
            last_snapshot = b
        stale_end = []
        if last_snapshot is not None:
            stale_end = [
                {
                    "instrument_id": p.instrument_id,
                    "value": p.value,
                    "weight": p.weight,
                    "last_mark_date": p.last_mark_date.isoformat()
                    if p.last_mark_date else None,
                }
                for p in last_snapshot.positions
                if p.missing_price
            ]
        stats[book_name] = {
            "rebalance_frozen_position_occurrences": len(frozen_trades),
            "unique_frozen_instruments": len(
                {t.instrument_id for t in frozen_trades}
            ),
            "daily_missing_position_occurrences": daily_missing,
            "end_or_blocked_missing_positions": stale_end,
        }
    return stats


def _audit_case(result) -> dict | None:
    first = result.first_blocking_event
    if first is None:
        return None
    instrument_trades = [
        t for t in result.trades
        if t.instrument_id == first.instrument_id and t.book == first.book
    ]
    nonzero_trades = [
        t for t in instrument_trades
        if t.signed_trade_value != 0.0 and t.execution_date <= first.blocking_session
    ]
    last_trade = nonzero_trades[-1] if nonzero_trades else None

    pre_snapshot = None
    for b in result.books:
        if b.book == first.book and b.trade_date < first.blocking_session:
            for p in b.positions:
                if p.instrument_id == first.instrument_id:
                    pre_snapshot = {
                        "trade_date": b.trade_date,
                        "value": p.value,
                        "weight": p.weight,
                        "last_price": p.last_price,
                        "last_mark_date": p.last_mark_date.isoformat()
                        if p.last_mark_date else None,
                        "missing_price": p.missing_price,
                    }
    return {
        "instrument_id": first.instrument_id,
        "event_type": first.event_type,
        "event_date": first.event_date.isoformat(),
        "blocking_session": first.blocking_session.isoformat(),
        "book": first.book,
        "position_value": first.position_value,
        "last_mark_date": first.last_mark_date.isoformat() if first.last_mark_date else None,
        "description": first.description,
        "has_prior_holding": last_trade is not None,
        "last_execution": {
            "signal_date": last_trade.signal_date.isoformat(),
            "execution_date": last_trade.execution_date.isoformat(),
        } if last_trade else None,
        "trades": [
            {
                "signal_date": t.signal_date.isoformat(),
                "execution_date": t.execution_date.isoformat(),
                "signed_trade_value": t.signed_trade_value,
                "target_weight": t.target_weight,
                "actual_weight": t.actual_weight,
                "execution_price": t.execution_price,
                "price_date": t.price_date.isoformat() if t.price_date else None,
                "price_kind": t.price_kind,
                "reason": t.reason,
            }
            for t in instrument_trades
        ],
        "pre_blocking_snapshot": pre_snapshot,
    }


def _delisting_audit(result, facts: dict) -> list[dict]:
    seen: dict[str, object] = {}
    for e in result.lifecycle_events:
        if e.event_id not in seen:
            seen[e.event_id] = e
    first_entry: dict[str, object] = {}
    for t in result.trades:
        if t.signed_trade_value > 0 and t.instrument_id not in first_entry:
            first_entry[t.instrument_id] = t.execution_date
    rows = []
    for e in seen.values():
        entry = first_entry.get(e.instrument_id)
        rows.append({
            "instrument_id": e.instrument_id,
            "event_type": e.event_type,
            "event_date": e.event_date.isoformat(),
            "blocking_session": e.blocking_session.isoformat(),
            "first_entry": entry.isoformat() if entry else None,
            "source_coverage": source_coverage(facts, e.instrument_id),
            "verification_status": source_coverage(facts, e.instrument_id),
        })
    return rows


def _shadow_audit(targets: dict, facts: dict, open_dates: list) -> dict:
    rows = []
    restricted_count = 0
    restricted_weight = 0.0
    restricted_instruments: set[str] = set()
    unknown_count = 0
    for signal_date in sorted(targets):
        target = targets[signal_date]
        exec_date = _next_open_session(open_dates, signal_date)
        for pos in target.positions:
            if pos.target_weight <= 0:
                continue
            trusted = trusted_facts_available_as_of(facts, pos.instrument_id, signal_date)
            decision = shadow_admission(pos.instrument_id, signal_date, trusted)
            rows.append({
                "instrument_id": pos.instrument_id,
                "signal_date": signal_date.isoformat(),
                "execution_date": exec_date.isoformat() if exec_date else None,
                "target_weight": pos.target_weight,
                "admission_status": decision.status,
                "fact_id": decision.fact_id,
                "available_from": decision.available_from.isoformat()
                if decision.available_from else None,
                "source": decision.source,
                "reason": decision.reason,
                "policy_version": decision.policy_version,
            })
            if decision.status == "restricted":
                restricted_count += 1
                restricted_weight += pos.target_weight
                restricted_instruments.add(pos.instrument_id)
            else:
                unknown_count += 1
    return {
        "policy_name": POLICY_NAME,
        "policy_version": POLICY_VERSION,
        "rows": rows,
        "restricted_target_count": restricted_count,
        "restricted_target_weight_sum": restricted_weight,
        "restricted_target_weight_note": (
            "cross-signal cumulative target weight sum, not portfolio exposure"
        ),
        "restricted_unique_instruments": len(restricted_instruments),
        "unknown_count": unknown_count,
        "unknown_note": "unknown = insufficient trusted fact coverage; not a claim of safety",
    }


def _next_open_session(open_dates: list, signal_date) -> object:
    idx = open_dates.index(signal_date) if signal_date in open_dates else -1
    if idx < 0 or idx + 1 >= len(open_dates):
        return None
    return open_dates[idx + 1]


def _main() -> None:
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar, securities, code_changes, open_dates = _load_inputs(storage)

    signal_dates = [
        d for d in weekly_signal_dates(open_dates) if PERIOD_START <= d <= PERIOD_END
    ]
    in_range = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    first_idx = open_dates.index(in_range[0])
    last_idx = open_dates.index(in_range[-1])
    padded_dates = open_dates[max(0, first_idx - LOOKBACK) : last_idx + 1]

    code_paths = _code_paths()
    data_paths = _data_paths(storage, padded_dates)

    git_sha_before = _git_sha()
    git_dirty_before = _git_dirty()

    provenance_before = {
        "code": content_manifest(code_paths, PROJECT_ROOT),
        "data": content_manifest(data_paths, PROJECT_ROOT),
    }
    env = environment_info()
    t0 = time.perf_counter()

    price_frame, targets = _build_targets(storage, signal_dates)
    bt_open_dates = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    bt_config = BacktestConfig(initial_nav=1.0, transaction_cost_bps=10.0, annualization=252)
    monitor = LifecycleMonitor(securities, code_changes)

    strict_result = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )
    diagnostic_result = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_DIAGNOSTIC, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )
    runtime = time.perf_counter() - t0

    # re-discover code files so added/removed files are detected
    provenance_after = {
        "code": content_manifest(_code_paths(), PROJECT_ROOT),
        "data": content_manifest(_data_paths(storage, padded_dates), PROJECT_ROOT),
    }
    code_unchanged = (
        provenance_before["code"]["combined_sha256"]
        == provenance_after["code"]["combined_sha256"]
    )
    data_unchanged = (
        provenance_before["data"]["combined_sha256"]
        == provenance_after["data"]["combined_sha256"]
    )
    reproducible = code_unchanged and data_unchanged

    report = build_report(
        strict_result, diagnostic_result, reproducible, bt_config,
        expected_sessions=bt_open_dates,
    )
    metrics = report["metrics"]
    diagnostic_metrics = report["diagnostic_metrics"]
    performance_valid = report["performance_valid"]
    invalid_reasons = report["invalid_reasons"]

    unique_event_ids = {e.event_id for e in diagnostic_result.lifecycle_events}

    facts_path = PROJECT_ROOT / "config" / "delisting_facts.json"
    delisting_facts = json.loads(facts_path.read_text())
    audit_rows = _delisting_audit(diagnostic_result, delisting_facts)
    shadow = _shadow_audit(targets, delisting_facts, bt_open_dates)
    fact_sha = sha256_file(facts_path)

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "experiments" / "lifecycle_admission_v0_1" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "engine_schema_version": ENGINE_SCHEMA_VERSION,
        "analysis_type": "portfolio_engineering_backtest",
        "code_version": git_sha_before,
        "workspace_dirty": git_dirty_before,
        "reproducible": reproducible,
        "invalid_reasons": invalid_reasons,
        "run_time": datetime.now().isoformat(),
        "run_id": run_id,
        "requested_period": {"start": PERIOD_START.isoformat(), "end": PERIOD_END.isoformat()},
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
        "lifecycle_boundary": {
            "delist": "valid through delist_date (inclusive); blocked on trade_date > delist_date",
            "code_change": "old instrument invalid from effective_date (inclusive)",
        },
        "conflict_diagnostics": monitor.conflict_diagnostics(),
        "delisting_facts": delisting_facts,
        "delisting_audit": {
            "unique_event_count": len(audit_rows),
            "verified_count": sum(1 for r in audit_rows if r["verification_status"] == "verified"),
            "unknown_count": sum(1 for r in audit_rows if r["verification_status"] == "unknown"),
            "rows": audit_rows,
        },
        "shadow_admission": {
            "policy_name": shadow["policy_name"],
            "policy_version": shadow["policy_version"],
            "fact_sha256": fact_sha,
            "time_convention": (
                "verified public date without intraday time -> next trading day open "
                "(available_from)"
            ),
            "effective_vs_available_note": (
                "effective_date = market event effective; available_from = strategy "
                "usable time; shadow admission uses available_from only"
            ),
            "restricted_target_count": shadow["restricted_target_count"],
            "restricted_target_weight_sum": shadow["restricted_target_weight_sum"],
            "restricted_target_weight_note": shadow["restricted_target_weight_note"],
            "restricted_unique_instruments": shadow["restricted_unique_instruments"],
            "unknown_count": shadow["unknown_count"],
            "unknown_note": shadow["unknown_note"],
            "rows": shadow["rows"],
        },
        "performance_claim": False,
        "test_observed": True,
        "performance_valid": performance_valid,
        "code_manifest": provenance_before["code"],
        "data_manifest": provenance_before["data"],
        "environment": env,
        "strict": {
            "run_mode": strict_result.run_mode,
            "status": strict_result.status,
            "valid_through": (
                strict_result.valid_through.isoformat()
                if strict_result.valid_through else None
            ),
            "simulated_period": {
                "start": strict_result.simulated_period_start.isoformat()
                if strict_result.simulated_period_start else None,
                "end": strict_result.simulated_period_end.isoformat()
                if strict_result.simulated_period_end else None,
            },
            "n_records": len(strict_result.records),
            "n_return_intervals": max(0, len(strict_result.records) - 1),
            "solver_root_residual": strict_result.solver_root_residual,
            "accounting_checks": [c.__dict__ for c in strict_result.accounting_checks],
            "accounting_error": strict_result.accounting_error,
            "accounting_error_date": strict_result.accounting_error_date.isoformat()
            if strict_result.accounting_error_date else None,
            "accounting_error_book": strict_result.accounting_error_book,
            "first_blocking_event": strict_result.first_blocking_event.__dict__
            if strict_result.first_blocking_event else None,
            "metrics": metrics,
            "statistics": _statistics(strict_result),
            "first_blocked_asset_audit": _audit_case(strict_result),
        },
        "diagnostic": {
            "run_mode": diagnostic_result.run_mode,
            "status": diagnostic_result.status,
            "diagnostic_from": diagnostic_result.diagnostic_from.isoformat()
            if diagnostic_result.diagnostic_from else None,
            "simulated_period": {
                "start": diagnostic_result.simulated_period_start.isoformat()
                if diagnostic_result.simulated_period_start else None,
                "end": diagnostic_result.simulated_period_end.isoformat()
                if diagnostic_result.simulated_period_end else None,
            },
            "diagnostic_event_count": len(diagnostic_result.lifecycle_events),
            "unique_event_count": len(unique_event_ids),
            "solver_root_residual": diagnostic_result.solver_root_residual,
            "accounting_checks": [c.__dict__ for c in diagnostic_result.accounting_checks],
            "accounting_error": diagnostic_result.accounting_error,
            "diagnostic_metrics": diagnostic_metrics,
            "diagnostic_metrics_valid": False,
            "statistics": _statistics(diagnostic_result),
        },
        "total_runtime_seconds": runtime,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "manifest.json").write_text(json.dumps(provenance_before, indent=2, default=str))
    (out_dir / "delisting_facts.json").write_text(
        json.dumps(delisting_facts, indent=2, ensure_ascii=False)
    )
    pd.DataFrame(audit_rows).to_csv(out_dir / "delisting_audit.csv", index=False)
    if shadow["rows"]:
        pd.DataFrame(shadow["rows"]).to_csv(out_dir / "shadow_admission.csv", index=False)

    def _failed_attempt_to_dict(fa):
        return {
            "trade_date": fa.trade_date.isoformat(),
            "reason": fa.reason,
            "trades": [t.__dict__ for t in fa.trades],
            "rebalance": fa.rebalance.__dict__ if fa.rebalance else None,
        }

    if strict_result.failed_attempts:
        (out_dir / "strict_failed_attempts.json").write_text(
            json.dumps(
                [_failed_attempt_to_dict(fa) for fa in strict_result.failed_attempts],
                indent=2, default=str,
            )
        )
    if diagnostic_result.failed_attempts:
        (out_dir / "diagnostic_failed_attempts.json").write_text(
            json.dumps(
                [_failed_attempt_to_dict(fa) for fa in diagnostic_result.failed_attempts],
                indent=2, default=str,
            )
        )

    pd.DataFrame([r.__dict__ for r in strict_result.records]).to_csv(
        out_dir / "strict_daily_records.csv", index=False
    )
    pd.DataFrame([r.__dict__ for r in strict_result.rebalances]).to_csv(
        out_dir / "strict_rebalance_log.csv", index=False
    )
    pd.DataFrame([t.__dict__ for t in strict_result.trades]).to_csv(
        out_dir / "strict_trade_details.csv", index=False
    )
    pd.DataFrame([t.__dict__ for t in diagnostic_result.trades]).to_csv(
        out_dir / "diagnostic_trade_details.csv", index=False
    )
    if diagnostic_result.lifecycle_events:
        pd.DataFrame([e.__dict__ for e in diagnostic_result.lifecycle_events]).to_csv(
            out_dir / "diagnostic_lifecycle_events.csv", index=False
        )
    if strict_result.lifecycle_events:
        pd.DataFrame([e.__dict__ for e in strict_result.lifecycle_events]).to_csv(
            out_dir / "strict_lifecycle_events.csv", index=False
        )

    def _write_books(prefix: str, books) -> None:
        book_rows = []
        position_rows = []
        for b in books:
            book_rows.append({
                "trade_date": b.trade_date, "book": b.book, "nav": b.nav,
                "daily_return": b.daily_return, "cash": b.cash,
                "market_pnl": b.market_pnl, "fee": b.fee,
                "gross_exposure": b.gross_exposure, "net_exposure": b.net_exposure,
                "cash_weight": b.cash_weight, "holdings_count": b.holdings_count,
            })
            for p in b.positions:
                position_rows.append({
                    "trade_date": b.trade_date, "book": b.book,
                    "instrument_id": p.instrument_id, "value": p.value,
                    "weight": p.weight, "last_price": p.last_price,
                    "last_mark_date": p.last_mark_date,
                    "missing_price": p.missing_price,
                })
        pd.DataFrame(book_rows).to_csv(out_dir / f"{prefix}_daily_books.csv", index=False)
        pd.DataFrame(position_rows).to_csv(out_dir / f"{prefix}_daily_positions.csv", index=False)

    _write_books("strict", strict_result.books)
    _write_books("diagnostic", diagnostic_result.books)
    if strict_result.skipped_executions:
        pd.DataFrame([s.__dict__ for s in strict_result.skipped_executions]).to_csv(
            out_dir / "strict_skipped_executions.csv", index=False
        )

    print(f"=== research backtest {ENGINE_SCHEMA_VERSION} ===")
    print(f"strict status: {strict_result.status}")
    print(f"performance_valid: {performance_valid}")
    print(f"reproducible: {reproducible}")
    if strict_result.first_blocking_event is not None:
        e = strict_result.first_blocking_event
        print(f"first blocking event: {e.instrument_id} {e.event_type} "
              f"event_date={e.event_date} blocking_session={e.blocking_session} book={e.book}")
        print(f"valid_through: {strict_result.valid_through}")
    else:
        print("no blocking event (strict completed)")
    if strict_result.accounting_error:
        print(f"accounting_error: {strict_result.accounting_error}")
    print(f"solver_root_residual: {strict_result.solver_root_residual:.3e}")
    for c in strict_result.accounting_checks:
        print(f"  accounting[{c.check}] max_abs={c.max_abs:.3e} max_rel={c.max_rel:.3e}")
    if metrics is not None:
        print(f"total_return gross={metrics['total_return_gross']:.4f} "
              f"net={metrics['total_return_net']:.4f}")
    else:
        print("metrics: null (strict run blocked; no valid full-period performance)")
    print(f"diagnostic status: {diagnostic_result.status} "
          f"diagnostic_from={diagnostic_result.diagnostic_from}")
    print(f"diagnostic events: {len(diagnostic_result.lifecycle_events)} "
          f"(unique {len(unique_event_ids)})")
    print(f"shadow admission ({shadow['policy_name']}_{shadow['policy_version']}): "
          f"restricted_targets={shadow['restricted_target_count']} "
          f"restricted_weight_sum={shadow['restricted_target_weight_sum']:.4f} "
          f"restricted_unique={shadow['restricted_unique_instruments']} "
          f"unknown={shadow['unknown_count']}")
    print(f"output dir: {out_dir}")
    print(f"runtime: {runtime:.1f}s")


if __name__ == "__main__":
    _main()
