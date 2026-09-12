"""Exactly six weekly Qlib fit attempts with saved-anchor comparison, never live authority."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import pickle
import subprocess
import sys
import time
from datetime import datetime
from functools import partial

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import init_qlib, resource_preflight
from quantlab.research.alpha158_rolling_data import batches, fit_scaler_inplace, transform
from quantlab.research.alpha158_rolling_protocol import FitLedger, runtime_manifest
from quantlab.research.alpha158_store import Budget, atomic_seal, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_pilot_data import (
    matrix_for_week,
    policy_diagnostics,
    prediction_mask,
    update_membership,
)
from quantlab.research.weekly_pilot_protocol import (
    CONFIG,
    HEAVY,
    OUTPUT,
    RUNTIME,
    SOURCES,
    generated_bytes,
    inherited_locks,
    invoke_once,
    monitor,
    now,
    prepare_plan,
    verify_inherited_locks,
)


def predict_and_reload(root, plan, slot, folder, model, scaler, metadata):
    from quantlab.research.alpha158_rolling_models import ArrayDataset

    config = plan["config"]
    week_id = int(slot.split("_")[0].removeprefix("week"))
    chosen = config["weeks"] if week_id == 1 else [config["weeks"][week_id - 1]]
    dates = [day for week in chosen for day in week["prediction_sessions"]]
    expected_count = sum(week["prediction_rows"] for week in chosen)
    expected_hash = (
        config["all_weeks_prediction_sha256"]
        if week_id == 1
        else config["membership_sha256"][f"week{week_id}"]["prediction"]
    )
    model_sha = _sha(folder / "model.pkl")
    # This pickle was created by this worker, in its exclusive slot, moments ago.
    with (folder / "model.pkl").open("rb") as stream:
        restored = pickle.load(stream)
    if _sha(folder / "model.pkl") != model_sha:
        raise DataValidationError("saved model changed during reload")
    path = folder / "predictions.parquet"
    if path.exists():
        raise DataValidationError("prediction artifact must never be overwritten")
    writer, count, digest, maximum = None, 0, hashlib.sha256(), 0.0
    try:
        for meta, values in batches(
            root / config["history_root"],
            root / config["metadata_root"],
            metadata,
            config["features"],
        ):
            mask = prediction_mask(meta, dates).to_numpy()
            if not mask.any():
                continue
            part = meta.loc[mask].copy()
            update_membership(digest, part)
            index = pd.MultiIndex.from_frame(part[["trade_date", "instrument_id"]])
            index = index.set_names(["datetime", "instrument"])
            dataset = ArrayDataset(transform(values[mask], scaler), index=index)
            predictions = model.predict(dataset).to_numpy()
            replay = restored.predict(dataset).to_numpy()
            if not np.isfinite(predictions).all() or not np.array_equal(predictions, replay):
                raise DataValidationError("saved model does not exactly reproduce predictions")
            maximum = max(maximum, float(np.abs(predictions - replay).max()))
            part["score"] = predictions.astype("float64")
            table = pa.Table.from_pandas(part, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema)
            writer.write_table(table)
            count += len(part)
    finally:
        if writer is not None:
            writer.close()
    if count != expected_count or digest.hexdigest() != expected_hash:
        raise DataValidationError("weekly prediction population or identities changed")
    return {
        "prediction_rows": count,
        "membership_sha256": digest.hexdigest(),
        "saved_model_sha256": model_sha,
        "prediction_file_sha256": _sha(path),
        "saved_model_max_prediction_difference": maximum,
    }


def validated_worker(root, plan, slot):
    """Publish only a complete source-bound worker; recovery never calls fit again."""
    folder = root / OUTPUT / "fits" / slot
    summary = sealed_read(folder / "worker_result.json")
    invoked, start = sealed_read(folder / "invoked.json"), sealed_read(folder / "started.json")
    config = plan["config"]
    week_id, kind = slot.split("_")
    week = config["weeks"][int(week_id.removeprefix("week")) - 1]
    chosen = config["weeks"] if week["week"] == 1 else [week]
    expected_count = sum(row["prediction_rows"] for row in chosen)
    expected_hash = (
        config["all_weeks_prediction_sha256"]
        if week["week"] == 1
        else config["membership_sha256"][week_id]["prediction"]
    )
    if any(
        value.get("identity") != plan["fingerprint"] or value.get("slot") != slot
        for value in (summary, invoked, start)
    ):
        raise DataValidationError("worker completion identity changed")
    if (
        summary["train_rows"] != week["train_rows"]
        or summary["train_end"] != week["train_end"]
        or summary["prediction_rows"] != expected_count
        or summary["membership_sha256"] != expected_hash
        or summary["saved_model_max_prediction_difference"] != 0
        or summary["replay_prediction_start"] != week["prediction_start"]
    ):
        raise DataValidationError("worker training or prediction contract changed")
    trained, started, called = (
        datetime.fromisoformat(value)
        for value in (summary["trained_at_utc"], start["started_at"], invoked["at"])
    )
    if (
        any(
            value.utcoffset() is None or value.utcoffset().total_seconds() != 0
            for value in (trained, started, called)
        )
        or not started <= called <= trained
    ):
        raise DataValidationError("worker actual UTC chronology changed")
    if summary.get("history_already_observed") is not True or any(
        summary.get(key) is not False for key in ("performance_evidence", "execution_authority")
    ):
        raise DataValidationError("worker acquired financial authority")
    if (
        kind == "lightgbm"
        and summary["boosted_rounds"] != config["models"][kind]["num_boost_round"]
    ):
        raise DataValidationError("worker did not use fixed boosting rounds")
    if kind == "ridge" and (
        len(summary["ridge_n_iter"]) != 1
        or not 0 < summary["ridge_n_iter"][0] <= config["models"][kind]["max_iter"]
    ):
        raise DataValidationError("worker Ridge iteration contract changed")
    if (
        _sha(folder / "model.pkl") != summary["saved_model_sha256"]
        or _sha(folder / "predictions.parquet") != summary["prediction_file_sha256"]
    ):
        raise DataValidationError("worker model/predictions bytes changed")
    prediction_file = pq.ParquetFile(folder / "predictions.parquet")
    expected_columns = {
        "instrument_id",
        "trade_date",
        "future_return_5d",
        "label_end_date",
        "label_reason",
        "complete_features",
        "score",
    }
    if (
        prediction_file.metadata.num_rows != expected_count
        or set(prediction_file.schema_arrow.names) != expected_columns
    ):
        raise DataValidationError("worker prediction file population changed")
    return summary


def fit_worker(root, slot, descriptors):
    from qlib.workflow import R
    from threadpoolctl import threadpool_limits

    from quantlab.research.alpha158_rolling_models import ArrayDataset, make_model

    verify_inherited_locks(root, descriptors)
    out = root / OUTPUT
    plan = sealed_read(out / "plan.json")
    config = plan["config"]
    verify_entries(root, plan["code_files"])
    verify_entries(root, plan["sources"]["inputs"])
    if runtime_manifest() != plan["sources"]["runtime"] or slot not in config["slots"]:
        raise DataValidationError("worker runtime or slot differs from frozen contract")
    folder = out / "fits" / slot
    start = sealed_read(folder / "started.json")
    if start["identity"] != plan["fingerprint"] or start["slot"] != slot:
        raise DataValidationError("worker differs from reserved fit intent")
    if any(
        (folder / name).exists()
        for name in ("model.pkl", "invoked.json", "worker_result.json", "result.json")
    ):
        raise DataValidationError("consumed worker cannot fit again")
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    week_id, kind = slot.split("_")
    week = config["weeks"][int(week_id.removeprefix("week")) - 1]
    metadata = sealed_read(root / config["metadata_root"] / "metadata.json")
    budget = Budget(out, config)
    init_qlib(folder / "qlib", experiment_name="alpha158_weekly_pilot")
    with budget.watchdog(), threadpool_limits(limits=2):
        x, y = matrix_for_week(
            root / config["history_root"],
            root / config["metadata_root"],
            metadata,
            config["features"],
            week,
            config["membership_sha256"][week_id]["train"],
        )
        scaler = fit_scaler_inplace(x) if kind == "ridge" else None
        atomic_seal(
            folder / "preprocessing.json",
            {
                "identity": plan["fingerprint"],
                "slot": slot,
                "train_start": week["train_start"],
                "train_end": week["train_end"],
                "train_rows": len(y),
                "train_membership_sha256": config["membership_sha256"][week_id]["train"],
                "scaler": scaler,
                "features": config["features"],
                "matrix_dtype": "float32",
                "label_dtype": "float32",
                "label_transform": "none",
                "fitted_on": "mature_training_only",
            },
        )
        model = make_model(kind, config["models"])
        with R.start(experiment_name="alpha158_weekly_pilot", recorder_name=slot) as recorder:
            R.log_params(
                slot=slot,
                plan_fingerprint=plan["fingerprint"],
                source_head=plan["code_head"],
                train_rows=len(y),
                training_cutoff=week["train_end"],
                fixed_model=json.dumps(config["models"][kind], sort_keys=True),
            )
            before = time.monotonic()
            invoke_once(
                folder,
                plan["fingerprint"],
                slot,
                partial(
                    model.fit,
                    ArrayDataset(x, y),
                    **({"verbose_eval": 0} if kind == "lightgbm" else {}),
                ),
            )
            fit_seconds = time.monotonic() - before
            trained_at = now()
            with (folder / "model.pkl").open("xb") as stream:
                pickle.dump(model, stream, protocol=5)
            R.save_objects(local_path=str(folder / "model.pkl"))
            recorder_id = recorder.id
        del x, y
        gc.collect()
        predictions = predict_and_reload(root, plan, slot, folder, model, scaler, metadata)
        verify_entries(root, plan["code_files"])
        atomic_seal(
            folder / "worker_result.json",
            {
                "identity": plan["fingerprint"],
                "slot": slot,
                "kind": kind,
                "week": week["week"],
                "trained_at_utc": trained_at,
                "replay_prediction_start": week["prediction_start"],
                "train_end": week["train_end"],
                "train_rows": week["train_rows"],
                "fit_seconds": fit_seconds,
                "peak_rss_bytes": peak_rss_bytes(),
                "qlib_recorder_id": recorder_id,
                "ridge_n_iter": getattr(model, "n_iter_", None),
                "boosted_rounds": model.model.current_iteration() if kind == "lightgbm" else None,
                "history_already_observed": True,
                "performance_evidence": False,
                "execution_authority": False,
                **predictions,
            },
        )


def make_diagnostics(root, plan, results):
    config = plan["config"]
    out = root / OUTPUT
    if any(result["status"] != "completed" for result in results) or len(results) != 6:
        raise DataValidationError("all six fixed fits required before policy comparison")
    frames = {
        result["slot"]: pd.read_parquet(
            out / "fits" / result["slot"] / "predictions.parquet", use_threads=False
        )
        for result in results
    }
    if sum(len(frame) for frame in frames.values()) != config["physical_prediction_rows"]:
        raise DataValidationError("physical prediction population changed")
    sessions = sealed_read(root / config["history_root"] / "inventory.json")["sessions"]
    summaries, daily, paired = policy_diagnostics(frames, config["weeks"], sessions)
    path = out / "daily_policies.parquet"
    if path.exists():
        raise DataValidationError("policy diagnostics already exist; inspect before recovery")
    daily.to_parquet(path, index=False)
    return {
        "summaries": summaries,
        "paired": paired,
        "artifacts": {
            "daily_policies.parquet": {"sha256": _sha(path), "bytes": path.stat().st_size}
        },
    }


def run(root=PROJECT_ROOT):
    segment_started = time.monotonic()
    out = root / OUTPUT
    (root / RUNTIME).mkdir(parents=True, exist_ok=True)
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    with inherited_locks([root / HEAVY, out]) as descriptors:
        if (out / "report.json").exists():
            return load_report(root)
        plan = prepare_plan(root)
        config = plan["config"]
        ledger = FitLedger(out, plan["fingerprint"], config["slots"])
        peak = 0
        for slot in config["slots"]:
            previous = ledger.read(slot)
            if previous is not None:
                if previous["status"] != "completed":
                    folder = out / "fits" / slot
                    if not (folder / "result.json").exists() and (folder / "monitor.json").exists():
                        watched = sealed_read(folder / "monitor.json")
                        if watched["returncode"] == 0 and watched["violation"] is None:
                            summary = validated_worker(root, plan, slot)
                            ledger.finish(
                                slot,
                                "completed",
                                summary=summary,
                                monitor=watched,
                                recovery="verified saved worker; no additional fit",
                            )
                            continue
                    raise DataValidationError(
                        "consumed unfinished/failed slot requires review; no refit"
                    )
                continue
            if (
                time.monotonic() - segment_started
                >= config["max_wakeup_seconds"] - config["next_partition_time_reserve_seconds"]
            ):
                break
            resource_preflight(config)
            if generated_bytes(root) + 512 * 1024**2 > config["max_generated_bytes"]:
                raise DataValidationError("cumulative pilot disk budget exhausted")
            verify_entries(root, plan["code_files"])
            folder = ledger.start(slot)
            print(f"starting {slot}", flush=True)
            env = {
                **os.environ,
                **{
                    key: "2"
                    for key in (
                        "OMP_NUM_THREADS",
                        "OPENBLAS_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS",
                        "ARROW_NUM_THREADS",
                    )
                },
            }
            before = time.monotonic()
            with (folder / "worker.log").open("x") as stream:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "quantlab.research.weekly_pilot",
                        "--fit",
                        slot,
                        "--locks",
                        *map(str, descriptors),
                    ],
                    cwd=root,
                    env=env,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    pass_fds=descriptors,
                    start_new_session=True,
                )
                watched = monitor(process, root, config, segment_started)
            watched = atomic_seal(folder / "monitor.json", watched)
            peak = max(peak, watched["combined_peak_rss_bytes"])
            path = folder / "worker_result.json"
            if watched["returncode"] == 0 and watched["violation"] is None and path.exists():
                summary = validated_worker(root, plan, slot)
                ledger.finish(
                    slot,
                    "completed",
                    summary=summary,
                    monitor=watched,
                    seconds=time.monotonic() - before,
                )
                print(f"completed {slot}", flush=True)
            else:
                ledger.finish(
                    slot,
                    "failed",
                    monitor=watched,
                    seconds=time.monotonic() - before,
                    error="bounded worker failed; original slot consumed, no retry",
                )
                break
        results = [value for slot in config["slots"] if (value := ledger.read(slot)) is not None]
        complete = len(results) == 6 and all(value["status"] == "completed" for value in results)
        failed = any(value["status"] != "completed" for value in results)
        if generated_bytes(root) + 1024**2 > config["max_generated_bytes"]:
            raise DataValidationError("pilot has no remaining report/diagnostic disk budget")
        diagnostics = make_diagnostics(root, plan, results) if complete else {}
        verify_entries(root, plan["sources"]["inputs"])
        verify_entries(root, plan["code_files"])
        report = {
            "identity": plan["fingerprint"],
            "at": now(),
            "status": "complete" if complete else "failed" if failed else "checkpoint",
            "attempts": results,
            "cumulative_fit_attempts": len(results),
            "actual_fit_invocations": sum(
                (out / "fits" / slot / "invoked.json").exists() for slot in config["slots"]
            ),
            "completed_fits": sum(value["status"] == "completed" for value in results),
            "generated_bytes": generated_bytes(root),
            "segment_seconds": time.monotonic() - segment_started,
            "combined_peak_rss_bytes": max(
                [
                    peak,
                    *[
                        item.get("monitor", {}).get("combined_peak_rss_bytes", 0)
                        for item in results
                    ],
                ]
            ),
            "history_already_observed": True,
            "provider_calls": 0,
            "performance_evidence": False,
            "execution_authority": False,
            "automatic_promotion": False,
            **diagnostics,
        }
        path = out / ("report.json" if complete or failed else f"checkpoint-{time.time_ns()}.json")
        return atomic_seal(path, report)


def load_report(root):
    out = root / OUTPUT
    report = sealed_read(out / "report.json")
    plan = sealed_read(out / "plan.json")
    if report["identity"] != plan["fingerprint"]:
        raise DataValidationError("weekly pilot report identity changed")
    if (
        _sha(root / CONFIG) != plan["sources"]["config_sha256"]
        or json.loads((root / SOURCES).read_text()) != plan["sources"]
    ):
        raise DataValidationError("weekly pilot config or sources changed")
    for key in ("performance_evidence", "execution_authority", "automatic_promotion"):
        if report.get(key) is not False:
            raise DataValidationError("pilot gained financial authority")
    if report.get("history_already_observed") is not True or report.get("provider_calls") != 0:
        raise DataValidationError("pilot history or provider authority changed")
    verify_historical_inputs(root, {"code_head": plan["code_head"], "inputs": plan["code_files"]})
    verify_entries(root, plan["sources"]["inputs"])
    ledger = FitLedger(out, plan["fingerprint"], plan["config"]["slots"])
    actual = [item for slot in plan["config"]["slots"] if (item := ledger.read(slot)) is not None]
    if (
        report["attempts"] != actual
        or report["cumulative_fit_attempts"] != len(actual)
        or type(report["cumulative_fit_attempts"]) is not int
    ):
        raise DataValidationError("pilot ledger differs from report")
    if report["completed_fits"] != sum(item["status"] == "completed" for item in actual):
        raise DataValidationError("pilot completion count changed")
    calls = sum((out / "fits" / slot / "invoked.json").exists() for slot in plan["config"]["slots"])
    if (
        report["actual_fit_invocations"] != calls
        or not calls <= len(actual) <= 6
        or type(report["actual_fit_invocations"]) is not int
    ):
        raise DataValidationError("pilot invocation budget changed")
    if report["status"] not in ("complete", "failed"):
        raise DataValidationError("terminal pilot report has an invalid status")
    if report["status"] == "complete" and report["completed_fits"] != 6:
        raise DataValidationError("incomplete pilot presented as completed")
    for item in actual:
        if (
            item["status"] == "completed"
            and validated_worker(root, plan, item["slot"]) != item["summary"]
        ):
            raise DataValidationError("pilot summary differs from original worker")
    verify_entries(out, report.get("artifacts", {}))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fit")
    parser.add_argument("--locks", nargs="*", type=int, default=[])
    args = parser.parse_args()
    if args.fit:
        fit_worker(PROJECT_ROOT, args.fit, args.locks)
    else:
        result = run()
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in ("status", "cumulative_fit_attempts", "completed_fits")
                }
            )
        )
