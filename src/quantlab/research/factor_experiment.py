"""Bounded factor and fixed-LightGBM research experiment for Daily v1."""

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

from quantlab.daily.service import PROJECT_ROOT, inspect_data_status
from quantlab.data.models import DataValidationError
from quantlab.data.storage import ParquetStorage
from quantlab.research.dataset import build_research_dataset
from quantlab.research.evaluation import daily_rank_ic, summarize_ic
from quantlab.research.factor_registry import (
    FACTOR_REGISTRY,
    add_transparent_combination,
    build_factor_columns,
    registry_rows,
)
from quantlab.research.label_period import (
    LABEL_PERIOD_POLICY,
    PERIOD_NAMES,
    select_period_labels,
    validate_factor_periods,
)
from quantlab.research.qlib_adapter import qlib_integration_status, to_qlib_static_loader
from quantlab.research.universe import filter_v1_universe

SHANGHAI = ZoneInfo("Asia/Shanghai")
FEATURE_COLUMNS = [item.factor_id for item in FACTOR_REGISTRY]


def _head() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _monthly_signal_dates(open_dates: list[date], start: date, end: date) -> list[date]:
    selected: dict[tuple[int, int], date] = {}
    for day in open_dates:
        if start <= day <= end:
            selected.setdefault((day.year, day.month), day)
    return list(selected.values())


def _cadence_signal_dates(
    open_dates: list[date], start: date, end: date, cadence: str
) -> list[date]:
    in_range = [day for day in open_dates if start <= day <= end]
    if cadence == "daily":
        return in_range
    if cadence == "weekly":
        selected: dict[tuple[int, int], date] = {}
        for day in in_range:
            iso = day.isocalendar()
            selected.setdefault((iso.year, iso.week), day)
        return list(selected.values())
    if cadence == "monthly":
        return _monthly_signal_dates(open_dates, start, end)
    raise ValueError(f"unsupported evaluation cadence: {cadence}")


def _year_frame(
    storage: ParquetStorage,
    year: int,
    start: date,
    end: date,
    open_dates: list[date],
    cadence: str = "monthly",
) -> pd.DataFrame:
    year_start = max(start, date(year, 1, 1))
    year_end = min(end, date(year, 12, 31))
    dataset = filter_v1_universe(
        build_research_dataset(
            storage,
            year_start,
            year_end,
            return_horizons=(1, 5, 20),
            forward_horizons=(5,),
        )
    )
    signals = _cadence_signal_dates(open_dates, year_start, year_end, cadence)
    dataset = dataset[dataset["trade_date"].isin(signals)]
    raw_rows = []
    basic_rows = []
    for signal_date in signals:
        raw_rows.extend(asdict(item) for item in storage.load_daily_bars_by_date(signal_date))
        basic_rows.extend(asdict(item) for item in storage.load_daily_basic_by_date(signal_date))
    raw = pd.DataFrame(raw_rows)
    basics = pd.DataFrame(basic_rows)
    joined = dataset.merge(
        raw[["instrument_id", "trade_date", "open", "high", "low", "amount"]],
        on=["instrument_id", "trade_date"],
        validate="one_to_one",
    )
    joined = joined.merge(
        basics[["instrument_id", "trade_date", "turnover_rate", "total_mv", "circ_mv"]],
        on=["instrument_id", "trade_date"],
        validate="one_to_one",
    )
    return build_factor_columns(joined)


