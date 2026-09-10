"""Retrospective audit of an exact Daily product configuration.

This runner answers a narrow question that the fractional candidate audit cannot:
what happens when the historical research engine receives the same exact-count
portfolio construction contract used by QuantLab Daily?  It intentionally keeps
all frozen backtest, lifecycle, cost, and control semantics unchanged.
"""

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
from quantlab.daily.service import PROJECT_ROOT, SUPPORTED_BOARDS, inspect_data_status
from quantlab.data.security_history import load_security_code_changes
from quantlab.data.storage import ParquetStorage
from quantlab.portfolio.control import build_equal_weight_control_targets
from quantlab.portfolio.product import (
    DAILY_FIXED_COUNT_TIE_POLICY,
    construct_daily_fixed_count_portfolio,
    fixed_count_config_from_daily,
)
from quantlab.research.dataset import build_research_dataset
from quantlab.research.factor_registry import add_transparent_combination, build_factor_columns
from quantlab.research.universe import filter_v1_universe, is_v1_a_share

SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMPARABLE_STATUSES = {"completed", "completed_with_settlement_assumptions"}
_SUPPORTED_SCORES = {
    "return_20d": "lower_is_better",
    "transparent_combo_v1": "higher_is_better",
}


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_configs(audit_path: Path, daily_path: Path) -> tuple[dict, dict]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("schema") != "daily_product_strategy_audit_v1":
        raise ValueError("unsupported product strategy audit schema")
    if audit.get("signal_frequency") != "weekly_first_open_session":
        raise ValueError("v1 product audit freezes weekly_first_open_session cadence")
    recoveries = audit.get("settlement_recovery_assumptions")
    if recoveries != [1.0, 0.0]:
        raise ValueError("v1 product audit requires recovery assumptions [1.0, 0.0]")
    if audit.get("performance_claim") is not False:
        raise ValueError("product strategy audit must keep performance_claim=false")

    daily = json.loads(daily_path.read_text(encoding="utf-8"))
    score = daily.get("score_definition")
    if score not in _SUPPORTED_SCORES:
        raise ValueError(f"unsupported Daily score for product audit: {score!r}")
    if daily.get("score_direction") != _SUPPORTED_SCORES[score]:
        raise ValueError("Daily score direction does not match the registered score")
    if daily.get("tie_policy") != DAILY_FIXED_COUNT_TIE_POLICY:
        raise ValueError("Daily config tie policy does not match exact-count contract")
    fixed_count_config_from_daily(daily)

    boards = daily.get("allowed_boards", list(SUPPORTED_BOARDS))
    if (
        not isinstance(boards, list)
        or not boards
        or len(boards) != len(set(boards))
        or not set(boards).issubset(SUPPORTED_BOARDS)
    ):
        raise ValueError("Daily allowed_boards is not a supported non-empty subset")
    daily = {**daily, "allowed_boards": boards}
    return audit, daily


def _build_signal_frame(
    storage: ParquetStorage,
    dataset: pd.DataFrame,
    signal_dates: list[date],
    board_by_instrument: dict[str, str],
    allowed_boards: frozenset[str],
) -> pd.DataFrame:
    universe = filter_v1_universe(dataset)
    signal = universe[universe["trade_date"].isin(signal_dates)].copy()
    signal["board"] = signal["instrument_id"].map(board_by_instrument)
    signal = signal[signal["board"].isin(allowed_boards)].copy()

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
        signal,
        ["reversal_20d", "low_amplitude", "small_size", "intraday_strength"],
    )


def _product_targets(
    frame: pd.DataFrame,
    daily_config: dict,
    signal_dates: list[date],
) -> dict:
    score = daily_config["score_definition"]
    targets = {}
    for signal_date in signal_dates:
        cross = frame.loc[
            frame["trade_date"].eq(signal_date),
            ["instrument_id", "trade_date", score],
        ].rename(columns={score: "alpha_score"})
        if not cross.empty:
            targets[signal_date] = construct_daily_fixed_count_portfolio(
                cross,
                signal_date,
                daily_config,
            )
    return targets


def _require_comparable(status: str, n_obs: int, expected_n_obs: int, label: str) -> None:
    if status not in _COMPARABLE_STATUSES:
        raise RuntimeError(f"{label} is not comparable: run status {status}")
    if n_obs != expected_n_obs:
        raise RuntimeError(f"{label} attribution coverage mismatch: {n_obs} != {expected_n_obs}")


