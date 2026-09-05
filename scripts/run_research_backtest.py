#!/usr/bin/env python3
"""Run the lifecycle date-semantics comparison experiment.

Runs baseline and admission paths under both the legacy delist_date boundary
(``legacy_delist_date_inclusive``) and the candidate ``delist_date_is_first_invalid_v1``
interpretation, each in strict and diagnostic mode, with full provenance.

Additionally runs the delisting settlement policy v0 on the same legacy
baseline configuration: an explicit settlement assumption at ``recovery_rate``
1.0 and a zero-recovery sensitivity bound at 0.0. The recovery-1.0 path is the
first performance-valid strict full-period baseline; the recovery-0.0 path
brackets the assumption from below. Settlement is an explicit accounting
assumption, not a verified delisting fact.

The two settlement bounds are attributed against four benchmarks (000300.SH,
000905.SH, 000852.SH index daily closes and a V1-universe equal-weight
benchmark computed from canonical data) via a strict date inner join. The
legacy blocked strict path carries a footnote only (no benchmark attribution
for a blocked path). Risk-free rate stays at 0.0.

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
    equal_weight_returns_from_frame,
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
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCHEMA_VERSION = "v0.3.0"
EXPERIMENT_SCHEMA = "lifecycle_date_semantics_v0_1_1"

PERIOD_START = date(2020, 1, 1)
PERIOD_END = date(2024, 12, 31)
LOOKBACK = 20
INDEX_BENCHMARKS = ("000300.SH", "000905.SH", "000852.SH")
EQUAL_WEIGHT_BENCHMARK = "equal_weight_v1"
BENCHMARK_ORDER = (*INDEX_BENCHMARKS, EQUAL_WEIGHT_BENCHMARK)


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


def _index_benchmark_returns(
    storage: ParquetStorage, coverage_dates: list[date], instrument_id: str
) -> dict[date, float]:
    """Load one index's per-session returns from canonical index_daily.

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
    closes: list[tuple[date, float]] = []
    for d in sorted(coverage_dates):
        for bar in storage.load_index_daily_by_date(d):
            if bar.instrument_id == instrument_id:
                closes.append((bar.trade_date, bar.close))
    if not closes:
        raise RuntimeError(
            f"no index_daily rows found for {instrument_id} "
            f"({coverage_dates[0]} .. {coverage_dates[-1]})"
        )
    return returns_from_closes(closes)


def _build_benchmark_returns(
    storage: ParquetStorage, coverage_dates: list[date], universe: pd.DataFrame
) -> dict[str, dict[date, float]]:
    """All benchmark return series keyed by benchmark name (strict PIT)."""
    benchmarks: dict[str, dict[date, float]] = {}
    for instrument_id in INDEX_BENCHMARKS:
        benchmarks[instrument_id] = _index_benchmark_returns(
            storage, coverage_dates, instrument_id
        )
    benchmarks[EQUAL_WEIGHT_BENCHMARK] = equal_weight_returns_from_frame(
        universe[["instrument_id", "trade_date", "adj_close"]]
    )
    return benchmarks


def _benchmark_comparison_section(
    settlement_strict,
    settlement_zero_strict,
    legacy_strict,
    benchmark_returns: dict[str, dict[date, float]],
    annualization: int,
) -> dict:
    """2 bounds x 4 benchmarks attribution for the primary valid result.

    The settlement bounds are the primary performance-valid strict baselines;
    the legacy strict path is blocked and only carries a footnote (benchmark
    attribution for a blocked path would be meaningless beyond its
    ``valid_through`` prefix).
    """
    comparisons: dict[str, dict] = {}
    for bound_key, result in (
        ("recovery_1", settlement_strict),
        ("recovery_0", settlement_zero_strict),
    ):
        rows = {}
        for name in BENCHMARK_ORDER:
            stats = compare_benchmark(
                result.records, name, benchmark_returns[name],
                annualization=annualization, book="net",
            )
            rows[name] = stats.to_dict()
        comparisons[bound_key] = rows

    equal_weight_v1_note = (
        "cross-sectional equal-weight daily returns of the V1 universe from "
        "canonical adjusted closes; rows are restricted to each instrument's "
        "[list_date, delist_date] window so delisted names stop contributing "
        "after their last available bar (no fill); a suspended name "
        "accumulates its return into the next available session"
    )

    def _beta_vs_equal_weight(rows: dict) -> float:
        return rows[EQUAL_WEIGHT_BENCHMARK]["beta"]

    return {
        "note": (
            "attribution of the primary valid strict result (delisting "
            "settlement bounds) against 4 benchmarks; strict inner join on "
            "actual record dates (missing benchmark dates are dropped, never "
            "filled); risk-free rate = 0.0; alpha is daily OLS, "
            "alpha_annualized = alpha_daily * annualization (arithmetic)"
        ),
        "risk_free_rate": 0.0,
        "annualization": annualization,
        "book": "net",
        "benchmark_definitions": {
            "000300.SH": "tushare index_daily, canonical index_daily partition",
            "000905.SH": "tushare index_daily, canonical index_daily partition",
            "000852.SH": "tushare index_daily, canonical index_daily partition",
            EQUAL_WEIGHT_BENCHMARK: equal_weight_v1_note,
        },
        "primary_valid_result": {
            "policy": "delisting_settlement_v0 (recovery bounds 1.0 / 0.0)",
            **comparisons,
            "beta_vs_equal_weight_v1": {
                "recovery_1": _beta_vs_equal_weight(comparisons["recovery_1"]),
                "recovery_0": _beta_vs_equal_weight(comparisons["recovery_0"]),
                "interpretation_note": (
                    "the strategy is a ~20% cross-sectional slice of the same "
                    "V1 universe in which equal_weight_v1 invests fully, so "
                    "beta against equal_weight_v1 is expected to be close to 1"
                ),
            },
        },
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
    }


