"""Bounded daily-versus-weekly signal evaluation in Discovery only."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.storage import ParquetStorage
from quantlab.research.evaluation import summarize_ic
from quantlab.research.factor_experiment import _factor_metrics, _year_frame
from quantlab.research.factor_registry import add_transparent_combination

SHANGHAI = ZoneInfo("Asia/Shanghai")
CANDIDATES = (
    "momentum_1d",
    "intraday_strength",
    "low_amplitude",
    "float_ratio",
    "transparent_combo_v1",
    "reversal_20d",
)


def _head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
    ).strip()


def _contained_end(open_dates: list[date], end: date, horizon: int) -> date:
    eligible = [item for item in open_dates if item <= end]
    if len(eligible) <= horizon:
        raise ValueError("discovery period is too short for contained labels")
    return eligible[-horizon - 1]


def run_cadence_audit(
    config_path: Path,
    *,
    storage: ParquetStorage | None = None,
    output_root: Path | None = None,
) -> Path:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    storage = storage or ParquetStorage(PROJECT_ROOT / "data/canonical")
    output_root = output_root or PROJECT_ROOT / "data/experiments"
    started = time.perf_counter()
    discovery_start, discovery_end = map(date.fromisoformat, config["discovery"])
    calendar = storage.load_trading_calendar()
    open_dates = sorted({item.trade_date for item in calendar if item.is_open})
    contained_end = _contained_end(open_dates, discovery_end, int(config["label_horizon_sessions"]))
    detail_frames = []
    summary_rows = []
    for cadence in ("daily", "weekly"):
        factor_rows: dict[str, list[pd.DataFrame]] = {item: [] for item in CANDIDATES}
        for year in range(discovery_start.year, contained_end.year + 1):
            frame = _year_frame(
                storage, year, discovery_start, contained_end, open_dates, cadence=cadence
            )
            frame = add_transparent_combination(
                frame,
                ["reversal_20d", "low_amplitude", "small_size", "intraday_strength"],
            )
            for candidate in CANDIDATES:
                _, rows = _factor_metrics(frame, candidate)
                rows["cadence"] = cadence
                factor_rows[candidate].append(rows)
                detail_frames.append(rows)
        for candidate, parts in factor_rows.items():
            values = pd.concat(parts, ignore_index=True)["rank_ic"]
            metrics = summarize_ic(values)
            summary_rows.append({"cadence": cadence, "candidate": candidate, **metrics})

    summary_frame = pd.DataFrame(summary_rows)
    comparison = {}
    for candidate in CANDIDATES:
        subset = summary_frame[summary_frame["candidate"].eq(candidate)].set_index("cadence")
        daily = float(subset.loc["daily", "mean_rank_ic"])
        weekly = float(subset.loc["weekly", "mean_rank_ic"])
        comparison[candidate] = {
            "daily_mean_rank_ic": daily,
            "weekly_mean_rank_ic": weekly,
            "same_sign": (daily >= 0) == (weekly >= 0),
            "absolute_difference": abs(daily - weekly),
        }
    run_id = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    out_dir = output_root / "daily_weekly_cadence_audit_v1_1" / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    summary_frame.to_csv(out_dir / "cadence_summary.csv", index=False)
    pd.concat(detail_frames, ignore_index=True).to_csv(out_dir / "cadence_rank_ic.csv", index=False)
    payload = {
        "schema": "daily_weekly_cadence_audit_v1_1",
        "run_id": run_id,
        "code_head": _head(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "scope": "discovery_only",
        "period": [discovery_start.isoformat(), contained_end.isoformat()],
        "label": config["label"],
        "cadences": ["daily", "weekly_first_open_session"],
        "candidates": list(CANDIDATES),
        "comparison": comparison,
        "free_frequency_search": False,
        "strategy_promoted": False,
        "performance_claim": False,
        "runtime_seconds": time.perf_counter() - started,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return out_dir
