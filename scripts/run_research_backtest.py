#!/usr/bin/env python3
"""Run the lifecycle date-semantics comparison experiment.

Runs baseline and admission paths under both the legacy delist_date boundary
(``legacy_delist_date_inclusive``) and the candidate ``delist_date_is_first_invalid_v1``
interpretation, each in strict and diagnostic mode, with full provenance.

Performance baseline hierarchy (v0.1 correctness closure):

- PRIMARY: strategy vs ``equal_weight_v1_control`` — a real self-financing
  equal-weight portfolio over the full PIT-eligible V1 cross-section, run
  through the same engine with the same schedule, cost, lifecycle monitor,
  risk policy and settlement scenario. Risk policy first, settlement only as
  the residual fallback (``exit_after_termination_decision_v1`` + validated
  fact snapshot; only ``delist`` events settle).
- SECONDARY: market price-index attribution vs 000300.SH / 000905.SH /
  000852.SH raw closes (``index_return_basis = price_index_close``). Raw
  index closes are NOT dividend-adjusted and are not equivalent to the
  adjusted-stock total-return basis of the strategy. Per-instrument session
  coverage is audited; incomplete coverage invalidates the attribution.
- DIAGNOSTIC: legacy blocked strict path (footnote only), the old
  no-risk-policy settlement paths, and the cross-sectional equal-weight
  return diagnostic (NOT a portfolio).

Settlement sensitivity stays a two-scenario assumption study
(``recovery_assumption_1`` / ``recovery_assumption_0``): scenario bounds over
the assumed [0, last_mark] recovery model. Actual economic recovery may
differ; the scenarios are not mathematically guaranteed economic bounds.

Fixed engineering configuration: momentum_20d (lower_is_better), weekly
rebalance, V1 universe, 20% selection, 10 bps transaction cost.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.alpha import calculate_momentum_alpha
from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    LEGACY_DELIST_DATE_INCLUSIVE,
    MISSING_PRICE_POLICY,
    RUN_MODE_DIAGNOSTIC,
    RUN_MODE_STRICT,
    BacktestConfig,
    DelistingSettlementConfig,
    LifecycleMonitor,
    build_report,
    compute_metrics,
    first_invalid_open_session,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.backtest.admission import (
    ENFORCEMENT_VERSION,
    LIMITED_FACT_COVERAGE,
    POLICY_NAME,
    POLICY_VERSION,
    compute_restricted_by_signal,
    evaluate_buy_rejection,
    shadow_admission,
)
from quantlab.backtest.audit import (
    consumer_impact_audit,
    consumer_matrix,
    fingerprint_frame,
    fingerprint_targets,
    symmetry_audit,
)
from quantlab.backtest.benchmark import (
    compare_benchmark,
    cross_sectional_equal_weight_return_diagnostic,
    index_benchmark_coverage,
    returns_from_closes,
)
from quantlab.backtest.delisting_facts import (
    load_validated_facts,
    retrieval_status_summary,
    source_coverage,
    trusted_facts_available_as_of,
)
from quantlab.backtest.experiment import build_group_report, export_group
from quantlab.backtest.provenance import content_manifest, environment_info
from quantlab.data import ParquetStorage
from quantlab.data.security_history import load_security_code_changes
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.portfolio.control import (
    CONTROL_PORTFOLIO_NAME,
    build_equal_weight_control_targets,
    strategy_control_symmetry_audit,
)
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCHEMA_VERSION = "v0.3.0"
EXPERIMENT_SCHEMA = "lifecycle_date_semantics_v0_1_1"

PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)
LOOKBACK = 20
INDEX_BENCHMARKS = ("000300.SH", "000905.SH", "000852.SH")
EQUAL_WEIGHT_DIAGNOSTIC = "equal_weight_v1_cross_sectional_diagnostic"
BOUND_KEYS = ("recovery_assumption_1", "recovery_assumption_0")


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
    paths.append(PROJECT_ROOT / "config" / "delisting_facts_v2.json")
    paths.append(PROJECT_ROOT / "config" / "delisting_facts_batch.json")
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
        # index benchmark inputs are part of the reproducibility surface:
        # if an index partition changes mid-run, the run is not reproducible
        paths.append(storage.index_daily_path(d))
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
    return price_frame, targets, df, universe, alpha_df


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


def _date_semantics_table(open_dates: list, delist_map: dict) -> list[dict]:
    """Theoretical first-invalid open session within the requested period.

    This is a boundary table, not an observed-blocking table. ``open_dates`` is
    already restricted to the requested period, so ``first_invalid_open_session``
    resolves every relation uniformly:

    - ``before``: the instrument is already invalid at period start, so the
      first invalid open session is the period's first open session.
    - ``within``: the legacy/v1 boundary applies.
    - ``after``: no invalid open session within the period (null).

    ``raw_date_relation_to_requested_period`` is reported for clarity but the
    first-invalid session is always derived through the shared policy.
    """
    rows = []
    for instr, delist_date in sorted(delist_map.items()):
        if delist_date < PERIOD_START:
            relation = "before"
        elif delist_date > PERIOD_END:
            relation = "after"
        else:
            relation = "within"
        legacy = first_invalid_open_session(
            delist_date, open_dates, LEGACY_DELIST_DATE_INCLUSIVE
        )
        v1 = first_invalid_open_session(
            delist_date, open_dates, DELIST_DATE_IS_FIRST_INVALID_V1
        )
        rows.append({
            "instrument_id": instr,
            "raw_delist_date": delist_date.isoformat(),
            "raw_date_relation_to_requested_period": relation,
            "legacy_first_invalid_open_session_in_period": (
                legacy.isoformat() if legacy else None
            ),
            "v1_first_invalid_open_session_in_period": (
                v1.isoformat() if v1 else None
            ),
        })
    return rows


def _index_closes(
    storage: ParquetStorage, coverage_dates: list[date], instrument_id: str
) -> dict[date, float]:
    """Load one index's per-session closes from canonical index_daily.

    Every open session in ``coverage_dates`` must have a stored file (synced
    by ``sync_index_daily_history``); a missing file is an error, never a
    silent gap, because a gap would corrupt the return denominator.
    """
    missing = [d for d in coverage_dates if not storage.index_daily_exists(d)]
    if missing:
        first, last = missing[0], missing[-1]
        raise RuntimeError(
            f"index_daily data missing for {instrument_id} on {len(missing)} "
            f"sessions ({first} .. {last}); run sync_index_daily_history first"
        )
    closes: dict[date, float] = {}
    for d in sorted(coverage_dates):
        for bar in storage.load_index_daily_by_date(d):
            if bar.instrument_id == instrument_id:
                closes[bar.trade_date] = bar.close
    if not closes:
        raise RuntimeError(
            f"no index_daily rows found for {instrument_id} "
            f"({coverage_dates[0]} .. {coverage_dates[-1]})"
        )
    return closes


def _build_benchmark_inputs(
    storage: ParquetStorage, coverage_dates: list[date], universe: pd.DataFrame
) -> tuple[
    dict[str, dict[date, float]],
    dict[str, dict[str, object]],
    dict[date, float],
]:
    """Index closes + coverage audit + diagnostic return series.

    Returns ``(index_returns, index_coverage, diagnostic_returns)``. The
    coverage audit is per index instrument over every expected market
    session; formal attribution is only allowed when every index is
    ``complete`` (2020-2024 has no pre-inception excuse). The diagnostic is
    the cross-sectional equal-weight return mean of the V1 universe — NOT a
    portfolio.
    """
    index_returns: dict[str, dict[date, float]] = {}
    coverage: dict[str, object] = {}
    for instrument_id in INDEX_BENCHMARKS:
        closes = _index_closes(storage, coverage_dates, instrument_id)
        audit = index_benchmark_coverage(instrument_id, closes, coverage_dates)
        coverage[instrument_id] = audit.to_dict()
        if not audit.complete:
            raise RuntimeError(
                f"index benchmark coverage incomplete for {instrument_id}: "
                f"{len(audit.missing_sessions)} missing sessions "
                f"({audit.missing_sessions[0]} .. {audit.missing_sessions[-1]}); "
                "formal attribution requires complete per-session closes"
            )
        index_returns[instrument_id] = returns_from_closes(
            sorted(closes.items())
        )
    diagnostic = cross_sectional_equal_weight_return_diagnostic(
        universe[["instrument_id", "trade_date", "adj_close"]]
    )
    return index_returns, coverage, diagnostic


def _attribution_or_fail(
    result, benchmark_name: str, series: dict[date, float], annualization: int
) -> dict:
    """Compare one run against one benchmark; enforce complete n_obs.

    With complete benchmark coverage the observation count must equal the
    full strategy return intervals (``len(records) - 1``); anything else
    invalidates the attribution instead of silently shrinking it.
    """
    stats = compare_benchmark(
        result.records, benchmark_name, series, annualization=annualization,
        book="net",
    ).to_dict()
    expected_intervals = len(result.records) - 1
    if stats["n_obs"] != expected_intervals:
        raise RuntimeError(
            f"attribution invalid for {benchmark_name}: n_obs="
            f"{stats['n_obs']} != expected return intervals "
            f"{expected_intervals}"
        )
    return stats


def _benchmark_comparison_section(
    runs: dict[str, dict[str, object]],
    legacy_settlement: dict[str, object],
    legacy_strict,
    index_returns: dict[str, dict[date, float]],
    index_coverage: dict[str, object],
    diagnostic_returns: dict[date, float],
    symmetry: dict[str, bool],
    annualization: int,
) -> dict:
    """Three-layer attribution for the two settlement bounds.

    PRIMARY: strategy vs the real equal_weight_v1_control portfolio.
    SECONDARY: market price-index attribution vs 000300/000905/000852.
    DIAGNOSTIC: legacy blocked path, no-risk-policy settlement paths, and the
    cross-sectional equal-weight return diagnostic (not a portfolio).
    """
    from quantlab.backtest.benchmark import strategy_daily_returns

    primary: dict[str, dict] = {}
    secondary: dict[str, dict] = {}
    diagnostic_paths: dict[str, dict] = {}
    for bound_key in BOUND_KEYS:
        strategy_result = runs[bound_key]["strategy"]
        control_result = runs[bound_key]["control"]
        control_series = strategy_daily_returns(control_result.records, book="net")
        primary[bound_key] = _attribution_or_fail(
            strategy_result, CONTROL_PORTFOLIO_NAME, control_series, annualization
        )
        secondary[bound_key] = {
            instrument_id: _attribution_or_fail(
                strategy_result, instrument_id, index_returns[instrument_id],
                annualization,
            )
            for instrument_id in INDEX_BENCHMARKS
        }
        legacy = legacy_settlement[bound_key]
        diagnostic_paths[bound_key] = {
            "status": legacy.status,
            "risk_policy": "none (legacy engineering diagnostic)",
            "settled_instruments": len(legacy.settlement_events),
            "note": (
                "no-risk-policy settlement path retained as a legacy "
                "engineering diagnostic; NOT a performance-valid baseline"
            ),
        }
    beta_vs_control = {bound_key: primary[bound_key]["beta"] for bound_key in BOUND_KEYS}
    return {
        "note": (
            "three-layer attribution of the settlement bounds; strict inner "
            "join on actual record dates (missing benchmark dates are "
            "dropped, never filled); risk-free rate = 0.0; alpha is daily "
            "OLS, alpha_annualized = alpha_daily * annualization (arithmetic)"
        ),
        "risk_free_rate": 0.0,
        "annualization": annualization,
        "book": "net",
        "benchmark_definitions": {
            CONTROL_PORTFOLIO_NAME: (
                "real self-financing equal-weight portfolio over the full "
                "PIT-eligible V1 cross-section, built per weekly signal date "
                "and executed by the same engine with the same cost, "
                "lifecycle monitor, risk policy and settlement scenario as "
                "the strategy"
            ),
            "000300.SH": "tushare index_daily, canonical index_daily partition",
            "000905.SH": "tushare index_daily, canonical index_daily partition",
            "000852.SH": "tushare index_daily, canonical index_daily partition",
            EQUAL_WEIGHT_DIAGNOSTIC: (
                "cross-sectional diagnostic only: daily mean of per-instrument "
                "consecutive available returns; not a portfolio"
            ),
        },
        "primary": {
            "question": (
                "does stock selection add value over the same universe held "
                "equal-weight under identical execution/risk/settlement?"
            ),
            "benchmark": CONTROL_PORTFOLIO_NAME,
            "result": primary,
            "beta_vs_control": {
                **beta_vs_control,
                "interpretation_note": (
                    "the strategy is a ~20% cross-sectional slice of the same "
                    "V1 universe in which the control invests fully, so beta "
                    "against the control is expected to be close to 1"
                ),
            },
        },
        "secondary": {
            "question": (
                "how does the strategy behave relative to the market indices "
                "(beta / active behavior)?"
            ),
            "index_return_basis": "price_index_close",
            "coverage_note": (
                "formal index attribution requires complete per-session "
                "close coverage per instrument over the requested period "
                "(2020-2024 has no pre-inception excuse)"
            ),
            "coverage": index_coverage,
            "result": secondary,
        },
        "diagnostic": {
            "legacy_blocked_path_footnote": {
                "path": "strict (legacy delist-date blocking, no settlement)",
                "status": legacy_strict.status,
                "note": (
                    "the legacy strict path is blocked_by_unsupported_event; no "
                    "benchmark attribution is computed for it because its return "
                    "series ends at the first blocking session and any "
                    "full-period comparison would be meaningless"
                ),
            },
            "no_risk_policy_settlement_paths": diagnostic_paths,
            "cross_sectional_equal_weight_return_diagnostic": {
                "definition": (
                    "daily mean of per-instrument consecutive available "
                    "returns over the PIT V1 universe; NOT a self-financing "
                    "portfolio (suspended names drop out of the daily "
                    "denominator and resume with lump returns; no cost is "
                    "charged); retained as a diagnostic only"
                ),
                "n_dates": len(diagnostic_returns),
                "first_date": min(diagnostic_returns).isoformat()
                if diagnostic_returns
                else None,
                "last_date": max(diagnostic_returns).isoformat()
                if diagnostic_returns
                else None,
            },
        },
        "symmetry_audit": symmetry,
    }


def _print_benchmark_table(section: dict) -> None:
    """Human-readable attribution tables (primary control + index)."""
    print("benchmark attribution (net book):")
    header = (
        f"  {'benchmark':<16}{'n_obs':>7}{'beta':>8}{'alpha_ann':>11}"
        f"{'TE':>8}{'IR':>8}{'act_CAGR':>10}{'act_MDD':>10}"
    )
    for bound_key in BOUND_KEYS:
        print(f"  [{bound_key}] PRIMARY strategy vs {CONTROL_PORTFOLIO_NAME}")
        print(header)
        row = section["primary"]["result"][bound_key]
        print(
            f"  {CONTROL_PORTFOLIO_NAME:<16}{row['n_obs']:>7}{row['beta']:>8.3f}"
            f"{row['alpha_annualized']:>11.4f}{row['tracking_error']:>8.4f}"
            f"{row['information_ratio']:>8.3f}{row['active_cagr']:>10.4f}"
            f"{row['active_max_drawdown']:>10.4f}"
        )
        print(f"  [{bound_key}] SECONDARY market price-index attribution")
        print(header)
        for name in INDEX_BENCHMARKS:
            row = section["secondary"]["result"][bound_key][name]
            print(
                f"  {name:<16}{row['n_obs']:>7}{row['beta']:>8.3f}"
                f"{row['alpha_annualized']:>11.4f}{row['tracking_error']:>8.4f}"
                f"{row['information_ratio']:>8.3f}{row['active_cagr']:>10.4f}"
                f"{row['active_max_drawdown']:>10.4f}"
            )


def _settlement_bound(result, config) -> dict:
    """Bound-level metrics, affected instruments and average settled weight."""
    metrics = compute_metrics(result.records, result.rebalances, config)
    net_rows = [e for e in result.settlement_events if e.book == "net"]
    nav_map = {r.trade_date: r.nav_net for r in result.records}
    weights = {}
    for e in net_rows:
        nav = nav_map.get(e.blocking_session)
        if nav:
            weights[e.instrument_id] = e.last_mark_value / nav
    return {
        "status": result.status,
        "total_return_net": metrics["total_return_net"],
        "cagr_net": metrics["cagr_net"],
        "sharpe_net": metrics["sharpe_net"],
        "max_drawdown_net": metrics["max_drawdown_net"],
        "affected_instrument_count": len({e.instrument_id for e in net_rows}),
        "settled_instrument_average_weight_at_settlement": (
            sum(weights.values()) / len(weights) if weights else 0.0
        ),
    }


def _compare_paths(base_strict, adm_strict, base_diag, adm_diag,
                   restricted_by_signal, bt_open_dates) -> dict:
    base_dates = {r.trade_date for r in base_strict.records}
    adm_dates = {r.trade_date for r in adm_strict.records}
    common_dates = base_dates & adm_dates

    base_trades = [t for t in base_strict.trades if t.execution_date in common_dates]
    adm_trades = [t for t in adm_strict.trades if t.execution_date in common_dates]

    base_map = {
        (t.execution_date, t.book, t.instrument_id): t.signed_trade_value
        for t in base_trades
    }
    adm_map = {
        (t.execution_date, t.book, t.instrument_id): t.signed_trade_value
        for t in adm_trades
    }
    first_diff = None
    for key in sorted(set(base_map) | set(adm_map)):
        b = base_map.get(key, 0.0)
        a = adm_map.get(key, 0.0)
        if abs(b - a) > 1e-12:
            first_diff = {
                "execution_date": key[0].isoformat(),
                "book": key[1],
                "instrument_id": key[2],
                "baseline_signed": b,
                "admission_signed": a,
            }
            break

    restriction_applicable_count = sum(
        len(s) for s in restricted_by_signal.values()
    )
    buy_cap_binding_net = sum(
        rb.restricted_binding_count for rb in adm_strict.rebalances
    )
    buy_cap_binding_gross = sum(
        rb.gross_book_restricted_binding_count for rb in adm_strict.rebalances
    )
    allowed_sell_occurrences = sum(
        1 for t in adm_strict.trades
        if t.reason == "sold"
        and t.instrument_id in restricted_by_signal.get(t.signal_date, frozenset())
    )

    # iterate every signal/instrument pair, not just the first signal per stock
    rejection_evaluations = []
    for sig in sorted(restricted_by_signal):
        restricted_set = restricted_by_signal[sig]
        exec_date = _next_open_session(bt_open_dates, sig)
        if exec_date is None:
            continue
        for instr in sorted(restricted_set):
            result = evaluate_buy_rejection(
                instr, exec_date, base_strict.trades, adm_strict.trades,
                adm_strict.rebalances, restricted_set,
            )
            rejection_evaluations.append({
                "instrument_id": instr,
                "signal_date": sig.isoformat(),
                "execution_date": exec_date.isoformat(),
                "rejection": result,
            })

    return {
        "first_trade_difference": first_diff,
        "restriction_applicable_count": restriction_applicable_count,
        "buy_cap_binding": {"net": buy_cap_binding_net, "gross": buy_cap_binding_gross},
        "allowed_sell_occurrences": allowed_sell_occurrences,
        "rejection_evaluations": rejection_evaluations,
        "admission_next_blocking_event": (
            adm_strict.first_blocking_event.__dict__
            if adm_strict.first_blocking_event else None
        ),
        "admission_strict_status": adm_strict.status,
        "admission_valid_through": (
            adm_strict.valid_through.isoformat() if adm_strict.valid_through else None
        ),
        "baseline_strict_status": base_strict.status,
        "baseline_valid_through": (
            base_strict.valid_through.isoformat() if base_strict.valid_through else None
        ),
    }


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

    price_frame, targets, research_df, universe, alpha_df = _build_targets(
        storage, signal_dates
    )
    bt_open_dates = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    bt_config = BacktestConfig(initial_nav=1.0, transaction_cost_bps=10.0, annualization=252)
    monitor = LifecycleMonitor(
        securities, code_changes, mode=LEGACY_DELIST_DATE_INCLUSIVE
    )
    monitor_new = LifecycleMonitor(
        securities, code_changes, mode=DELIST_DATE_IS_FIRST_INVALID_V1
    )

    facts_path = PROJECT_ROOT / "config" / "delisting_facts.json"
    facts_v2_path = PROJECT_ROOT / "config" / "delisting_facts_v2.json"
    batch_path = PROJECT_ROOT / "config" / "delisting_facts_batch.json"
    calendar_entries = [(c.trade_date, c.is_open) for c in calendar]
    delisting_facts, fact_sha, fact_errors = load_validated_facts(
        facts_path, calendar_entries
    )
    if fact_errors:
        raise ValueError("; ".join(fact_errors))
    delisting_facts_v2, fact_sha_v2, fact_errors_v2 = load_validated_facts(
        facts_v2_path, calendar_entries
    )
    if fact_errors_v2:
        raise ValueError("; ".join(fact_errors_v2))
    batch = json.loads(batch_path.read_text())

    restricted_by_signal = compute_restricted_by_signal(targets, delisting_facts)
    restricted_by_signal_v2 = compute_restricted_by_signal(targets, delisting_facts_v2)

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
    admission_strict = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal,
    )
    admission_diagnostic = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_DIAGNOSTIC, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal,
    )
    admission_v2_strict = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal_v2,
    )
    admission_v2_diagnostic = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_DIAGNOSTIC, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal_v2,
    )
    baseline_new_strict = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor_new,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )
    baseline_new_diagnostic = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_DIAGNOSTIC, lifecycle=monitor_new,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )
    admission_v2_new_strict = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor_new,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal_v2,
    )
    admission_v2_new_diagnostic = run_backtest(
        price_frame, bt_open_dates, targets, bt_config,
        execution_lag_sessions=1, mode=RUN_MODE_DIAGNOSTIC, lifecycle=monitor_new,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        restricted_by_signal=restricted_by_signal_v2,
    )

    # delisting settlement policy v0: explicit assumption bounds on the same
    # legacy baseline configuration. Legacy paths run WITHOUT the risk policy
    # (kept as engineering diagnostics only).
    settlement_config = replace(
        bt_config,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=1.0, settlement_fee_bps=0.0
        ),
    )
    settlement_zero_config = replace(
        bt_config,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=0.0, settlement_fee_bps=0.0
        ),
    )
    settlement_strict = run_backtest(
        price_frame, bt_open_dates, targets, settlement_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )
    settlement_zero_strict = run_backtest(
        price_frame, bt_open_dates, targets, settlement_zero_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
    )

    # PRIMARY performance baseline: PIT lifecycle risk policy FIRST, and the
    # delisting settlement only as the residual fallback. Trusted-fact forced
    # exits (exit_after_termination_decision_v1) use the validated fact
    # snapshot; only lifecycle-invalid residual delist positions without a
    # valid exit price reach the settlement assumption.
    settlement_risk_strict = run_backtest(
        price_frame, bt_open_dates, targets, settlement_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        risk_facts=delisting_facts, risk_policy=EXIT_POLICY_ID,
    )
    settlement_risk_zero_strict = run_backtest(
        price_frame, bt_open_dates, targets, settlement_zero_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        risk_facts=delisting_facts, risk_policy=EXIT_POLICY_ID,
    )

    # formal equal_weight_v1_control: real portfolio, same schedule/cost/
    # lifecycle/risk/settlement, only the target construction differs
    control_targets = build_equal_weight_control_targets(universe, signal_dates)
    control_recovery_1 = run_backtest(
        price_frame, bt_open_dates, control_targets, settlement_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        risk_facts=delisting_facts, risk_policy=EXIT_POLICY_ID,
    )
    control_recovery_0 = run_backtest(
        price_frame, bt_open_dates, control_targets, settlement_zero_config,
        execution_lag_sessions=1, mode=RUN_MODE_STRICT, lifecycle=monitor,
        requested_period_start=PERIOD_START, requested_period_end=PERIOD_END,
        risk_facts=delisting_facts, risk_policy=EXIT_POLICY_ID,
    )

    # benchmark inputs: per-instrument session coverage is audited; formal
    # index attribution fails hard on incomplete coverage
    index_returns, index_coverage, diagnostic_returns = _build_benchmark_inputs(
        storage, padded_dates, universe
    )

    control_symmetry = {}
    for bound_key, recovery in (
        ("recovery_assumption_1", settlement_config.delisting_settlement.recovery_rate),
        ("recovery_assumption_0", settlement_zero_config.delisting_settlement.recovery_rate),
    ):
        control_symmetry[bound_key] = strategy_control_symmetry_audit(
            {
                "open_dates": bt_open_dates,
                "signal_dates": sorted(targets),
                "cost_rate": bt_config.transaction_cost_bps,
                "mode": RUN_MODE_STRICT,
                "risk_policy": EXIT_POLICY_ID,
                "risk_facts": fact_sha,
                "recovery_rate": recovery,
                "execution_lag_sessions": 1,
                "missing_price_policy": MISSING_PRICE_POLICY,
            },
            {
                "open_dates": bt_open_dates,
                "signal_dates": sorted(control_targets),
                "cost_rate": bt_config.transaction_cost_bps,
                "mode": RUN_MODE_STRICT,
                "risk_policy": EXIT_POLICY_ID,
                "risk_facts": fact_sha,
                "recovery_rate": recovery,
                "execution_lag_sessions": 1,
                "missing_price_policy": MISSING_PRICE_POLICY,
            },
        )
    control_symmetry["same_target_construction_allowed_to_differ"] = (
        fingerprint_targets(targets) != fingerprint_targets(control_targets)
    )

    runs = {
        "recovery_assumption_1": {
            "strategy": settlement_risk_strict, "control": control_recovery_1,
        },
        "recovery_assumption_0": {
            "strategy": settlement_risk_zero_strict, "control": control_recovery_0,
        },
    }
    legacy_settlement = {
        "recovery_assumption_1": settlement_strict,
        "recovery_assumption_0": settlement_zero_strict,
    }
    benchmark_section = _benchmark_comparison_section(
        runs, legacy_settlement, strict_result, index_returns, index_coverage,
        diagnostic_returns, control_symmetry, bt_config.annualization,
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

    settlement_report = build_report(
        settlement_risk_strict, None, reproducible, settlement_config,
        expected_sessions=bt_open_dates,
    )
    bound_full = _settlement_bound(settlement_risk_strict, settlement_config)
    bound_zero = _settlement_bound(settlement_risk_zero_strict, settlement_zero_config)
    settlement_sensitivity = {
        "note": (
            "same strict configuration, targets, risk policy and fact "
            "snapshot; only recovery_rate varies between the two scenarios "
            "(1.0 = settle at the last available mark, 0.0 = settle at zero). "
            "These are scenario bounds over the assumed [0, last_mark] "
            "recovery model, NOT mathematically guaranteed economic bounds: "
            "actual economic recovery may differ from this simplified "
            "assumption. The gap measures how much conclusions depend on "
            "the settlement rate."
        ),
        "risk_policy": EXIT_POLICY_ID,
        "risk_facts_sha256": fact_sha,
        "recovery_assumption_1": bound_full,
        "recovery_assumption_0": bound_zero,
        "deltas": {
            "total_return_net": abs(
                bound_zero["total_return_net"] - bound_full["total_return_net"]
            ),
            "cagr_net": abs(bound_zero["cagr_net"] - bound_full["cagr_net"]),
            "sharpe_net": abs(bound_zero["sharpe_net"] - bound_full["sharpe_net"]),
            "max_drawdown_net": abs(
                bound_zero["max_drawdown_net"] - bound_full["max_drawdown_net"]
            ),
        },
        "cagr_net_delta": abs(bound_zero["cagr_net"] - bound_full["cagr_net"]),
        "affected_instruments": sorted(
            {e.instrument_id for e in settlement_risk_strict.settlement_events}
        ),
    }

    unique_event_ids = {e.event_id for e in diagnostic_result.lifecycle_events}

    audit_rows = _delisting_audit(diagnostic_result, delisting_facts)
    shadow = _shadow_audit(targets, delisting_facts, bt_open_dates)
    comparison = _compare_paths(
        strict_result, admission_strict, diagnostic_result, admission_diagnostic,
        restricted_by_signal, bt_open_dates,
    )
    comparison_bc = _compare_paths(
        admission_strict, admission_v2_strict, admission_diagnostic, None,
        restricted_by_signal_v2, bt_open_dates,
    )
    date_semantics = _date_semantics_table(bt_open_dates, monitor_new.delist_map)

    # symmetric date-semantics groups (each strict + diagnostic)
    group_a_legacy = build_group_report(
        strict_result, diagnostic_result, bt_config, bt_open_dates, reproducible,
        lifecycle_mode=LEGACY_DELIST_DATE_INCLUSIVE,
    )
    group_c_legacy = build_group_report(
        admission_v2_strict, admission_v2_diagnostic, bt_config, bt_open_dates,
        reproducible, lifecycle_mode=LEGACY_DELIST_DATE_INCLUSIVE,
    )
    group_a_v1 = build_group_report(
        baseline_new_strict, baseline_new_diagnostic, bt_config, bt_open_dates,
        reproducible, lifecycle_mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )
    group_c_v1 = build_group_report(
        admission_v2_new_strict, admission_v2_new_diagnostic, bt_config,
        bt_open_dates, reproducible, lifecycle_mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )

    # observed blocking (from lifecycle events), separate from the theoretical table
    observed_blocking = {
        "legacy_baseline": {
            "blocking_session": (
                strict_result.first_blocking_event.blocking_session.isoformat()
                if strict_result.first_blocking_event else None
            ),
            "instrument_id": (
                strict_result.first_blocking_event.instrument_id
                if strict_result.first_blocking_event else None
            ),
        },
        "v1_baseline": {
            "blocking_session": (
                baseline_new_strict.first_blocking_event.blocking_session.isoformat()
                if baseline_new_strict.first_blocking_event else None
            ),
            "instrument_id": (
                baseline_new_strict.first_blocking_event.instrument_id
                if baseline_new_strict.first_blocking_event else None
            ),
        },
    }

    consumer_matrix_rows = consumer_matrix()
    consumer_impact = consumer_impact_audit(
        research_df, universe, alpha_df, targets, monitor_new.delist_map
    )

    # configuration symmetry audit: legacy vs v1 share data/targets/config
    strategy_config_snapshot = {
        "transaction_cost_bps": bt_config.transaction_cost_bps,
        "initial_nav": bt_config.initial_nav,
        "annualization": bt_config.annualization,
        "execution_lag_sessions": 1,
        "requested_period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
        "missing_price_policy": MISSING_PRICE_POLICY,
    }
    data_fingerprint = fingerprint_frame(price_frame)
    target_fingerprint = fingerprint_targets(targets)
    symmetry = symmetry_audit(
        {
            "data_fingerprint": data_fingerprint,
            "target_fingerprint": target_fingerprint,
            "config": strategy_config_snapshot,
            "lifecycle_mode": LEGACY_DELIST_DATE_INCLUSIVE,
        },
        {
            "data_fingerprint": data_fingerprint,
            "target_fingerprint": target_fingerprint,
            "config": strategy_config_snapshot,
            "lifecycle_mode": DELIST_DATE_IS_FIRST_INVALID_V1,
        },
    )

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = PROJECT_ROOT / "data" / "experiments" / EXPERIMENT_SCHEMA / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "engine_schema_version": ENGINE_SCHEMA_VERSION,
        "experiment_schema": EXPERIMENT_SCHEMA,
        "analysis_type": "lifecycle_date_semantics_comparison",
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
            "delisting_settlement": {
                "recovery_rate": settlement_config.delisting_settlement.recovery_rate,
                "settlement_fee_bps": (
                    settlement_config.delisting_settlement.settlement_fee_bps
                ),
                "gross_book_settlement_fee_bps": 0.0,
                "scope": (
                    "delisting settlement assumption: covers held positions "
                    "with event_type == 'delist' only; code_change/conflict "
                    "events keep the strict blocked path. The PRIMARY "
                    "baseline applies the PIT risk policy first; settlement "
                    "is the residual fallback. The legacy no-risk-policy "
                    "settlement paths are retained as diagnostics only."
                ),
            },
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
            "code_change": "old instrument invalid from effective_date (inclusive)",
            "modes": {
                LEGACY_DELIST_DATE_INCLUSIVE: (
                    "valid through delist_date; invalid after delist_date "
                    "(trade_date > delist_date)"
                ),
                DELIST_DATE_IS_FIRST_INVALID_V1: (
                    "delist_date is the delisting effective date; invalid from "
                    "delist_date (trade_date >= delist_date)"
                ),
            },
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
        "primary_baseline_performance_valid": (
            settlement_report["performance_valid"]
        ),
        "result_hierarchy": {
            "primary": "strategy vs equal_weight_v1_control (same engine/risk/settlement)",
            "secondary": "market price-index attribution vs CSI300/CSI500/CSI1000",
            "diagnostic": [
                "legacy blocked strict path (footnote)",
                "no-risk-policy settlement paths",
                "cross-sectional equal-weight return diagnostic (not a portfolio)",
            ],
        },
        "termination_fact_source": {
            "source": "config/delisting_facts.json (validated golden snapshot)",
            "fact_sha256": fact_sha,
            "coverage_limited": True,
            "unknown_is_not_safe": True,
            "note": (
                "anns_d remains blocked_by_missing_anns_d_permission; no "
                "manual delisting facts were added this round and no ST "
                "forced-delisting rule exists. The validated snapshot is an "
                "explicitly disclosed partial source."
            ),
        },
        "index_return_basis": "price_index_close",
        "control_target_fingerprint": fingerprint_targets(control_targets),
        "strategy_target_fingerprint": fingerprint_targets(targets),
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
        "strict_settlement_baseline": {
            "policy": "delisting_settlement_v0",
            "role": (
                "PRIMARY performance baseline: PIT lifecycle risk policy "
                "first (trusted-fact forced exits), settlement only as the "
                "residual fallback for lifecycle-invalid delist positions "
                "without a valid exit price"
            ),
            "risk_policy": EXIT_POLICY_ID,
            "risk_fact_snapshot": {
                "source": "config/delisting_facts.json (validated golden)",
                "fact_sha256": fact_sha,
                "coverage_limited": True,
                "unknown_is_not_safe": True,
                "note": (
                    "anns_d remains blocked_by_missing_anns_d_permission; "
                    "the validated snapshot is an explicitly disclosed "
                    "partial source"
                ),
            },
            "settlement_scope": "event_type == delist only (held positions)",
            "recovery_rate": settlement_config.delisting_settlement.recovery_rate,
            "settlement_fee_bps": (
                settlement_config.delisting_settlement.settlement_fee_bps
            ),
            "lifecycle_mode": LEGACY_DELIST_DATE_INCLUSIVE,
            "status": settlement_risk_strict.status,
            "performance_valid": settlement_report["performance_valid"],
            "invalid_reasons": settlement_report["invalid_reasons"],
            "n_records": len(settlement_risk_strict.records),
            "valid_through": (
                settlement_risk_strict.valid_through.isoformat()
                if settlement_risk_strict.valid_through else None
            ),
            "solver_root_residual": settlement_risk_strict.solver_root_residual,
            "accounting_checks": [
                c.__dict__ for c in settlement_risk_strict.accounting_checks
            ],
            "accounting_error": settlement_risk_strict.accounting_error,
            "first_blocking_event": (
                settlement_risk_strict.first_blocking_event.__dict__
                if settlement_risk_strict.first_blocking_event else None
            ),
            "risk_forced_exit_count": len(settlement_risk_strict.risk_policy_audit),
            "residual_settlement_count": len(settlement_risk_strict.settlement_events),
            "metrics": settlement_report["metrics"],
            "settlement_disclosure": settlement_report["settlement_disclosure"],
            "statistics": _statistics(settlement_risk_strict),
        },
        "diagnostic_settlement_paths_no_risk_policy": {
            "recovery_assumption_1": {
                "status": settlement_strict.status,
                "settled_instruments": len(settlement_strict.settlement_events),
                "cagr_net": _settlement_bound(settlement_strict, settlement_config)[
                    "cagr_net"
                ],
            },
            "recovery_assumption_0": {
                "status": settlement_zero_strict.status,
                "settled_instruments": len(settlement_zero_strict.settlement_events),
                "cagr_net": _settlement_bound(
                    settlement_zero_strict, settlement_zero_config
                )["cagr_net"],
            },
            "note": (
                "legacy no-risk-policy settlement paths kept as engineering "
                "diagnostics; superseded by strict_settlement_baseline "
                "(risk policy first, settlement residual)"
            ),
        },
        "settlement_sensitivity": settlement_sensitivity,
        "benchmark_comparison": benchmark_section,
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
        "admission": {
            "policy_name": POLICY_NAME,
            "policy_version": POLICY_VERSION,
            "enforcement_version": ENFORCEMENT_VERSION,
            "limited_fact_coverage": LIMITED_FACT_COVERAGE,
            "unknown_action": "baseline_passthrough",
            "scope_note": (
                "restricted set computed from trusted facts at signal_date and "
                "carried to execution; no execution-date re-check of later "
                "announcements"
            ),
            "restriction_semantics": {
                "not_held": "forbid buy",
                "held": "forbid positive signed_trade_value",
                "reduce": "allow sell per original target",
                "not_in_target": "allow sell per original logic",
                "missing_or_blocked": "follow original freeze rules",
                "no_forced_liquidation": True,
            },
            "strict": {
                "status": admission_strict.status,
                "valid_through": (
                    admission_strict.valid_through.isoformat()
                    if admission_strict.valid_through else None
                ),
                "first_blocking_event": admission_strict.first_blocking_event.__dict__
                if admission_strict.first_blocking_event else None,
                "accounting_error": admission_strict.accounting_error,
                "solver_root_residual": admission_strict.solver_root_residual,
                "n_records": len(admission_strict.records),
            },
            "diagnostic": {
                "status": admission_diagnostic.status,
                "diagnostic_from": admission_diagnostic.diagnostic_from.isoformat()
                if admission_diagnostic.diagnostic_from else None,
                "diagnostic_event_count": len(admission_diagnostic.lifecycle_events),
            },
        },
        "fact_batch": batch,
        "admission_v2": {
            "fact_sha256": fact_sha_v2,
            "retrieval_status": retrieval_status_summary(delisting_facts_v2),
            "strict_status": admission_v2_strict.status,
            "strict_valid_through": (
                admission_v2_strict.valid_through.isoformat()
                if admission_v2_strict.valid_through else None
            ),
            "first_blocking_event": admission_v2_strict.first_blocking_event.__dict__
            if admission_v2_strict.first_blocking_event else None,
            "restricted_instruments": sum(
                len(s) for s in restricted_by_signal_v2.values()
            ),
            "note": (
                "expanded batch fact file; only instruments with trusted "
                "termination-decision facts are restricted, so B==C when no new "
                "verified facts are added"
            ),
        },
        "comparison": comparison,
        "comparison_bc": comparison_bc,
        "groups": {
            "A_legacy_baseline": group_a_legacy,
            "C_legacy_admission_v2": group_c_legacy,
            "A_v1_baseline": group_a_v1,
            "C_v1_admission_v2": group_c_v1,
        },
        "date_semantics": {
            "legacy_mode": LEGACY_DELIST_DATE_INCLUSIVE,
            "new_mode": DELIST_DATE_IS_FIRST_INVALID_V1,
            "theoretical_boundary_table": {
                "note": (
                    "theoretical first-invalid open session within the requested "
                    "period; NOT an observed blocking table"
                ),
                "rows": date_semantics,
            },
            "observed_blocking": observed_blocking,
            "consumer_matrix": consumer_matrix_rows,
            "consumer_impact": consumer_impact,
            "symmetry_audit": symmetry,
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

    export_group(out_dir, "A_legacy_baseline", strict_result)
    export_group(out_dir, "A_legacy_baseline_diagnostic", diagnostic_result)
    export_group(out_dir, "C_legacy_admission_v2", admission_v2_strict)
    export_group(out_dir, "C_legacy_admission_v2_diagnostic", admission_v2_diagnostic)
    export_group(out_dir, "A_v1_baseline", baseline_new_strict)
    export_group(out_dir, "A_v1_baseline_diagnostic", baseline_new_diagnostic)
    export_group(out_dir, "C_v1_admission_v2", admission_v2_new_strict)
    export_group(out_dir, "C_v1_admission_v2_diagnostic", admission_v2_new_diagnostic)
    export_group(out_dir, "legacy_settlement_recovery_1", settlement_strict)
    export_group(out_dir, "legacy_settlement_recovery_0", settlement_zero_strict)
    export_group(
        out_dir, "primary_strategy_recovery_assumption_1", settlement_risk_strict
    )
    export_group(
        out_dir, "primary_strategy_recovery_assumption_0", settlement_risk_zero_strict
    )
    export_group(out_dir, "equal_weight_v1_control_recovery_assumption_1", control_recovery_1)
    export_group(out_dir, "equal_weight_v1_control_recovery_assumption_0", control_recovery_0)

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
    print(f"admission strict status: {admission_strict.status} "
          f"valid_through={admission_strict.valid_through}")
    print(f"first trade difference: {comparison['first_trade_difference']}")
    print(f"restriction_applicable: {comparison['restriction_applicable_count']} "
          f"buy_cap_binding: {comparison['buy_cap_binding']} "
          f"allowed_sell: {comparison['allowed_sell_occurrences']}")
    print(f"rejection evaluations: {comparison['rejection_evaluations']}")
    print(f"legacy settlement (diagnostic) status: {settlement_strict.status} "
          f"settlements={len(settlement_strict.settlement_events)}")
    print(f"PRIMARY baseline status: {settlement_risk_strict.status} "
          f"performance_valid={settlement_report['performance_valid']} "
          f"risk_forced_exits={len(settlement_risk_strict.risk_policy_audit)} "
          f"residual_settlements={len(settlement_risk_strict.settlement_events)}")
    if settlement_report["metrics"] is not None:
        sm = settlement_report["metrics"]
        print(f"PRIMARY baseline: total_return_net={sm['total_return_net']:.4f} "
              f"cagr_net={sm['cagr_net']:.4f} sharpe_net={sm['sharpe_net']:.4f} "
              f"max_drawdown_net={sm['max_drawdown_net']:.4f}")
    print(f"control status: {control_recovery_1.status} "
          f"n_holdings_first_target="
          f"{len(next(iter(control_targets.values())).positions)}")
    print(f"settlement bounds cagr_net: "
          f"recovery_assumption_1={bound_full['cagr_net']:.4f} "
          f"recovery_assumption_0={bound_zero['cagr_net']:.4f} "
          f"delta={settlement_sensitivity['cagr_net_delta']:.4f}")
    _print_benchmark_table(benchmark_section)
    print(f"output dir: {out_dir}")
    print(f"runtime: {runtime:.1f}s")


if __name__ == "__main__":
    _main()