def _print_benchmark_table(section: dict) -> None:
    """Human-readable 2x4 comparison table."""
    print("benchmark comparison (net book):")
    primary = section["primary_valid_result"]
    header = (
        f"  {'benchmark':<16}{'n_obs':>7}{'beta':>8}{'alpha_ann':>11}"
        f"{'TE':>8}{'IR':>8}{'act_CAGR':>10}{'cum_act':>10}"
    )
    for bound_key in ("recovery_1", "recovery_0"):
        print(f"  [{bound_key}]")
        print(header)
        for name in BENCHMARK_ORDER:
            row = primary[bound_key][name]
            print(
                f"  {name:<16}{row['n_obs']:>7}{row['beta']:>8.3f}"
                f"{row['alpha_annualized']:>11.4f}{row['tracking_error']:>8.4f}"
                f"{row['information_ratio']:>8.3f}{row['active_cagr']:>10.4f}"
                f"{row['cumulative_active_return']:>10.4f}"
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
    # legacy baseline configuration. Inside the engine, trusted-fact forced
    # exits keep priority; settlement only resolves residual unknown
    # lifecycle invalidations without a valid exit price.
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

    # benchmark attribution for the primary valid strict result (the two
    # settlement bounds); padded_dates covers the first record's prior close
    benchmark_returns = _build_benchmark_returns(storage, padded_dates, universe)
    benchmark_section = _benchmark_comparison_section(
        settlement_strict, settlement_zero_strict, strict_result, benchmark_returns,
        bt_config.annualization,
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
        settlement_strict, None, reproducible, settlement_config,
        expected_sessions=bt_open_dates,
    )
    bound_full = _settlement_bound(settlement_strict, settlement_config)
    bound_zero = _settlement_bound(settlement_zero_strict, settlement_zero_config)
    settlement_sensitivity = {
        "note": (
            "same strict configuration and targets; only recovery_rate varies "
            "between the explicit settlement bounds (1.0 = settle at the last "
            "available mark, 0.0 = settle at zero). The bounds bracket the "
            "assumption; they are not recovery expectations and the gap "
            "measures how much conclusions depend on the settlement rate."
        ),
        "recovery_1": bound_full,
        "recovery_0": bound_zero,
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
            {e.instrument_id for e in settlement_strict.settlement_events}
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
                    "opt-in; only the strict_settlement paths use it, all "
                    "comparison paths keep legacy blocking semantics"
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
            "recovery_rate": settlement_config.delisting_settlement.recovery_rate,
            "settlement_fee_bps": (
                settlement_config.delisting_settlement.settlement_fee_bps
            ),
            "lifecycle_mode": LEGACY_DELIST_DATE_INCLUSIVE,
            "status": settlement_strict.status,
            "performance_valid": settlement_report["performance_valid"],
            "invalid_reasons": settlement_report["invalid_reasons"],
            "n_records": len(settlement_strict.records),
            "valid_through": (
                settlement_strict.valid_through.isoformat()
                if settlement_strict.valid_through else None
            ),
            "solver_root_residual": settlement_strict.solver_root_residual,
            "accounting_checks": [
                c.__dict__ for c in settlement_strict.accounting_checks
            ],
            "accounting_error": settlement_strict.accounting_error,
            "first_blocking_event": (
                settlement_strict.first_blocking_event.__dict__
                if settlement_strict.first_blocking_event else None
            ),
            "settlement_event_count": len(settlement_strict.settlement_events),
            "metrics": settlement_report["metrics"],
            "settlement_disclosure": settlement_report["settlement_disclosure"],
            "statistics": _statistics(settlement_strict),
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
    export_group(out_dir, "strict_settlement_recovery_1", settlement_strict)
    export_group(out_dir, "strict_settlement_recovery_0", settlement_zero_strict)

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
    print(f"settlement strict status: {settlement_strict.status} "
          f"performance_valid={settlement_report['performance_valid']} "
          f"settlements={len(settlement_strict.settlement_events)}")
    if settlement_report["metrics"] is not None:
        sm = settlement_report["metrics"]
        print(f"settlement baseline: total_return_net={sm['total_return_net']:.4f} "
              f"cagr_net={sm['cagr_net']:.4f} sharpe_net={sm['sharpe_net']:.4f} "
              f"max_drawdown_net={sm['max_drawdown_net']:.4f}")
    print(f"settlement bounds cagr_net: recovery_1={bound_full['cagr_net']:.4f} "
          f"recovery_0={bound_zero['cagr_net']:.4f} "
          f"delta={settlement_sensitivity['cagr_net_delta']:.4f}")
    _print_benchmark_table(benchmark_section)
    print(f"output dir: {out_dir}")
    print(f"runtime: {runtime:.1f}s")


if __name__ == "__main__":
    _main()
