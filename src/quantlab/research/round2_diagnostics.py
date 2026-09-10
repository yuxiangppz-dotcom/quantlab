"""One predeclared round: 21 daily signal comparisons, at most six fixed fits."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.context_backfill import _write_json
from quantlab.data.models import DataValidationError
from quantlab.research.evaluation import daily_rank_ic, summarize_ic
from quantlab.research.input_audit import _sha
from quantlab.research.label_period import PERIOD_NAMES, select_period_labels
from quantlab.research.round2_dataset import (
    KEYS,
    prepare_dataset,
    sealed_read,
    sealed_write,
    verify_entries,
)
from quantlab.research.round2_features import AUGMENTED_FEATURES, BASELINE_FEATURES

CONFIG_SHA256 = "b9b8fd870551379abcd411f7dda5a136962438936847602304a4ef59f3547616"
ROUND_DIRECTORY = "data/products/research_round2/research_round2_20260911"
MODEL_GROUPS = {"lightgbm_baseline": BASELINE_FEATURES, "lightgbm_augmented": AUGMENTED_FEATURES}


def load_config(root: Path = PROJECT_ROOT) -> dict:
    raw = (root / "config/research_round2_v1.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONFIG_SHA256:
        raise DataValidationError("predeclared round config changed; a new commission is required")
    return json.loads(raw)


def clean_numbers(value):
    if isinstance(value, dict):
        return {key: clean_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_numbers(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def contained_cohorts(frame, config, sessions, horizon):
    cohorts, coverage = {}, {}
    label = f"future_return_{horizon}d"
    for period in PERIOD_NAMES:
        selected, counts = select_period_labels(
            frame, config[period], sessions, horizon=horizon, label_column=label
        )
        counts["excluded_incomplete_features"] = int((~selected.common_features_available).sum())
        selected = selected.loc[selected.common_features_available].copy()
        counts["diagnostic_rows"] = len(selected)
        counts["diagnostic_dates"] = selected.trade_date.nunique()
        counts["expected_signal_dates"] = sum(
            config[period][0] <= str(day) <= config[period][1] for day in sessions
        )
        coverage[period] = counts
        cohorts[period] = selected
    return cohorts, coverage


def fit_medians(training: pd.DataFrame, columns) -> pd.Series:
    matrix = training[list(columns)].replace([np.inf, -np.inf], np.nan)
    medians = matrix.median()
    if not np.isfinite(medians).all():
        raise DataValidationError("a training feature has no finite observations")
    return medians


def model_matrix(frame: pd.DataFrame, columns, medians) -> pd.DataFrame:
    return frame[list(columns)].replace([np.inf, -np.inf], np.nan).fillna(medians).astype("float32")


def _default_model(params):
    from lightgbm import LGBMRegressor

    return LGBMRegressor(**params)


def fit_candidate(out, group, horizon, cohorts, config, dataset_fingerprint, *, factory=None):
    """Exclusive intent consumes one fit even on failure. Never retry an existing intent."""
    if group not in MODEL_GROUPS or horizon not in (5, 10, 20):
        raise DataValidationError("candidate is outside the six-fit budget")
    candidate = out / f"{group}_{horizon}d"
    candidate.mkdir(exist_ok=False)
    columns = MODEL_GROUPS[group]
    label = f"future_return_{horizon}d"
    intent = {
        "group": group,
        "horizon": horizon,
        "features": list(columns),
        "params": config["lightgbm"],
        "dataset_fingerprint": dataset_fingerprint,
        "started_at": datetime.now(UTC).isoformat(),
        "target": label,
        "target_transform": config["target_transform"],
    }
    sealed_write(candidate / "intent.json", intent)
    try:
        train = cohorts["discovery"]
        if train.empty:
            raise DataValidationError("training cohort is empty")
        medians = fit_medians(train, columns)
        sealed_write(
            candidate / "preprocessing.json",
            {
                "fitted_period": "discovery",
                "rows": len(train),
                "max_signal_date": str(train.trade_date.max()),
                "max_label_end_date": str(train.label_end_date.max()),
                "medians": medians.to_dict(),
                "dtype": "float32",
                "label_transform": "none",
            },
        )
        model = (factory or _default_model)(config["lightgbm"])
        model.fit(model_matrix(train, columns, medians), train[label].to_numpy(dtype=float))
        model.booster_.save_model(str(candidate / "model.txt"))
        predictions = {}
        for period, cohort in cohorts.items():
            predicted = cohort[[*KEYS, "label_end_date", label]].copy()
            predicted["alpha_score"] = (
                model.predict(model_matrix(cohort, columns, medians))
                if len(cohort)
                else np.empty(0)
            )
            if not np.isfinite(predicted.alpha_score).all():
                raise DataValidationError("model returned nonfinite predictions")
            predicted.to_parquet(candidate / f"{period}.parquet", index=False)
            predictions[period] = predicted
        artifacts = {path.name: {"sha256": _sha(path)} for path in sorted(candidate.iterdir())}
        sealed_write(
            candidate / "result.json",
            {
                "status": "complete",
                "finished_at": datetime.now(UTC).isoformat(),
                "artifacts": artifacts,
                "feature_importance": dict(
                    zip(columns, model.feature_importances_.tolist(), strict=True)
                ),
            },
        )
        return predictions, None
    except Exception as exc:
        artifacts = {path.name: {"sha256": _sha(path)} for path in sorted(candidate.iterdir())}
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "finished_at": datetime.now(UTC).isoformat(),
            "artifacts": artifacts,
        }
        sealed_write(candidate / "result.json", failure)
        return None, failure


def describe_scores(predicted: pd.DataFrame, label: str):
    if predicted.empty:
        ic = pd.Series(dtype=float, index=pd.Index([], name="trade_date"))
    else:
        ic = daily_rank_ic(predicted, label)
    summary = clean_numbers(summarize_ic(ic))
    summary.update({"rows": len(predicted), "dates": predicted.trade_date.nunique()})
    annual = []
    if not ic.empty:
        for year, values in ic.groupby(pd.to_datetime(ic.index).year):
            annual.append({"year": int(year), **clean_numbers(summarize_ic(values))})
    daily = ic.rename("rank_ic").reset_index()
    return summary, annual, daily


def run_diagnostics(root: Path, out: Path, *, factory=None, progress=print) -> dict:
    config = load_config(root)
    stage = out / "stage"
    manifest = sealed_read(stage / "manifest.json")
    if manifest["config"] != config:
        raise DataValidationError("staged configuration differs from predeclared configuration")
    verify_entries(root, manifest["inputs"])
    verify_entries(stage, manifest["artifacts"])
    results_dir = out / "results"
    # A whole-round exclusive directory prevents concurrent callers multiplying fits.
    results_dir.mkdir(exist_ok=False)
    _write_json(
        results_dir / "started.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "stage_fingerprint": manifest["fingerprint"],
            "maximum_fits": 6,
        },
    )
    models = results_dir / "models"
    models.mkdir()
    frame = pd.read_parquet(stage / "dataset.parquet")
    sessions = [date.fromisoformat(value) for value in manifest["open_dates"]]
    results, all_daily, coverage, fits = [], [], {}, 0
    try:
        for horizon in config["horizons"]:
            cohorts, coverage[str(horizon)] = contained_cohorts(frame, config, sessions, horizon)
            label = f"future_return_{horizon}d"
            for group in config["signal_groups"]:
                error = None
                if group in MODEL_GROUPS:
                    fits += 1
                    if fits > config["model_fit_budget"]:
                        raise DataValidationError("model fit budget exhausted")
                    progress(f"fit {fits}/6: {group} horizon={horizon}", flush=True)
                    predictions, error = fit_candidate(
                        models,
                        group,
                        horizon,
                        cohorts,
                        config,
                        manifest["fingerprint"],
                        factory=factory,
                    )
                else:
                    predictions = {
                        period: data[[*KEYS, label, group]].rename(columns={group: "alpha_score"})
                        for period, data in cohorts.items()
                    }
                candidate = {
                    "group": group,
                    "horizon": horizon,
                    "status": "failed" if error else "complete",
                    "error": error,
                    "periods": {},
                }
                if not error:
                    for period, predicted in predictions.items():
                        summary, annual, daily = describe_scores(predicted, label)
                        candidate["periods"][period] = {"summary": summary, "annual": annual}
                        daily["group"], daily["horizon"], daily["period"] = group, horizon, period
                        all_daily.append(daily)
                results.append(candidate)
                sealed_write(results_dir / f"{group}_{horizon}d.json", candidate)
                progress(f"diagnosed {len(results)}/21: {group} horizon={horizon}", flush=True)
            del cohorts
        if len(results) != 21 or fits != 6:
            raise DataValidationError("predeclared comparisons were not all attempted")
        daily_path = results_dir / "daily_ic.parquet"
        pd.concat(all_daily, ignore_index=True).to_parquet(daily_path, index=False)
        verify_entries(root, manifest["inputs"])
        verify_entries(stage, manifest["artifacts"])
        artifacts = {
            path.relative_to(results_dir).as_posix(): {"sha256": _sha(path)}
            for path in sorted(results_dir.rglob("*"))
            if path.is_file()
        }
        result = {
            "schema_version": 1,
            "round_id": config["round_id"],
            "status": "complete"
            if all(item["status"] == "complete" for item in results)
            else "complete_with_failed_candidates",
            "finished_at": datetime.now(UTC).isoformat(),
            "stage_fingerprint": manifest["fingerprint"],
            "code_head": manifest["code_head"],
            "config": config,
            "fit_attempts": fits,
            "comparisons": results,
            "coverage": coverage,
            "artifacts": artifacts,
            "feature_coverage": {
                name: manifest[name]
                for name in (
                    "feature_rows",
                    "common_feature_rows",
                    "feature_missing",
                    "invalid_cap_rows",
                )
            },
            "runtime": {
                "python": platform.python_version(),
                **{
                    name: importlib.metadata.version(name)
                    for name in ("numpy", "pandas", "pyarrow", "lightgbm")
                },
            },
            "performance_eligible": False,
            "execution_authority": False,
            "drawdown_assessed": False,
            "net_return_assessed": False,
            "limitations": [
                "retrospectively observed/revised provider history",
                "common complete-feature cohort conditioned on observed label availability",
                "historical tradability and corporate actions not fully certified",
                "no fees, fills or executable returns in RankIC",
                "overlapping labels: no IID significance claims",
                "2025 onward already observed; not fresh out-of-sample",
                "20 percent drawdown target not tested; no automatic promotion",
            ],
        }
        return sealed_write(results_dir / "report.json", result)
    except BaseException as exc:
        sealed_write(
            results_dir / "interrupted_or_failed.json",
            {
                "status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "fit_attempts": fits,
                "completed_comparisons": len(results),
                "at": datetime.now(UTC).isoformat(),
                "rerun_authorized": False,
            },
        )
        raise


def load_report(out: Path, *, full_verify: bool = False) -> dict:
    report = sealed_read(out / "results/report.json")
    stage = sealed_read(out / "stage/manifest.json")
    if report["stage_fingerprint"] != stage["fingerprint"]:
        raise DataValidationError("report stage identity mismatch")
    if full_verify:
        verify_entries(out / "results", report["artifacts"])
        verify_entries(out / "stage", stage["artifacts"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "verify"))
    args = parser.parse_args()
    root, out = PROJECT_ROOT, PROJECT_ROOT / ROUND_DIRECTORY
    if args.action == "prepare":
        result = prepare_dataset(root, out / "stage", load_config(root))
    elif args.action == "run":
        result = run_diagnostics(root, out)
    else:
        result = load_report(out, full_verify=True)
    print(json.dumps({"fingerprint": result["fingerprint"], "action": args.action}), flush=True)


if __name__ == "__main__":
    main()
