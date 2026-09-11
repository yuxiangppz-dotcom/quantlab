"""Isolated six-fit Qlib rolling experiment with immutable intents and bounded workers."""

from __future__ import annotations

import argparse
import json
import os
import pickle
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_native import load_contract as native_contract
from quantlab.research.alpha158_rolling_data import (
    batches,
    fit_scaler_inplace,
    prediction_mask,
    stage_metadata,
    training_matrix,
    transform,
)
from quantlab.research.alpha158_rolling_metrics import daily_diagnostics
from quantlab.research.alpha158_rolling_protocol import (
    HISTORY,
    OUTPUT,
    FitLedger,
    load_contract,
    now,
)
from quantlab.research.alpha158_store import (
    Budget,
    atomic_seal,
    directory_bytes,
    exclusive_job,
    peak_rss_bytes,
)
from quantlab.research.round2_dataset import (
    InputBinding,
    code_binding,
    sealed_read,
    verify_entries,
)


def slots(config):
    return [f"{fold['id']}_{kind}" for fold in config["folds"] for kind in ("ridge", "lightgbm")]


def prepare_plan(root, out):
    config, source = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"],
        cwd=root,
        text=True,
    ).strip()
    if head != upstream:
        raise DataValidationError("model implementation must be pushed before real research")
    payload = {
        "schema": "quantlab_alpha158_rolling_v1",
        "config": config,
        "source": source,
        "code_head": head,
        "code_files": binding.entries,
        "features": [f["name"] for f in native_contract(root)["features"]],
        "slots": slots(config),
    }
    path = out / "plan.json"
    if path.exists():
        result = sealed_read(path)
        if {k: v for k, v in result.items() if k != "fingerprint"} != payload:
            raise DataValidationError("rolling resume code/config/runtime identity changed")
        return result
    return atomic_seal(path, payload)


def init_qlib(folder, experiment_name="alpha158_fixed_rolling"):
    import tempfile

    import qlib

    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "temp"
    temp.mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(temp)
    tempfile.tempdir = str(temp)
    # Qlib 0.9.7 uses this supported MLflow backend; 3.16 requires explicit opt-in.
    os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"
    # Precreate under our job lock. Qlib's file:// creation lock strips the leading
    # slash and would otherwise leave a second directory under the current checkout.
    from mlflow.tracking import MlflowClient

    uri = (folder / "mlruns").resolve().as_uri()
    client = MlflowClient(tracking_uri=uri)
    if client.get_experiment_by_name(experiment_name) is None:
        client.create_experiment(experiment_name)
    # No market provider calls; ArrayDataset owns all model input.
    qlib.init(
        provider_uri=str(folder / "unused_provider"),
        region="cn",
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
        exp_manager={
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {
                "uri": uri,
                "default_exp_name": "alpha158_rolling",
            },
        },
    )