def run_product_strategy_audit(
    audit_config_path: Path,
    daily_config_path: Path,
    *,
    storage: ParquetStorage | None = None,
    output_root: Path | None = None,
) -> Path:
    """Run the exact Daily portfolio contract against the frozen backtest engine.

    This is retrospective evidence only. It does not promote a strategy and it
    does not claim that a user would actually rebalance every configured signal
    date. The v1 audit intentionally uses the same weekly cadence as the earlier
    candidate portfolio audit so the selection-contract change is isolated.
    """
    audit, daily = _load_configs(audit_config_path, daily_config_path)
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    output_root = output_root or PROJECT_ROOT / "data" / "experiments"
    started = time.perf_counter()

    effective_text = inspect_data_status(storage, datetime.now(SHANGHAI).date())["effective_as_of"]
    if effective_text is None:
        raise ValueError("no complete Daily data date for product strategy audit")
    effective = date.fromisoformat(effective_text)
    period_start = date.fromisoformat(audit["data_start"])
    period_end = date.fromisoformat(audit["data_end"])
    if period_end > effective:
        raise ValueError("product strategy audit end exceeds latest complete data date")
    if period_start > period_end:
        raise ValueError("product strategy audit start exceeds end")

    calendar = storage.load_trading_calendar()
    securities = storage.load_securities()
    board_by_instrument = {item.instrument_id: item.board for item in securities}
    allowed_boards = frozenset(daily["allowed_boards"])
    allowed_master_ids = {
        item.instrument_id for item in securities if item.board in allowed_boards
    }
    open_dates = sorted(
        {
            item.trade_date
            for item in calendar
            if item.is_open and period_start <= item.trade_date <= period_end
        }
    )
    if len(open_dates) < 2:
        raise ValueError("product strategy audit requires at least two open sessions")
    signal_dates = [
        day for day in weekly_signal_dates(open_dates) if period_start <= day <= period_end
    ]

    dataset = build_research_dataset(
        storage,
        period_start,
        period_end,
        return_horizons=(1, 5, 20),
        forward_horizons=(),
    )
    price_frame = dataset[["instrument_id", "trade_date", "adj_close"]].copy()
    signal_frame = _build_signal_frame(
        storage,
        dataset,
        signal_dates,
        board_by_instrument,
        allowed_boards,
    )
    strategy_targets = _product_targets(signal_frame, daily, signal_dates)

    code_changes = load_security_code_changes(PROJECT_ROOT / "config/security_code_changes.csv")
    monitor = LifecycleMonitor(securities, code_changes, mode=LEGACY_DELIST_DATE_INCLUSIVE)
    facts, fact_sha, errors = load_validated_facts(
        PROJECT_ROOT / "config/delisting_facts.json",
        [(item.trade_date, item.is_open) for item in calendar],
    )
    if errors:
        raise ValueError("; ".join(errors))

    def product_universe_predicate(instrument_id: str) -> bool:
        return is_v1_a_share(instrument_id) and instrument_id in allowed_master_ids

    eligibility = pit_eligibility_frame(
        securities,
        code_changes,
        signal_dates,
        LEGACY_DELIST_DATE_INCLUSIVE,
        universe_predicate=product_universe_predicate,
    )
    control_targets = build_equal_weight_control_targets(eligibility, signal_dates)

    scenario_rows = []
    scenarios = {}
    expected_n_obs = len(open_dates) - 1
    for recovery in audit["settlement_recovery_assumptions"]:
        backtest_config = BacktestConfig(
            initial_nav=1.0,
            transaction_cost_bps=float(audit["transaction_cost_bps"]),
            annualization=252,
            delisting_settlement=DelistingSettlementConfig(
                recovery_rate=float(recovery),
                settlement_fee_bps=0.0,
            ),
        )

        def spec(label: str, targets: dict) -> BacktestRunSpec:
            return BacktestRunSpec(
                label=label,
                price_frame=price_frame,
                open_dates=tuple(open_dates),
                targets=targets,
                config=backtest_config,
                execution_lag_sessions=int(audit["execution_lag_sessions"]),
                mode=RUN_MODE_STRICT,
                lifecycle=monitor,
                requested_period_start=period_start,
                requested_period_end=period_end,
                risk_facts=facts,
                risk_policy=EXIT_POLICY_ID,
            )

        scenario_id = f"recovery_assumption_{int(float(recovery))}"
        control_spec = spec(f"{scenario_id}:equal_weight_v1_control", control_targets)
        strategy_spec = spec(f"{scenario_id}:{daily['strategy_id']}", strategy_targets)
        control_result = control_spec.run()
        strategy_result = strategy_spec.run()
        _require_comparable(
            control_result.status,
            len(control_result.records) - 1,
            expected_n_obs,
            f"{scenario_id}:control",
        )
        attribution = compare_benchmark(
            strategy_result.records,
            "equal_weight_v1_control",
            strategy_daily_returns(control_result.records, book="net"),
            book="net",
        )
        _require_comparable(
            strategy_result.status,
            attribution.n_obs,
            expected_n_obs,
            f"{scenario_id}:strategy",
        )
        if strategy_result.valid_through != period_end or control_result.valid_through != period_end:
            raise RuntimeError(f"{scenario_id} did not remain valid through configured period end")
        symmetry = strategy_control_symmetry_audit(strategy_spec, control_spec)
        strategy_metrics = compute_metrics(
            strategy_result.records,
            strategy_result.rebalances,
            backtest_config,
        )
        control_metrics = compute_metrics(
            control_result.records,
            control_result.rebalances,
            backtest_config,
        )
        scenarios[scenario_id] = {
            "settlement_recovery_assumption": recovery,
            "strategy": {
                "status": strategy_result.status,
                "metrics": strategy_metrics,
                "settlement_event_count": len(strategy_result.settlement_events),
                "risk_policy_event_count": len(strategy_result.risk_policy_audit),
            },
            "control": {
                "status": control_result.status,
                "metrics": control_metrics,
                "settlement_event_count": len(control_result.settlement_events),
                "risk_policy_event_count": len(control_result.risk_policy_audit),
            },
            "primary_attribution": asdict(attribution),
            "strategy_control_symmetry": symmetry,
        }
        scenario_rows.append(
            {
                "scenario": scenario_id,
                "strategy_id": daily["strategy_id"],
                "score_definition": daily["score_definition"],
                "target_count": daily["target_count"],
                "max_weight_per_name": daily["max_weight_per_name"],
                "cagr_net": strategy_metrics.get("cagr_net"),
                "active_cagr": attribution.active_cagr,
                "sharpe_net": strategy_metrics.get("sharpe_net"),
                "max_drawdown_net": strategy_metrics.get("max_drawdown_net"),
                "active_max_drawdown": attribution.active_max_drawdown,
                "annualized_turnover": strategy_metrics.get("annualized_turnover"),
                "cagr_cost_drag": strategy_metrics.get("cagr_cost_drag"),
                "information_ratio": attribution.information_ratio,
                "beta": attribution.beta,
                "coverage": attribution.n_obs / expected_n_obs,
            }
        )

    run_id = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    out_dir = output_root / audit["experiment_id"] / daily["config_id"] / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    pd.DataFrame(scenario_rows).to_csv(out_dir / "comparison.csv", index=False)
    summary = {
        "schema": "daily_product_strategy_audit_v1",
        "run_id": run_id,
        "code_head": _head(),
        "audit_config_sha256": _sha256(audit_config_path),
        "daily_config_sha256": _sha256(daily_config_path),
        "period": [period_start.isoformat(), period_end.isoformat()],
        "history_status": audit["history_status"],
        "signal_frequency": audit["signal_frequency"],
        "execution_timing": "validated_T_close_signal_then_T_plus_1_close_engine",
        "portfolio_contract": {
            "constructor": "fixed_count_v1",
            "target_count": daily["target_count"],
            "score_direction": daily["score_direction"],
            "gross_exposure": daily["gross_exposure"],
            "max_weight_per_name": daily["max_weight_per_name"],
            "tie_policy": daily["tie_policy"],
            "allowed_boards": daily["allowed_boards"],
        },
        "strategy": {
            "strategy_id": daily["strategy_id"],
            "model_status": daily["model_status"],
            "score_definition": daily["score_definition"],
        },
        "evidence_applicability": {
            "portfolio_construction": "exact_Daily_config_contract",
            "signal_cadence": (
                "weekly research audit; isolates exact-count portfolio semantics and does not "
                "claim the user rebalances every Daily report"
            ),
            "fresh_oos": False,
        },
        "scenarios": scenarios,
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