def build_experiment_frame(storage: ParquetStorage, config: dict) -> tuple[pd.DataFrame, date]:
    validate_factor_periods(config)
    status = inspect_data_status(storage, datetime.now(SHANGHAI).date())
    if status["effective_as_of"] is None:
        raise DataValidationError("factor experiment requires a complete local data date")
    effective = date.fromisoformat(status["effective_as_of"])
    calendar = storage.load_trading_calendar()
    open_dates = sorted(
        {item.trade_date for item in calendar if item.is_open and item.trade_date <= effective}
    )
    effective_index = open_dates.index(effective)
    horizon = int(config["label_horizon_sessions"])
    if effective_index < horizon:
        raise DataValidationError("insufficient sessions for label containment")
    signal_end = open_dates[effective_index - horizon]
    start = date.fromisoformat(config["data_start"])
    parts = [
        _year_frame(storage, year, start, signal_end, open_dates)
        for year in range(start.year, signal_end.year + 1)
    ]
    frame = pd.concat(parts, ignore_index=True)
    frame = add_transparent_combination(frame, config["combination"])
    return frame, signal_end


def _experiment_periods(
    frame: pd.DataFrame, config: dict, open_dates: list[date],
) -> tuple[dict[str, pd.DataFrame], dict]:
    validate_factor_periods(config)
    periods = {}
    counts = {}
    for name in PERIOD_NAMES:
        periods[name], counts[name] = select_period_labels(
            frame, config[name], open_dates,
            horizon=config["label_horizon_sessions"], label_column="future_return_5d",
        )
    calendar_text = "\n".join(day.isoformat() for day in sorted(set(open_dates)))
    return periods, {
        "policy": LABEL_PERIOD_POLICY,
        "horizon_sessions": config["label_horizon_sessions"],
        "label_column": "future_return_5d",
        "open_calendar_sha256": hashlib.sha256(calendar_text.encode()).hexdigest(),
        "periods": counts,
    }


def _factor_metrics(frame: pd.DataFrame, column: str) -> tuple[dict, pd.DataFrame]:
    alpha = frame[["instrument_id", "trade_date", "future_return_5d"]].copy()
    alpha["alpha_score"] = frame[column].to_numpy()
    daily = (
        daily_rank_ic(alpha, "future_return_5d") if not alpha.empty
        else pd.Series(dtype=float, index=pd.Index([], name="trade_date"))
    )
    rows = daily.rename("rank_ic").reset_index()
    rows["factor_id"] = column
    return summarize_ic(daily), rows


def _train_lightgbm(
    frame: pd.DataFrame, config: dict, open_dates: list[date],
) -> tuple[dict, pd.DataFrame, object | None]:
    periods, selection = _experiment_periods(frame, config, open_dates)
    train = periods["discovery"]
    if train.empty:
        return (
            {"status": "no_contained_training_labels", "label_selection": selection},
            pd.DataFrame(), None,
        )
    try:
        from lightgbm import LGBMRegressor
    except (ImportError, OSError) as exc:
        return (
            {
                "status": "runtime_dependency_unavailable",
                "error_class": type(exc).__name__,
                "note": "LightGBM was not run; no fallback model is presented as LightGBM",
                "label_selection": selection,
            },
            pd.DataFrame(),
            None,
        )

    medians = train[FEATURE_COLUMNS].median()
    params = config["lightgbm"]
    model = LGBMRegressor(verbosity=-1, deterministic=True, **params)
    model.fit(train[FEATURE_COLUMNS].fillna(medians), train["future_return_5d"])

    prediction_parts = []
    metrics: dict[str, object] = {
        "status": "trained",
        "train_rows": len(train),
        "feature_columns": FEATURE_COLUMNS,
        "preprocessing": "median values fitted on discovery only",
        "params": params,
        "label_selection": selection,
    }
    for name in ("validation", "test_observed"):
        subset = periods[name]
        scored = subset[
            ["instrument_id", "trade_date", "label_end_date", "future_return_5d"]
        ].copy()
        scored["alpha_score"] = (
            model.predict(subset[FEATURE_COLUMNS].fillna(medians)) if not subset.empty
            else pd.Series(dtype=float)
        )
        metrics[name], _ = _factor_metrics(scored, "alpha_score")
        scored["period"] = name
        prediction_parts.append(scored)
    predictions = pd.concat(prediction_parts, ignore_index=True)
    metrics["train_medians"] = {key: float(value) for key, value in medians.items()}
    return metrics, predictions, model