def score_model(root, out, folder, model, scaler, fold, plan, metadata):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from quantlab.research.alpha158_rolling_models import ArrayDataset

    periods = {"train": fold["train"], "evaluation": fold["evaluation"]}
    if fold["id"] == "fold3":
        periods["observed_2026"] = plan["config"]["observed_2026"]
    writers = {}
    # Pickle is local, freshly written by this trusted worker; external pickles are never loaded.
    with (folder / "model.pkl").open("rb") as handle:
        restored = pickle.load(handle)
    maximum = 0.0
    try:
        for meta, matrix in batches(root / HISTORY, out, metadata, plan["features"]):
            for period, bounds in periods.items():
                mask = prediction_mask(meta, bounds).to_numpy()
                if not mask.any():
                    continue
                part = meta.loc[mask].copy()
                index = pd.MultiIndex.from_frame(part[["trade_date", "instrument_id"]])
                index = index.set_names(["datetime", "instrument"])
                dataset = ArrayDataset(transform(matrix[mask], scaler), index=index)
                prediction = model.predict(dataset).to_numpy()
                reproduced = restored.predict(dataset).to_numpy()
                if not np.array_equal(prediction, reproduced) or not np.isfinite(prediction).all():
                    raise DataValidationError("saved-model prediction reproduction failed")
                maximum = max(maximum, float(np.max(np.abs(prediction - reproduced))))
                part["score"] = prediction.astype("float64")
                table = pa.Table.from_pandas(part, preserve_index=False)
                if period not in writers:
                    writers[period] = pq.ParquetWriter(
                        folder / f"scores_{period}.parquet", table.schema
                    )
                writers[period].write_table(table)
    finally:
        for writer in writers.values():
            writer.close()
    summaries = {}
    sessions = sealed_read(root / HISTORY / "inventory.json")["sessions"]
    for period, bounds in periods.items():
        path = folder / f"scores_{period}.parquet"
        if not path.exists():
            raise DataValidationError("empty fixed scoring period")
        frame = pd.read_parquet(path, use_threads=False)
        daily, summary = daily_diagnostics(frame, bounds, sessions)
        daily.to_parquet(folder / f"daily_{period}.parquet", index=False)
        summaries[period] = summary
        del frame, daily
    return summaries, maximum


def fit_worker(root, slot):
    import pyarrow as pa
    from qlib.workflow import R
    from threadpoolctl import threadpool_limits

    from quantlab.research.alpha158_rolling_models import ArrayDataset, make_model

    out = root / OUTPUT
    plan = sealed_read(out / "plan.json")
    config = plan["config"]
    verify_entries(root, plan["code_files"])
    load_contract(root)
    if slot not in plan["slots"]:
        raise DataValidationError("unknown supervised fit slot")
    folder = out / "fits" / slot
    start = sealed_read(folder / "started.json")
    if start["identity"] != plan["fingerprint"] or any(
        (folder / name).exists() for name in ("model.pkl", "result.json", "worker_result.json")
    ):
        raise DataValidationError("worker cannot fit a consumed model again")
    atomic_seal(folder / "invoked.json", {"identity": plan["fingerprint"], "at": now()})
    fold_id, kind = slot.split("_", 1)
    fold = next(f for f in config["folds"] if f["id"] == fold_id)
    metadata = sealed_read(out / "metadata.json")
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    budget = Budget(out, config)
    init_qlib(folder / "qlib")
    with budget.watchdog(), threadpool_limits(limits=2):
        x, y = training_matrix(root / HISTORY, out, metadata, plan["features"], fold)
        scaler = fit_scaler_inplace(x) if kind == "ridge" else None
        atomic_seal(
            folder / "preprocessing.json",
            {
                "fold": fold,
                "train_rows": len(y),
                "scaler": scaler,
                "feature_dtype": "float32",
                "training_label_dtype": "float32",
                "label_transform": "none",
                "fitted_on": "purged_training_only",
            },
        )
        model = make_model(kind, config)
        with R.start(experiment_name="alpha158_fixed_rolling", recorder_name=slot) as recorder:
            R.log_params(
                slot=slot,
                plan_fingerprint=plan["fingerprint"],
                source_head=plan["code_head"],
                train_rows=len(y),
                fixed_model=json.dumps(config[kind], sort_keys=True),
            )
            before = time.monotonic()
            if kind == "lightgbm":
                model.fit(ArrayDataset(x, y), verbose_eval=0)
            else:
                model.fit(ArrayDataset(x, y))
            fit_seconds = time.monotonic() - before
            with (folder / "model.pkl").open("xb") as handle:
                pickle.dump(model, handle, protocol=5)
            R.save_objects(local_path=str(folder / "model.pkl"))
            record_id = recorder.id
        del x, y
        # Return large allocator arenas before reading millions of diagnostic rows.
        import gc

        gc.collect()
        summaries, maximum = score_model(root, out, folder, model, scaler, fold, plan, metadata)
        result = {
            "kind": kind,
            "fold": fold,
            "fit_seconds": fit_seconds,
            "peak_rss_bytes": peak_rss_bytes(),
            "qlib_recorder_id": record_id,
            "ridge_n_iter": getattr(model, "n_iter_", None),
            "boosted_rounds": model.model.current_iteration() if kind == "lightgbm" else None,
            "summaries": summaries,
            "saved_model_max_prediction_difference": maximum,
            "performance_eligible": False,
            "execution_authority": False,
        }
        atomic_seal(folder / "worker_result.json", result)


