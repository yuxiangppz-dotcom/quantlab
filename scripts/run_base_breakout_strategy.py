#!/usr/bin/env python3
"""Run the complete price/volume base-breakout research strategy locally."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from quantlab.data import ParquetStorage
from quantlab.data.models import DataValidationError
from quantlab.research.base_breakout_strategy import (
    FEATURE_HISTORY_SESSIONS,
    STRATEGY_ID,
    BaseBreakoutStrategyConfig,
    build_base_breakout_signals,
    summarize_base_breakout_signals,
)
from quantlab.research.dataset import build_research_dataset
from quantlab.research.s5_base_completion import S5BaseConfig
from quantlab.research.universe import filter_v1_universe

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate causal base-breakout candidates and close-return diagnostics."
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "base_breakout_strategy.json",
    )
    parser.add_argument(
        "--storage",
        type=Path,
        default=PROJECT_ROOT / "data" / "canonical",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "experiments" / STRATEGY_ID,
    )
    args = parser.parse_args()
    output = run_strategy(
        start=args.start,
        end=args.end,
        config_path=args.config,
        storage=ParquetStorage(args.storage),
        output_root=args.output_root,
    )
    print(f"output: {output}")


def run_strategy(
    *,
    start: date,
    end: date,
    config_path: Path,
    storage: ParquetStorage,
    output_root: Path,
) -> Path:
    if start > end:
        raise ValueError("start cannot be after end")
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    config = _parse_config(raw_config)
    calendar = storage.load_trading_calendar()
    open_dates = sorted({item.trade_date for item in calendar if item.is_open})
    in_range = [value for value in open_dates if start <= value <= end]
    if not in_range:
        raise DataValidationError("requested period contains no market sessions")
    first_index = open_dates.index(in_range[0])
    if first_index + 1 < FEATURE_HISTORY_SESSIONS:
        raise DataValidationError("insufficient 120-session history before start")
    padded_start = open_dates[first_index - FEATURE_HISTORY_SESSIONS + 1]
    cadence = int(raw_config["signal_interval_sessions"])
    if cadence <= 0:
        raise ValueError("signal_interval_sessions must be positive")
    signal_dates = tuple(in_range[::cadence])

    dataset = filter_v1_universe(
        build_research_dataset(
            storage,
            padded_start,
            in_range[-1],
            return_horizons=(),
            forward_horizons=config.evaluation_horizons,
        )
    )
    volume_rows = []
    last_index = open_dates.index(in_range[-1])
    for trade_date in open_dates[open_dates.index(padded_start) : last_index + 1]:
        volume_rows.extend(
            {
                "instrument_id": item.instrument_id,
                "trade_date": item.trade_date,
                "volume": item.volume,
            }
            for item in storage.load_daily_bars_by_date(trade_date)
        )
    volumes = pd.DataFrame(volume_rows)
    strategy_input = dataset.merge(
        volumes,
        on=["instrument_id", "trade_date"],
        how="left",
        validate="one_to_one",
    )
    sessions = tuple(value for value in open_dates if padded_start <= value <= in_range[-1])
    signals = build_base_breakout_signals(
        strategy_input,
        sessions,
        signal_dates=signal_dates,
        config=config,
    )
    diagnostics = summarize_base_breakout_signals(
        signals,
        dataset,
        config.evaluation_horizons,
    )

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    output = output_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    signals.to_csv(output / "signals.csv", index=False)
    pd.DataFrame(diagnostics).to_csv(output / "diagnostics.csv", index=False)
    summary = {
        "schema": "quantlab_base_breakout_strategy_run_v1",
        "strategy_id": STRATEGY_ID,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "signal_dates": len(signal_dates),
        "signal_rows": len(signals),
        "selected_rows": int(signals["selected"].sum()),
        "signal_interval_sessions": cadence,
        "signal_interval_note": (
            "controls signal cadence; it is not a forced holding or lock-up period"
        ),
        "holding_policy_enforced": False,
        "evaluation_horizons": list(config.evaluation_horizons),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "input_fingerprint": _frame_fingerprint(strategy_input),
        "git_head": _git_head(),
        "research_only": True,
        "test_observed": True,
        "executable_pnl": False,
        "performance_claim": False,
        "risk_policy_changed": False,
        "broker_order_authority": False,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def _parse_config(payload: dict[str, object]) -> BaseBreakoutStrategyConfig:
    if payload.get("strategy_id") != STRATEGY_ID:
        raise DataValidationError("config strategy_id mismatch")
    state_payload = payload.get("state_thresholds")
    if not isinstance(state_payload, dict):
        raise DataValidationError("state_thresholds must be an object")
    allowed = {item.name for item in fields(S5BaseConfig)}
    if set(state_payload) != allowed:
        raise DataValidationError("state_thresholds must exactly bind S5BaseConfig")
    horizons = payload.get("evaluation_horizons")
    if not isinstance(horizons, list):
        raise DataValidationError("evaluation_horizons must be a list")
    return BaseBreakoutStrategyConfig(
        max_names=int(payload["max_names"]),
        weight_per_name=float(payload["weight_per_name"]),
        evaluation_horizons=tuple(horizons),
        state_config=S5BaseConfig(**state_payload),
    )


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    ordered = frame.sort_values(["trade_date", "instrument_id"], kind="stable")
    hashed = pd.util.hash_pandas_object(ordered, index=False).to_numpy().tobytes()
    return hashlib.sha256(hashed).hexdigest()


def _git_head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


if __name__ == "__main__":
    main()
