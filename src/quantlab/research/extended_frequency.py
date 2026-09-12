"""Resume one immutable annual replay; at most six jobs per bounded segment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import pandas as pd
import pyarrow as pa

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import resource_preflight
from quantlab.research.alpha158_store import Budget, atomic_seal
from quantlab.research.extended_frequency_metrics import policy_diagnostics
from quantlab.research.extended_frequency_protocol import (
    CONFIG,
    HEAVY,
    OUTPUT,
    RUNTIME,
    SOURCES,
    Ledger,
    folder_for,
    generated_bytes,
    monitor,
    now,
    prepare_plan,
)
from quantlab.research.extended_frequency_worker import validated_worker, worker
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_pilot_protocol import inherited_locks


def records(root, plan):
    config = plan["config"]
    fit = Ledger(root / OUTPUT, plan["fingerprint"], config["new_slots"], "fits")
    reuse = Ledger(root / OUTPUT, plan["fingerprint"], config["reuse_slots"], "reuse")
    result = []
    for slot in config["slot_order"]:
        ledger = reuse if slot in config["reuse_slots"] else fit
        value = ledger.read(slot)
        if value is not None:
            result.append(value)
    return result


def recover(root, plan, ledger, slot, previous):
    if previous["status"] == "completed":
        return True
    folder = folder_for(root, plan["config"], slot)
    if (folder / "result.json").exists():
        return False
    if (folder / "monitor.json").exists() and (folder / "worker_result.json").exists():
        watched = sealed_read(folder / "monitor.json")
        if watched["returncode"] == 0 and watched["violation"] is None:
            summary = validated_worker(root, plan, slot)
            ledger.finish(
                slot,
                "completed",
                summary=summary,
                monitor=watched,
                recovery="validated saved monitor/worker; no fit or prediction repeated",
            )
            return True
    ledger.finish(
        slot,
        "interrupted",
        error="consumed slot has no complete trusted worker/monitor receipt; no automatic retry",
    )
    return False


def diagnostic_report(root, plan, results):
    out = root / OUTPUT
    path = out / "diagnostics.json"
    if path.exists():
        saved = sealed_read(path)
        if saved["identity"] != plan["fingerprint"]:
            raise DataValidationError("extended diagnostics identity changed")
        verify_entries(out, saved["artifacts"])
        return saved
    config = plan["config"]
    if len(results) != 104 or any(value["status"] != "completed" for value in results):
        raise DataValidationError(
            "all 98 new fits and six reuse jobs required for complete diagnostics"
        )
    frames = {
        slot: pd.read_parquet(
            folder_for(root, config, slot) / "predictions.parquet", use_threads=False
        )
        for slot in config["slot_order"]
    }
    if sum(len(frame) for frame in frames.values()) != config["physical_prediction_rows"]:
        raise DataValidationError("extended physical prediction population changed")
    with Budget(out, config).watchdog():
        daily, weekly, monthly, paired = policy_diagnostics(frames, config)
        if (len(daily), len(weekly), len(monthly), len(paired)) != (968, 208, 48, 80):
            raise DataValidationError("extended complete diagnostic population changed")
        target = out / "daily_policies.parquet"
        if target.exists():
            if not pd.read_parquet(target, use_threads=False).equals(daily):
                raise DataValidationError(
                    "unsealed extended daily diagnostic differs; do not overwrite"
                )
        else:
            daily.to_parquet(target, index=False)
        return atomic_seal(
            path,
            {
                "identity": plan["fingerprint"],
                "weekly": weekly,
                "monthly": monthly,
                "paired": paired,
                "daily_rows": len(daily),
                "artifacts": {
                    target.name: {"sha256": _sha(target), "bytes": target.stat().st_size}
                },
                "performance_evidence": False,
                "execution_authority": False,
            },
        )


def publish(root, plan, segment_started, jobs, *, final=False):
    config, out = plan["config"], root / OUTPUT
    values = records(root, plan)
    new = [value for value in values if value["mode"] == "fits"]
    reused = [value for value in values if value["mode"] == "reuse"]
    failed = any(value["status"] != "completed" for value in values)
    complete = len(values) == 104 and not failed
    diagnostics = diagnostic_report(root, plan, values) if complete else None
    result = {
        "identity": plan["fingerprint"],
        "at": now(),
        "source_head": plan["code_head"],
        "status": "complete" if complete else "failed" if failed else "checkpoint",
        "attempts": values,
        "cumulative_fit_attempts": len(new),
        "actual_fit_invocations": sum(
            (folder_for(root, config, slot) / "invoked.json").exists()
            for slot in config["new_slots"]
        ),
        "completed_new_fits": sum(value["status"] == "completed" for value in new),
        "completed_reuse_jobs": sum(value["status"] == "completed" for value in reused),
        "failed_or_interrupted_jobs": sum(value["status"] != "completed" for value in values),
        "max_new_fit_attempts": 98,
        "original_reusable_models": 6,
        "next_unused_slot": next(
            (
                slot
                for slot in config["slot_order"]
                if not (folder_for(root, config, slot) / "started.json").exists()
            ),
            None,
        ),
        "segment_jobs": jobs,
        "segment_seconds": time.monotonic() - segment_started,
        "combined_peak_rss_bytes": max(
            [0, *[value.get("monitor", {}).get("combined_peak_rss_bytes", 0) for value in values]]
        ),
        "generated_bytes": generated_bytes(root),
        "prediction_rows_completed": sum(
            value.get("summary", {}).get("prediction_rows", 0)
            for value in values
            if value["status"] == "completed"
        ),
        "reused_prediction_rows": sum(
            value.get("summary", {}).get("reused_prediction_rows", 0)
            for value in values
            if value["status"] == "completed"
        ),
        "newly_scored_rows": sum(
            value.get("summary", {}).get("newly_scored_rows", 0)
            for value in values
            if value["status"] == "completed"
        ),
        "provider_calls": 0,
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
        "automatic_promotion": False,
        "diagnostics_fingerprint": None if diagnostics is None else diagnostics["fingerprint"],
    }
    if generated_bytes(root) + 2 * 1024**2 > config["max_generated_bytes"]:
        raise DataValidationError("extended report disk budget exhausted")
    folder = out / "checkpoints"
    folder.mkdir(exist_ok=True)
    saved = atomic_seal(folder / f"{time.time_ns()}.json", result)
    if final and (complete or failed):
        atomic_seal(out / "report.json", result)
    return saved


def run(root=PROJECT_ROOT):
    started = time.monotonic()
    (root / RUNTIME).mkdir(parents=True, exist_ok=True)
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    with inherited_locks([root / HEAVY, root / OUTPUT]) as descriptors:
        if (root / OUTPUT / "report.json").exists():
            return load_status(root)
        plan = prepare_plan(root)
        config = plan["config"]
        fit = Ledger(root / OUTPUT, plan["fingerprint"], config["new_slots"], "fits")
        reuse = Ledger(root / OUTPUT, plan["fingerprint"], config["reuse_slots"], "reuse")
        jobs = 0
        for slot in config["slot_order"]:
            ledger = reuse if slot in config["reuse_slots"] else fit
            previous = ledger.read(slot)
            if previous is not None:
                if not recover(root, plan, ledger, slot, previous):
                    return publish(root, plan, started, jobs, final=True)
                continue
            if (
                jobs >= config["max_jobs_per_segment"]
                or time.monotonic() - started
                >= config["max_wakeup_seconds"] - config["next_partition_time_reserve_seconds"]
            ):
                break
            resource_preflight(config)
            if generated_bytes(root) + 512 * 1024**2 > config["max_generated_bytes"]:
                raise DataValidationError(
                    "extended cumulative disk budget exhausted before next worker"
                )
            verify_entries(root, plan["code_files"])
            publish(root, plan, started, jobs)
            folder = ledger.start(slot)
            jobs += 1
            env = {
                **os.environ,
                **dict.fromkeys(
                    (
                        "OMP_NUM_THREADS",
                        "OPENBLAS_NUM_THREADS",
                        "MKL_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS",
                        "ARROW_NUM_THREADS",
                    ),
                    "2",
                ),
            }
            env["XDG_CACHE_HOME"] = str(root / RUNTIME / "cache")
            temp_dir = root / RUNTIME / "tmp"
            temp_dir.mkdir(exist_ok=True)
            env["TMPDIR"] = str(temp_dir)
            before = time.monotonic()
            print(f"starting {slot} ({ledger.mode})", flush=True)
            with (folder / "worker.log").open("x") as stream:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "quantlab.research.extended_frequency",
                        "--worker",
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
                watched = monitor(process, root, config, started)
            watched = atomic_seal(folder / "monitor.json", watched)
            if (
                watched["returncode"] == 0
                and watched["violation"] is None
                and (folder / "worker_result.json").exists()
            ):
                summary = validated_worker(root, plan, slot)
                ledger.finish(
                    slot,
                    "completed",
                    summary=summary,
                    monitor=watched,
                    seconds=time.monotonic() - before,
                )
                print(f"completed {slot}", flush=True)
                publish(root, plan, started, jobs)
            else:
                ledger.finish(
                    slot,
                    "failed",
                    monitor=watched,
                    seconds=time.monotonic() - before,
                    error="bounded extended worker failed; consumed slot retained, no retry",
                )
                break
        verify_entries(root, plan["sources"]["inputs"])
        verify_entries(root, plan["code_files"])
        return publish(root, plan, started, jobs, final=True)


def load_status(root):
    """Read the latest sealed snapshot; newer active work is deliberately not invented."""
    out = root / OUTPUT
    path = out / "report.json"
    if not path.exists():
        choices = sorted((out / "checkpoints").glob("*.json"))
        if not choices:
            return None
        path = choices[-1]
    report, plan = sealed_read(path), sealed_read(out / "plan.json")
    config = plan["config"]
    if (
        report["identity"] != plan["fingerprint"]
        or report["source_head"] != plan["code_head"]
        or _sha(root / CONFIG) != plan["sources"]["config_sha256"]
        or json.loads((root / SOURCES).read_text()) != plan["sources"]
    ):
        raise DataValidationError("extended status source/config changed")
    verify_historical_inputs(root, {"code_head": plan["code_head"], "inputs": plan["code_files"]})
    verify_entries(root, plan["sources"]["inputs"])
    fit, reuse = (
        Ledger(out, plan["fingerprint"], config["new_slots"], "fits"),
        Ledger(out, plan["fingerprint"], config["reuse_slots"], "reuse"),
    )
    values = report["attempts"]
    if [v["slot"] for v in values] != config["slot_order"][: len(values)]:
        raise DataValidationError("extended status omits or duplicates completed slots")
    for value in values:
        ledger = reuse if value["slot"] in config["reuse_slots"] else fit
        if value != ledger.read(value["slot"]):
            raise DataValidationError("extended sealed checkpoint receipt changed")
        if (
            value["status"] == "completed"
            and validated_worker(root, plan, value["slot"]) != value["summary"]
        ):
            raise DataValidationError("extended saved worker differs from reported summary")
    expected = {
        "cumulative_fit_attempts": sum(v["mode"] == "fits" for v in values),
        "actual_fit_invocations": sum(
            (folder_for(root, config, v["slot"]) / "invoked.json").exists()
            for v in values
            if v["mode"] == "fits"
        ),
        "completed_new_fits": sum(
            v["mode"] == "fits" and v["status"] == "completed" for v in values
        ),
        "completed_reuse_jobs": sum(
            v["mode"] == "reuse" and v["status"] == "completed" for v in values
        ),
        "failed_or_interrupted_jobs": sum(v["status"] != "completed" for v in values),
        "max_new_fit_attempts": 98,
        "original_reusable_models": 6,
        "provider_calls": 0,
    }
    for key in ("prediction_rows_completed", "reused_prediction_rows", "newly_scored_rows"):
        summary_key = "prediction_rows" if key == "prediction_rows_completed" else key
        expected[key] = sum(
            v.get("summary", {}).get(summary_key, 0) for v in values if v["status"] == "completed"
        )
    if (
        any(
            type(report.get(key)) is not int or report[key] != count
            for key, count in expected.items()
        )
        or not expected["actual_fit_invocations"] <= expected["cumulative_fit_attempts"] <= 98
    ):
        raise DataValidationError("extended status cumulative budget changed")
    next_slot = config["slot_order"][len(values)] if len(values) < 104 else None
    if report.get("next_unused_slot") != next_slot:
        raise DataValidationError("extended checkpoint next unused slot changed")
    failed = expected["failed_or_interrupted_jobs"] > 0
    if (report["status"] == "failed") != failed:
        raise DataValidationError("extended failure status differs from consumed receipts")
    if (
        any(
            report.get(key) is not False
            for key in ("performance_evidence", "execution_authority", "automatic_promotion")
        )
        or report.get("history_already_observed") is not True
    ):
        raise DataValidationError("extended status gained authority")
    if report["status"] == "complete":
        if expected["completed_new_fits"] != 98 or expected["completed_reuse_jobs"] != 6:
            raise DataValidationError("incomplete extended run presented as complete")
        diagnostics = sealed_read(out / "diagnostics.json")
        if (
            diagnostics["fingerprint"] != report["diagnostics_fingerprint"]
            or diagnostics["identity"] != plan["fingerprint"]
        ):
            raise DataValidationError("extended diagnostic receipt changed")
        verify_entries(out, diagnostics["artifacts"])
    elif (
        report["status"] not in ("checkpoint", "failed")
        or report["diagnostics_fingerprint"] is not None
    ):
        raise DataValidationError("invalid extended status/diagnostic claim")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--locks", nargs="*", type=int, default=[])
    args = parser.parse_args()
    if args.worker:
        worker(PROJECT_ROOT, args.worker, args.locks)
    else:
        result = run()
        print(
            json.dumps(
                {
                    k: result[k]
                    for k in (
                        "status",
                        "cumulative_fit_attempts",
                        "completed_new_fits",
                        "completed_reuse_jobs",
                    )
                }
            )
        )