def resource_preflight(config):
    available = (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    if available < config["minimum_available_memory_bytes"]:
        raise DataValidationError("less than 10 GiB available RAM; no next fit started")
    if shutil.disk_usage("/mnt/d").free < (
        config["max_generated_bytes"] + config["reserve_host_D_bytes"]
    ):
        raise DataValidationError("insufficient D reserve; no next fit started")


def paired_diagnostics(out, plan, results):
    import pyarrow.parquet as pq

    paired = []
    by_slot = {r["slot"]: r for r in results}
    for fold in plan["config"]["folds"]:
        left, right = (f"{fold['id']}_{kind}" for kind in ("ridge", "lightgbm"))
        if any(by_slot.get(s, {}).get("status") != "completed" for s in (left, right)):
            continue
        for period in by_slot[left]["summary"]["summaries"]:
            files = [
                pq.ParquetFile(out / "fits" / slot / f"scores_{period}.parquet")
                for slot in (left, right)
            ]
            keys = [c for c in files[0].schema_arrow.names if c != "score"]
            if files[0].metadata.num_rows != files[1].metadata.num_rows:
                raise DataValidationError("model comparison cohort counts differ")
            for a, b in zip(
                *(f.iter_batches(columns=keys, batch_size=65536, use_threads=False) for f in files),
                strict=True,
            ):
                if not a.equals(b):
                    raise DataValidationError("model comparison cohorts differ")
            da, db = [
                pd.read_parquet(out / "fits" / slot / f"daily_{period}.parquet", use_threads=False)
                for slot in (left, right)
            ]
            if not da[["trade_date", "evaluation_rows"]].equals(
                db[["trade_date", "evaluation_rows"]]
            ):
                raise DataValidationError("daily paired diagnostic cohorts differ")
            common = da.rank_ic.notna() & db.rank_ic.notna()
            paired.append(
                {
                    "fold": fold["id"],
                    "period": period,
                    "identical_score_identities": True,
                    "paired_rank_ic_days": int(common.sum()),
                    "mean_lightgbm_minus_ridge_rank_ic": float(
                        (db.rank_ic[common] - da.rank_ic[common]).mean()
                    )
                    if common.any()
                    else None,
                }
            )
    return paired


def run(root=PROJECT_ROOT):
    out = root / OUTPUT
    with exclusive_job(out):
        plan = prepare_plan(root, out)
        config = plan["config"]
        resource_preflight(config)
        budget = Budget(out, config)
        with budget.watchdog():
            stage_metadata(root / HISTORY, out, plan, plan["features"])
        ledger = FitLedger(out, plan["fingerprint"], plan["slots"])
        for slot in plan["slots"]:
            prior = ledger.read(slot)
            if prior is not None:
                if (
                    prior["status"] == "interrupted"
                    and not (out / "fits" / slot / "result.json").exists()
                ):
                    ledger.finish(
                        slot, "interrupted", error="prior worker exited without terminal receipt"
                    )
                continue
            if not budget.can_start():
                break
            resource_preflight(config)
            budget.check(projected_bytes=512 * 1024**2)
            folder = ledger.start(slot)
            print(f"starting fixed fit {slot}", flush=True)
            env = {
                **os.environ,
                **{
                    name: "2"
                    for name in (
                        "OPENBLAS_NUM_THREADS",
                        "OMP_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS",
                    )
                },
            }
            before = time.monotonic()
            with (folder / "worker.log").open("x") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "quantlab.research.alpha158_rolling", "--fit", slot],
                    cwd=root,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                while process.poll() is None:
                    if (
                        time.monotonic() - budget.started > config["max_wakeup_seconds"]
                        or directory_bytes(out) > config["max_generated_bytes"]
                        or shutil.disk_usage("/mnt/d").free < config["reserve_host_D_bytes"]
                    ):
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    time.sleep(1)
            result_path = folder / "worker_result.json"
            if process.returncode == 0 and result_path.exists():
                summary = sealed_read(result_path)
                ledger.finish(slot, "completed", summary=summary, seconds=time.monotonic() - before)
                print(f"completed {slot} in {time.monotonic() - before:.1f}s", flush=True)
            else:
                ledger.finish(
                    slot,
                    "failed",
                    error=f"worker exit {process.returncode}; see immutable log",
                    seconds=time.monotonic() - before,
                )
                print(f"failed {slot}; attempt retained; no retry", flush=True)
                break
        results = [r for slot in plan["slots"] if (r := ledger.read(slot)) is not None]
        complete = len(results) == 6
        report = {
            "schema": "quantlab_alpha158_rolling_report_v1",
            "identity": plan["fingerprint"],
            "at": now(),
            "status": "complete" if complete else "checkpoint",
            "attempts": results,
            "cumulative_fit_attempts": len(results),
            "completed_fits": sum(r["status"] == "completed" for r in results),
            "paired": paired_diagnostics(out, plan, results),
            "generated_bytes": directory_bytes(out),
            "wakeup_seconds": time.monotonic() - budget.started,
            "child_peak_rss_bytes": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024,
            "performance_eligible": False,
            "execution_authority": False,
        }
        name = "report.json" if complete else f"checkpoint-{time.time_ns()}.json"
        if not (out / name).exists():
            atomic_seal(out / name, report)
        print(
            json.dumps(
                {k: report[k] for k in ("status", "cumulative_fit_attempts", "completed_fits")}
            ),
            flush=True,
        )


