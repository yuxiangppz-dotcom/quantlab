"""Bounded portfolio translation audit over the frozen research engine."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.backtest import (
    EXIT_POLICY_ID,
    LEGACY_DELIST_DATE_INCLUSIVE,
    RUN_MODE_STRICT,
    BacktestConfig,
    BacktestRunSpec,
    DelistingSettlementConfig,
    LifecycleMonitor,
    compare_benchmark,
    compute_metrics,
    pit_eligibility_frame,
    strategy_control_symmetry_audit,
    strategy_daily_returns,
    weekly_signal_dates,
)
from quantlab.backtest.delisting_facts import load_validated_facts
from quantlab.daily.service import PROJECT_ROOT, inspect_data_status
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.portfolio import RankPortfolioConfig, construct_rank_portfolio
from quantlab.portfolio.control import build_equal_weight_control_targets
from quantlab.research.dataset import build_research_dataset
from quantlab.research.factor_registry import add_transparent_combination, build_factor_columns
from quantlab.research.universe import filter_v1_universe, is_v1_a_share

SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMPARABLE_STATUSES = {"completed", "completed_with_settlement_assumptions"}


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _build_signal_frame(
    storage: ParquetStorage, dataset: pd.DataFrame, signal_dates: list[date]
) -> pd.DataFrame:
    universe = filter_v1_universe(dataset)
    signal = universe[universe["trade_date"].isin(signal_dates)].copy()
    raw_rows = []
    basic_rows = []
    for signal_date in signal_dates:
        raw_rows.extend(asdict(item) for item in storage.load_daily_bars_by_date(signal_date))
        basic_rows.extend(asdict(item) for item in storage.load_daily_basic_by_date(signal_date))
    raw = pd.DataFrame(raw_rows)
    basic = pd.DataFrame(basic_rows)
    signal = signal.merge(
        raw[["instrument_id", "trade_date", "open", "high", "low", "amount"]],
        on=["instrument_id", "trade_date"],
        validate="one_to_one",
    )
    signal = signal.merge(
        basic[["instrument_id", "trade_date", "turnover_rate", "total_mv", "circ_mv"]],
        on=["instrument_id", "trade_date"],
        validate="one_to_one",
    )
    signal = build_factor_columns(signal)
    return add_transparent_combination(
        signal, ["reversal_20d", "low_amplitude", "small_size", "intraday_strength"]
    )


def _targets(frame: pd.DataFrame, candidate: str, signal_dates: list[date]) -> dict:
    config = RankPortfolioConfig(0.2, "higher_is_better", 1.0, None)
    targets = {}
    for signal_date in signal_dates:
        cross = frame.loc[
            frame["trade_date"].eq(signal_date),
            ["instrument_id", "trade_date", candidate],
        ].rename(columns={candidate: "alpha_score"})
        if not cross.empty:
            targets[signal_date] = construct_rank_portfolio(cross, signal_date, config)
    return targets


def _require_comparable(status: str, n_obs: int, expected_n_obs: int, label: str) -> None:
    if status not in _COMPARABLE_STATUSES:
        raise RuntimeError(f"{label} is not comparable: run status {status}")
    if n_obs != expected_n_obs:
        raise RuntimeError(f"{label} attribution coverage mismatch: {n_obs} != {expected_n_obs}")


def run_portfolio_translation_audit(
    config_path: Path,
    *,
    storage: ParquetStorage | None = None,
    output_root: Path | None = None,
) -> Path:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = {
        "momentum_1d",
        "intraday_strength",
        "low_amplitude",
        "float_ratio",
        "transparent_combo_v1",
        "reversal_20d",
    }
    if set(config["candidates"]) != allowed or len(config["candidates"]) != len(allowed):
        raise ValueError("portfolio translation candidate set must remain the frozen six")
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    output_root = output_root or PROJECT_ROOT / "data" / "experiments"
    started = time.perf_counter()
    effective_text = inspect_data_status(storage, datetime.now(SHANGHAI).date())["effective_as_of"]
    if effective_text is None:
        raise ValueError("no complete daily data date for portfolio audit")
    period_start = date.fromisoformat(config["data_start"])
    effective = date.fromisoformat(effective_text)
    period_end = date.fromisoformat(config["data_end"])
    if period_end > effective:
        raise ValueError("portfolio audit end exceeds latest complete data date")
    calendar = storage.load_trading_calendar()
    securities = storage.load_securities()
    open_dates = sorted(
        {
            item.trade_date
            for item in calendar
            if item.is_open and period_start <= item.trade_date <= period_end
        }
    )
    signal_dates = [
        item for item in weekly_signal_dates(open_dates) if period_start <= item <= period_end
    ]
    dataset = build_research_dataset(
        storage, period_start, period_end, return_horizons=(1, 5, 20), forward_horizons=()
    )
    price_frame = dataset[["instrument_id", "trade_date", "adj_close"]].copy()
    signal_frame = _build_signal_frame(storage, dataset, signal_dates)
    code_changes = load_security_code_changes(PROJECT_ROOT / "config/security_code_changes.csv")
    monitor = LifecycleMonitor(securities, code_changes, mode=LEGACY_DELIST_DATE_INCLUSIVE)
    facts, fact_sha, errors = load_validated_facts(
        PROJECT_ROOT / "config/delisting_facts.json",
        [(item.trade_date, item.is_open) for item in calendar],
    )
    if errors:
        raise ValueError("; ".join(errors))
    backtest_config = BacktestConfig(
        initial_nav=1.0,
        transaction_cost_bps=float(config["transaction_cost_bps"]),
        annualization=252,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=float(config["settlement_recovery_assumption"]),
            settlement_fee_bps=0.0,
        ),
    )
    eligibility = pit_eligibility_frame(
        securities,
        code_changes,
        signal_dates,
        LEGACY_DELIST_DATE_INCLUSIVE,
        universe_predicate=is_v1_a_share,
    )
    control_targets = build_equal_weight_control_targets(eligibility, signal_dates)

    def spec(label: str, targets: dict) -> BacktestRunSpec:
        return BacktestRunSpec(
            label=label,
            price_frame=price_frame,
            open_dates=tuple(open_dates),
            targets=targets,
            config=backtest_config,
            execution_lag_sessions=int(config["execution_lag_sessions"]),
            mode=RUN_MODE_STRICT,
            lifecycle=monitor,
            requested_period_start=period_start,
            requested_period_end=period_end,
            risk_facts=facts,
            risk_policy=EXIT_POLICY_ID,
        )

    control_spec = spec("equal_weight_v1_control", control_targets)
    control_result = control_spec.run()
    _require_comparable(
        control_result.status,
        len(control_result.records) - 1,
        len(open_dates) - 1,
        "equal_weight_v1_control",
    )
    if control_result.valid_through != period_end:
        raise RuntimeError(
            "equal_weight_v1_control did not remain valid through the configured period end"
        )
    control_returns = strategy_daily_returns(control_result.records, book="net")
    control_metrics = compute_metrics(
        control_result.records, control_result.rebalances, backtest_config
    )
    rows = []
    details = {}
    for candidate in config["candidates"]:
        candidate_targets = _targets(signal_frame, candidate, signal_dates)
        candidate_spec = spec(candidate, candidate_targets)
        result = candidate_spec.run()
        metrics = compute_metrics(result.records, result.rebalances, backtest_config)
        attribution = compare_benchmark(
            result.records, "equal_weight_v1_control", control_returns, book="net"
        )
        _require_comparable(result.status, attribution.n_obs, len(result.records) - 1, candidate)
        symmetry = strategy_control_symmetry_audit(candidate_spec, control_spec)
        details[candidate] = {
            "run_status": result.status,
            "metrics": metrics,
            "primary_attribution": asdict(attribution),
            "strategy_control_symmetry": symmetry,
            "settlement_event_count": len(result.settlement_events),
            "risk_policy_event_count": len(result.risk_policy_audit),
        }
        rows.append(
            {
                "candidate": candidate,
                "status": result.status,
                "cagr_net": metrics.get("cagr_net"),
                "active_cagr": attribution.active_cagr,
                "sharpe_net": metrics.get("sharpe_net"),
                "max_drawdown_net": metrics.get("max_drawdown_net"),
                "active_max_drawdown": attribution.active_max_drawdown,
                "annualized_turnover": metrics.get("annualized_turnover"),
                "cagr_cost_drag": metrics.get("cagr_cost_drag"),
                "information_ratio": attribution.information_ratio,
                "beta": attribution.beta,
                "coverage": attribution.n_obs / max(1, len(result.records) - 1),
            }
        )

    run_id = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    out_dir = output_root / config["experiment_id"] / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(rows).to_csv(out_dir / "portfolio_comparison.csv", index=False)
    summary = {
        "schema": "portfolio_translation_audit_v1_1",
        "run_id": run_id,
        "code_head": _head(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "period": [period_start.isoformat(), period_end.isoformat()],
        "history_status": (
            "retrospective_discovery_validation; 2025-2026 history was previously observed; "
            "not fresh OOS"
        ),
        "signal_frequency": config["signal_frequency"],
        "execution_timing": "validated_T_close_signal_then_T_plus_1_close_engine",
        "control": {"status": control_result.status, "metrics": control_metrics},
        "candidates": details,
        "delisting_fact_sha256": fact_sha,
        "performance_claim": False,
        "strategy_promoted": False,
        "runtime_seconds": time.perf_counter() - started,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return out_dir