def run_factor_experiment(
    config_path: Path,
    *,
    storage: ParquetStorage | None = None,
    output_root: Path | None = None,
) -> Path:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    validate_factor_periods(config)
    if config["candidate_budget"] != len(FACTOR_REGISTRY):
        raise DataValidationError("candidate budget must exactly bind the registry inventory")
    if len(FACTOR_REGISTRY) > 12:
        raise DataValidationError("independent factor candidate budget exceeds 12")
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    output_root = output_root or PROJECT_ROOT / "data" / "experiments"
    started = time.perf_counter()
    frame, signal_end = build_experiment_frame(storage, config)
    open_dates = sorted({
        item.trade_date for item in storage.load_trading_calendar() if item.is_open
    })
    periods, selection = _experiment_periods(frame, config, open_dates)

    threshold = config["promotion_threshold"]
    registry = registry_rows()
    daily_rows = []
    for row in registry:
        factor_id = row["factor_id"]
        discovery, daily = _factor_metrics(periods["discovery"], factor_id)
        validation, validation_daily = _factor_metrics(
            periods["validation"], factor_id
        )
        observed, observed_daily = _factor_metrics(
            periods["test_observed"], factor_id
        )
        row["discovery"] = discovery
        row["validation"] = validation
        row["test_observed"] = observed
        row["status"] = (
            "research_candidate"
            if validation["mean_rank_ic"] >= threshold["validation_mean_rank_ic"]
            and validation["positive_ratio"] >= threshold["validation_positive_ratio"]
            else "rejected_or_observe"
        )
        daily_rows.extend(
            pd.concat([daily, validation_daily, observed_daily], ignore_index=True).to_dict(
                "records"
            )
        )

    combo_metrics = {}
    for period_name in PERIOD_NAMES:
        combo_metrics[period_name], daily = _factor_metrics(
            periods[period_name], "transparent_combo_v1"
        )
        daily_rows.extend(daily.to_dict("records"))

    ml_metrics, predictions, model = _train_lightgbm(frame, config, open_dates)
    qlib_status = qlib_integration_status()
    if qlib_status["qlib_available"]:
        loader = to_qlib_static_loader(frame.head(1000), FEATURE_COLUMNS)
        loaded = loader.load()
        qlib_status["smoke_rows"] = len(loaded)
        qlib_status["status"] = "static_loader_smoke_passed"

    run_id = datetime.now(SHANGHAI).strftime("%Y%m%dT%H%M%S")
    out_dir = output_root / config["experiment_id"] / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    registry_frame = pd.json_normalize(registry, sep=".")
    registry_frame.to_csv(out_dir / "alpha_registry.csv", index=False)
    pd.DataFrame(daily_rows).to_csv(out_dir / "daily_rank_ic.csv", index=False)
    predictions.to_csv(out_dir / "lightgbm_predictions.csv", index=False)
    if model is not None:
        model.booster_.save_model(out_dir / "lightgbm_model.txt")
    config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    summary = {
        "schema": "daily_factor_research_v1",
        "run_id": run_id,
        "code_head": _head(),
        "config_sha256": config_sha,
        "data_start": config["data_start"],
        "signal_end": signal_end.isoformat(),
        "signal_rows": len(frame),
        "signal_dates": frame["trade_date"].nunique(),
        "label_selection": selection,
        "independent_candidate_count": len(FACTOR_REGISTRY),
        "alpha158_input_feature_count": len(FEATURE_COLUMNS),
        "alpha158_scope": "style subset only; not the complete official Alpha158 handler",
        "registry": registry,
        "transparent_combination": combo_metrics,
        "lightgbm": ml_metrics,
        "qlib": qlib_status,
        "test_observed": True,
        "performance_claim": False,
        "runtime_seconds": time.perf_counter() - started,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return out_dir