def load_report(out):
    report = sealed_read(out / "report.json")
    plan = sealed_read(out / "plan.json")
    if report["identity"] != plan["fingerprint"]:
        raise DataValidationError("rolling report/plan identity mismatch")
    if (
        report.get("performance_eligible") is not False
        or report.get("execution_authority") is not False
        or report.get("cumulative_fit_attempts") != 6
        or [r["slot"] for r in report["attempts"]] != plan["slots"]
    ):
        raise DataValidationError("invalid rolling report authority or attempt count")
    for result in report["attempts"]:
        actual = sealed_read(out / "fits" / result["slot"] / "result.json")
        if actual != result or actual["identity"] != plan["fingerprint"]:
            raise DataValidationError("rolling report differs from fit receipts")
    return report


def progress_report(out):
    if (out / "report.json").exists():
        return load_report(out)
    if not (out / "plan.json").exists():
        return None
    plan = sealed_read(out / "plan.json")
    rows = []
    for slot in plan["slots"]:
        folder = out / "fits" / slot
        if (folder / "result.json").exists():
            result = sealed_read(folder / "result.json")
        elif (folder / "started.json").exists():
            result = {
                "slot": slot,
                "status": "started_or_interrupted",
                **{
                    "identity": sealed_read(folder / "started.json")["identity"],
                },
            }
        else:
            continue
        if result["identity"] != plan["fingerprint"]:
            raise DataValidationError("rolling progress identity changed")
        rows.append(result)
    return {
        "status": "checkpoint",
        "attempts": rows,
        "cumulative_fit_attempts": len(rows),
        "completed_fits": sum(r["status"] == "completed" for r in rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fit", choices=[f"fold{i}_{k}" for i in (1, 2, 3) for k in ("ridge", "lightgbm")]
    )
    args = parser.parse_args()
    if args.fit:
        fit_worker(PROJECT_ROOT, args.fit)
    else:
        run()
