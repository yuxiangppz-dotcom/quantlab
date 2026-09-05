#!/usr/bin/env python3
"""Run the frozen-boundary Lifecycle Risk Policy v0 engineering comparison."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

from quantlab.alpha import calculate_momentum_alpha
from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    MISSING_PRICE_POLICY,
    RUN_MODE_DIAGNOSTIC,
    RUN_MODE_STRICT,
    BacktestConfig,
    LifecycleMonitor,
    risk_policy_statistics,
    run_backtest,
    weekly_signal_dates,
)
from quantlab.backtest.admission import (
    ENFORCEMENT_VERSION,
    POLICY_NAME,
    POLICY_VERSION,
    compute_restricted_by_signal,
)
from quantlab.backtest.audit import fingerprint_frame, fingerprint_targets
from quantlab.backtest.delisting_facts import (
    load_validated_facts,
    source_coverage,
)
from quantlab.backtest.experiment import build_group_report, export_group
from quantlab.backtest.provenance import content_manifest, environment_info
from quantlab.data import ParquetStorage
from quantlab.data.security_history import load_security_code_changes
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.research import build_research_dataset, filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINE_SCHEMA_VERSION = "v0.3.0"
EXPERIMENT_SCHEMA = "lifecycle_risk_policy_v0_1"
LIFECYCLE_MODE_STATUS = "candidate_interpretation_not_canonical_universal_truth"
FACTS_PATH = PROJECT_ROOT / "config" / "delisting_facts_v2.json"

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
        output = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True
        )
        return bool(output.strip())
    except Exception:
        return True


def _code_paths() -> list[Path]:
    paths = sorted((PROJECT_ROOT / "src" / "quantlab").rglob("*.py"))
    paths.extend(
        [
            PROJECT_ROOT / "scripts" / "run_lifecycle_risk_policy.py",
            PROJECT_ROOT / "pyproject.toml",
            PROJECT_ROOT / "uv.lock",
            PROJECT_ROOT / "config" / "security_code_changes.csv",
        ]
    )
    return paths


def _data_paths(storage: ParquetStorage, padded_dates: list[date]) -> list[Path]:
    paths = [storage.securities_path, storage.calendar_path, FACTS_PATH]
    for session in padded_dates:
        paths.append(storage.daily_bars_path(session))
        paths.append(storage.adj_factor_path(session))
    return paths


def _build_targets(storage: ParquetStorage, signal_dates: list[date]):
    research = build_research_dataset(
        storage,
        PERIOD_START,
        PERIOD_END,
        return_horizons=(LOOKBACK,),
        forward_horizons=(),
    )
    price_frame = research[["instrument_id", "trade_date", "adj_close"]]
    alpha = calculate_momentum_alpha(filter_v1_universe(research), lookback=LOOKBACK)
    portfolio_config = RankPortfolioConfig(
        selection_fraction=0.20,
        score_direction="lower_is_better",
        gross_exposure=1.0,
        max_weight_per_name=None,
    )
    targets = {}
    for signal_date in signal_dates:
        cross_section = alpha[alpha["trade_date"] == signal_date]
        if not cross_section.empty:
            targets[signal_date] = construct_rank_portfolio(
                cross_section, signal_date, portfolio_config
            )
    return price_frame, targets


def _path_summary(strict, diagnostic, config, sessions, reproducible) -> dict:
    return build_group_report(
        strict,
        diagnostic,
        config,
        sessions,
        reproducible,
        lifecycle_mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )


def _first_forced_exit(records) -> dict | None:
    candidates = [r for r in records if r.forced_sell_value > 0]
    if not candidates:
        return None
    first_date, instrument_id = min(
        (r.decision_date, r.instrument_id) for r in candidates
    )
    rows = {
        r.book: r
        for r in candidates
        if r.decision_date == first_date and r.instrument_id == instrument_id
    }
    gross = rows.get("gross")
    net = rows.get("net")
    reference = net or gross
    return {
        "instrument_id": instrument_id,
        "fact_id": reference.fact_id,
        "fact_available_from": reference.available_from.isoformat(),
        "exit_session": first_date.isoformat(),
        "execution_price": reference.execution_price,
        "gross_sell_value": gross.forced_sell_value if gross else None,
        "net_sell_value": net.forced_sell_value if net else None,
        "net_fee": net.fee if net else None,
    }


def _unexited_before_invalidation(records, facts: dict) -> list[dict]:
    rows = {}
    for record in records:
        if record.risk_state != "blocked_before_exit":
            continue
        key = (record.instrument_id, record.decision_date)
        rows.setdefault(
            key,
            {
                "instrument_id": record.instrument_id,
                "fact_id": record.fact_id,
                "fact_coverage_status": source_coverage(facts, record.instrument_id),
                "blocking_session": record.decision_date.isoformat(),
            },
        )
    return list(rows.values())


def _blocking_event_summary(result, facts: dict) -> dict | None:
    event = result.first_blocking_event
    if event is None:
        return None
    entry = facts.get(event.instrument_id, {})
    return {
        "instrument_id": event.instrument_id,
        "event_type": event.event_type,
        "event_date": event.event_date.isoformat(),
        "blocking_session": event.blocking_session.isoformat(),
        "valid_through": result.valid_through.isoformat()
        if result.valid_through
        else None,
        "fact_coverage_status": source_coverage(facts, event.instrument_id),
        "retrieval_status": entry.get("retrieval_status", "not_present"),
    }


def main() -> None:
    storage = ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    calendar = storage.load_trading_calendar()
    securities = storage.load_securities()
    if not calendar or not securities:
        raise RuntimeError("canonical calendar and securities are required")
    code_changes = load_security_code_changes(
        PROJECT_ROOT / "config" / "security_code_changes.csv"
    )
    open_dates = sorted({row.trade_date for row in calendar if row.is_open})
    sessions = [d for d in open_dates if PERIOD_START <= d <= PERIOD_END]
    if not sessions:
        raise RuntimeError("no open sessions in requested period")
    signal_dates = [
        d
        for d in weekly_signal_dates(open_dates)
        if PERIOD_START <= d <= PERIOD_END
    ]
    first_index = open_dates.index(sessions[0])
    last_index = open_dates.index(sessions[-1])
    padded_dates = open_dates[max(0, first_index - LOOKBACK) : last_index + 1]

    git_sha = _git_sha()
    workspace_dirty = _git_dirty()
    provenance_before = {
        "code": content_manifest(_code_paths(), PROJECT_ROOT),
        "data": content_manifest(_data_paths(storage, padded_dates), PROJECT_ROOT),
    }
    started = time.perf_counter()

    price_frame, targets = _build_targets(storage, signal_dates)
    facts, fact_fingerprint, fact_errors = load_validated_facts(
        FACTS_PATH, [(row.trade_date, row.is_open) for row in calendar]
    )
    if fact_errors:
        raise ValueError("; ".join(fact_errors))
    restricted_by_signal = compute_restricted_by_signal(targets, facts)
    config = BacktestConfig(
        initial_nav=1.0,
        transaction_cost_bps=10.0,
        annualization=252,
    )
    monitor = LifecycleMonitor(
        securities,
        code_changes,
        mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )
    common = {
        "price_frame": price_frame,
        "open_dates": sessions,
        "targets": targets,
        "config": config,
        "execution_lag_sessions": 1,
        "lifecycle": monitor,
        "requested_period_start": PERIOD_START,
        "requested_period_end": PERIOD_END,
    }
    control_strict = run_backtest(
        **common,
        mode=RUN_MODE_STRICT,
        restricted_by_signal=restricted_by_signal,
    )
    control_diagnostic = run_backtest(
        **common,
        mode=RUN_MODE_DIAGNOSTIC,
        restricted_by_signal=restricted_by_signal,
    )
    new_strict = run_backtest(
        **common,
        mode=RUN_MODE_STRICT,
        risk_facts=facts,
        risk_policy=EXIT_POLICY_ID,
    )
    new_diagnostic = run_backtest(
        **common,
        mode=RUN_MODE_DIAGNOSTIC,
        risk_facts=facts,
        risk_policy=EXIT_POLICY_ID,
    )

    provenance_after = {
        "code": content_manifest(_code_paths(), PROJECT_ROOT),
        "data": content_manifest(_data_paths(storage, padded_dates), PROJECT_ROOT),
    }
    reproducible = provenance_before == provenance_after
    data_fingerprint = fingerprint_frame(price_frame)
    target_fingerprint = fingerprint_targets(targets)
    strategy_config = {
        "period": [PERIOD_START.isoformat(), PERIOD_END.isoformat()],
        "signal": "return_20d",
        "score_direction": "lower_is_better",
        "universe": "v1_sh_sz_a_share",
        "selection_fraction": 0.20,
        "long_only": True,
        "weighting": "equal_weight",
        "target_stock_exposure": 1.0,
        "rebalance_schedule": "weekly_last_open_session",
        "execution": "signal_T_to_next_market_session_close",
        "transaction_cost_bps": 10.0,
        "initial_nav": 1.0,
        "annualization": 252,
        "cash_return": 0.0,
        "missing_price_policy": MISSING_PRICE_POLICY,
    }
    new_strict_stats = risk_policy_statistics(new_strict.risk_policy_audit)
    new_diagnostic_stats = risk_policy_statistics(new_diagnostic.risk_policy_audit)

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    out_dir = PROJECT_ROOT / "data" / "experiments" / EXPERIMENT_SCHEMA / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    summary = {
        "engine_schema_version": ENGINE_SCHEMA_VERSION,
        "experiment_schema": EXPERIMENT_SCHEMA,
        "analysis_type": "lifecycle_risk_policy_engineering_comparison",
        "run_id": run_id,
        "run_time": datetime.now().isoformat(),
        "code_sha": git_sha,
        "workspace_dirty": workspace_dirty,
        "environment": environment_info(),
        "requested_period": {
            "start": PERIOD_START.isoformat(),
            "end": PERIOD_END.isoformat(),
        },
        "simulated_period": {
            "start": sessions[0].isoformat(),
            "end": sessions[-1].isoformat(),
        },
        "lifecycle_mode": DELIST_DATE_IS_FIRST_INVALID_V1,
        "lifecycle_mode_status": LIFECYCLE_MODE_STATUS,
        "risk_policy": EXIT_POLICY_ID,
        "control_policy": f"{POLICY_NAME}_{POLICY_VERSION}",
        "control_enforcement_version": ENFORCEMENT_VERSION,
        "fact_snapshot_path": str(FACTS_PATH.relative_to(PROJECT_ROOT)),
        "fact_snapshot_fingerprint": fact_fingerprint,
        "data_fingerprint": data_fingerprint,
        "target_fingerprint": target_fingerprint,
        "strategy_config": strategy_config,
        "performance_claim": False,
        "test_observed": True,
        "reproducible": reproducible,
        "provenance": provenance_before,
        "control": _path_summary(
            control_strict, control_diagnostic, config, sessions, reproducible
        ),
        "new_policy": _path_summary(
            new_strict, new_diagnostic, config, sessions, reproducible
        ),
        "first_blocking_event_fact_coverage": {
            "control": _blocking_event_summary(control_strict, facts),
            "new_policy": _blocking_event_summary(new_strict, facts),
        },
        "risk_policy_statistics": {
            "strict": new_strict_stats,
            "diagnostic": new_diagnostic_stats,
        },
        "first_forced_exit": _first_forced_exit(new_diagnostic.risk_policy_audit),
        "trusted_fact_unexited_before_invalidation": _unexited_before_invalidation(
            new_diagnostic.risk_policy_audit, facts
        ),
        "total_runtime_seconds": time.perf_counter() - started,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "manifest.json").write_text(
        json.dumps(provenance_before, indent=2, default=str), encoding="utf-8"
    )
    export_group(out_dir, "control_strict", control_strict)
    export_group(out_dir, "control_diagnostic", control_diagnostic)
    export_group(out_dir, "new_policy_strict", new_strict)
    export_group(out_dir, "new_policy_diagnostic", new_diagnostic)

    print(f"=== {EXPERIMENT_SCHEMA} ===")
    print(
        f"control strict: {control_strict.status} "
        f"valid_through={control_strict.valid_through}"
    )
    print(
        f"new strict: {new_strict.status} "
        f"valid_through={new_strict.valid_through}"
    )
    print(f"new diagnostic risk stats: {new_diagnostic_stats}")
    print(f"first forced exit: {summary['first_forced_exit']}")
    print(f"unexited before invalidation: {summary['trusted_fact_unexited_before_invalidation']}")
    print(f"output dir: {out_dir}")


if __name__ == "__main__":
    main()
