"""Complete only four unused original fit slots; never change failed or recovered parents."""

from __future__ import annotations

import argparse
import gc
import os
import subprocess
import sys
import time
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import init_qlib, resource_preflight
from quantlab.research.alpha158_store import Budget, atomic_seal, peak_rss_bytes
from quantlab.research.extended_completion_protocol import (
    OUTPUT,
    PARENT_REPORT,
    RECOVERY_REPORT,
    RUNTIME,
    SLOTS,
    CarryLedger,
    monitor,
    prepare_plan,
    validate_outputs,
    verify_context,
    verify_locks,
)
from quantlab.research.extended_frequency_worker import (
    fit_new,
    predict_union,
    utc,
    validate_saved_result,
)
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks, now


def folder_for(root, slot):
    if slot not in SLOTS:
        raise DataValidationError("slot outside remaining original authority")
    return root / OUTPUT / "fits" / slot


def validated_job(root, plan, slot):
    folder = folder_for(root, slot)
    book = CarryLedger(root / OUTPUT, plan["fingerprint"])
    book.read(slot)
    return validate_saved_result(root, plan, slot, folder)


def worker(root, slot, descriptors):
    import pyarrow as pa
    from threadpoolctl import threadpool_limits

    verify_locks(root, descriptors)
    plan = sealed_read(root / OUTPUT / "plan.json")
    c = plan["config"]
    folder = folder_for(root, slot)
    book = CarryLedger(root / OUTPUT, plan["fingerprint"])
    reservation = book.read(slot)
    if reservation is None or (folder / "result.json").exists():
        raise DataValidationError("completion worker requires one unused durable reservation")
    if c["model_specs"][slot]["reuse_slot"] is not None:
        raise DataValidationError("completion cannot acquire another old-model recovery")
    if any(
        (folder / n).exists()
        for n in (
            "invoked.json",
            "model.pkl",
            "preprocessing.json",
            "worker_result.json",
            "predictions.parquet",
        )
    ):
        raise DataValidationError("consumed completion worker cannot restart")
    verify_context(root, plan)
    budget = Budget(root / OUTPUT, plan["contract"])
    budget.check()
    md = sealed_read(root / c["metadata_root"] / "metadata.json")
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    init_qlib(folder / "qlib", experiment_name="alpha158_extended_frequency")
    spec = c["model_specs"][slot]
    with budget.watchdog(), threadpool_limits(limits=2):
        model, scaler, fitted = fit_new(root, plan, spec, folder, md)
        prediction = predict_union(root, c, spec, folder, model, scaler, md)
        verify_context(root, plan)
        validate_outputs(root, plan["contract"])
        result = {
            "identity": plan["fingerprint"],
            "slot": slot,
            "mode": "fits",
            "kind": spec["model"],
            **{
                k: spec[k]
                for k in (
                    "train_start",
                    "train_end",
                    "train_rows",
                    "train_sha256",
                    "replay_prediction_start",
                )
            },
            "derived_at_utc": now(),
            "peak_rss_bytes": peak_rss_bytes(),
            "ridge_n_iter": getattr(model, "n_iter_", None),
            "boosted_rounds": model.model.current_iteration()
            if spec["model"] == "lightgbm"
            else None,
            "history_already_observed": True,
            "performance_evidence": False,
            "execution_authority": False,
            **fitted,
            **prediction,
        }
        atomic_seal(folder / "worker_result.json", result)
        del model
        gc.collect()


def invocation_count(root, plan, values):
    count = 0
    for value in values:
        folder = folder_for(root, value["slot"])
        if (folder / "invoked.json").exists():
            invoked = sealed_read(folder / "invoked.json")
            started = sealed_read(folder / "started.json")
            if (
                invoked["identity"] != plan["fingerprint"]
                or invoked["slot"] != value["slot"]
                or utc(invoked["at"]) < utc(started["started_at"])
            ):
                raise DataValidationError("completion invocation is not bound to its reservation")
            count += 1
    return count


def publish(root, plan, started):
    book = CarryLedger(root / OUTPUT, plan["fingerprint"])
    values = [v for slot in SLOTS if (v := book.read(slot)) is not None]
    completed = [v for v in values if v["status"] == "completed"]
    failed = any(v["status"] != "completed" for v in values)
    status = "failed" if failed else "complete" if len(completed) == 4 else "checkpoint"
    invoked = invocation_count(root, plan, values)
    rows = sum(v["summary"]["prediction_rows"] for v in completed)
    result = {
        "at": now(),
        "segment_started_at": started,
        "identity": plan["fingerprint"],
        "source_head": plan["code_head"],
        "status": status,
        "parent_status": "failed",
        "parent_report": PARENT_REPORT,
        "recovery_report": RECOVERY_REPORT,
        "attempts": values,
        "consumed_completion_slots": len(values),
        "completion_fit_invocations": invoked,
        "completed_completion_models": len(completed),
        "global_fit_ceiling": 98,
        "global_consumed_fit_reservations": 94 + len(values),
        "global_actual_fit_invocations": 94 + invoked,
        "global_completed_new_models": 94 + len(completed),
        "recovery_fit_invocations": 0,
        "combined_model_references": 100 + len(completed),
        "required_model_references": 104,
        "prediction_rows": rows,
        "combined_prediction_rows": 4259564 + rows,
        "remaining_unconsumed_fit_slots": 4 - len(values),
        "complete_annual_comparison": False,
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
        "provider_calls": 0,
    }
    target = (
        root
        / OUTPUT
        / (
            "report.json"
            if status in ("complete", "failed")
            else f"checkpoints/{len(values):02d}.json"
        )
    )
    if target.exists():
        previous = sealed_read(target)
        skip = {"fingerprint", "at", "segment_started_at"}
        if {k: v for k, v in previous.items() if k not in skip} != {
            k: v for k, v in result.items() if k not in skip
        }:
            raise DataValidationError("sealed completion checkpoint differs from durable evidence")
        return previous
    return atomic_seal(target, result)


def run(root):
    out = root / OUTPUT
    with inherited_locks([root / HEAVY, out]) as descriptors:
        if (out / "report.json").exists():
            return load_report(root)
        plan = prepare_plan(root)
        verify_context(root, plan)
        book = CarryLedger(out, plan["fingerprint"])
        budget = Budget(out, plan["contract"])
        stamp = now()
        before = time.monotonic()
        jobs = 0
        runtime = root / RUNTIME
        runtime.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                k: "2"
                for k in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                    "ARROW_NUM_THREADS",
                )
            }
        )
        env.update(XDG_CACHE_HOME=str(runtime / "cache"), TMPDIR=str(runtime / "tmp"))
        (runtime / "tmp").mkdir(exist_ok=True)
        for slot in SLOTS:
            previous = book.read(slot)
            if previous is not None:
                if previous["status"] != "completed":
                    if not (folder_for(root, slot) / "result.json").exists():
                        book.finish(
                            slot,
                            "interrupted",
                            error="consumed completion reservation retained; no automatic retry",
                        )
                    return publish(root, plan, stamp)
                if validated_job(root, plan, slot) != previous["summary"]:
                    raise DataValidationError("completed remaining model changed")
                continue
            if jobs >= 4 or not budget.can_start():
                break
            resource_preflight(plan["contract"])
            budget.check(projected_bytes=64 * 1024**2)
            verify_context(root, plan)
            validate_outputs(root, plan["contract"])
            publish(root, plan, stamp)
            folder = book.start(slot)
            jobs += 1
            print(f"global attempt {95 + SLOTS.index(slot)}/98: {slot}", flush=True)
            try:
                command = [
                    sys.executable,
                    "-m",
                    "quantlab.research.extended_completion",
                    "--worker",
                    slot,
                ]
                for fd in descriptors:
                    command += ["--lock-fd", str(fd)]
                with (folder / "worker.log").open("xb") as log:
                    process = subprocess.Popen(
                        command,
                        cwd=root,
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        pass_fds=tuple(descriptors),
                        start_new_session=True,
                    )
                    observed = monitor(process, root, plan["contract"], before)
                atomic_seal(folder / "monitor.json", observed)
                if observed["returncode"] != 0 or observed["violation"] is not None:
                    raise DataValidationError(f"completion worker stopped: {observed}")
                summary = validated_job(root, plan, slot)
                verify_context(root, plan)
                validate_outputs(root, plan["contract"])
            except Exception as exc:
                book.finish(slot, "failed", error=f"{type(exc).__name__}: {exc}")
                return publish(root, plan, stamp)
            book.finish(slot, "completed", summary=summary, monitor=observed)
            publish(root, plan, stamp)
        verify_context(root, plan)
        validate_outputs(root, plan["contract"])
        return publish(root, plan, stamp)


def load_report(root):
    out = root / OUTPUT
    path = out / "report.json"
    if not path.exists():
        paths = sorted((out / "checkpoints").glob("*.json"))
        if not paths:
            return None
        path = paths[-1]
    r = sealed_read(path)
    p = sealed_read(out / "plan.json")
    verify_context(root, p, historical=True)
    validate_outputs(root, p["contract"])
    if (
        r["identity"] != p["fingerprint"]
        or r["source_head"] != p["code_head"]
        or r["parent_status"] != "failed"
        or r["parent_report"] != PARENT_REPORT
        or r["recovery_report"] != RECOVERY_REPORT
    ):
        raise DataValidationError("completion report lineage changed")
    values = r["attempts"]
    book = CarryLedger(out, p["fingerprint"])
    if [v["slot"] for v in values] != SLOTS[: len(values)]:
        raise DataValidationError("completion report skips original remaining slots")
    if r["status"] in ("complete", "failed") and {v["slot"] for v in values} != {
        x.name for x in book.base.iterdir()
    }:
        raise DataValidationError("terminal completion report hides a reservation")
    for v in values:
        if v != book.read(v["slot"]):
            raise DataValidationError("completion durable receipt changed")
        if v["status"] == "completed":
            monitor_receipt = sealed_read(folder_for(root, v["slot"]) / "monitor.json")
            if (
                validated_job(root, p, v["slot"]) != v["summary"]
                or v["monitor"] != {k: a for k, a in monitor_receipt.items() if k != "fingerprint"}
                or monitor_receipt["returncode"] != 0
                or monitor_receipt["violation"] is not None
            ):
                raise DataValidationError("completion model/monitor evidence changed")
    completed = [v for v in values if v["status"] == "completed"]
    invoked = invocation_count(root, p, values)
    rows = sum(v["summary"]["prediction_rows"] for v in completed)
    expected = {
        "consumed_completion_slots": len(values),
        "completion_fit_invocations": invoked,
        "completed_completion_models": len(completed),
        "global_fit_ceiling": 98,
        "global_consumed_fit_reservations": 94 + len(values),
        "global_actual_fit_invocations": 94 + invoked,
        "global_completed_new_models": 94 + len(completed),
        "recovery_fit_invocations": 0,
        "combined_model_references": 100 + len(completed),
        "required_model_references": 104,
        "prediction_rows": rows,
        "combined_prediction_rows": 4259564 + rows,
        "remaining_unconsumed_fit_slots": 4 - len(values),
        "provider_calls": 0,
    }
    if any(type(r.get(k)) is not int or r[k] != v for k, v in expected.items()):
        raise DataValidationError("completion report global budget/population changed")
    status = (
        "failed"
        if any(v["status"] != "completed" for v in values)
        else "complete"
        if len(completed) == 4
        else "checkpoint"
    )
    if r["status"] != status or (status == "complete" and (rows, invoked) != (60998, 4)):
        raise DataValidationError("completion outcome or expected prediction population changed")
    if r["history_already_observed"] is not True or any(
        r[k] is not False
        for k in ("complete_annual_comparison", "performance_evidence", "execution_authority")
    ):
        raise DataValidationError("completion gained financial/annual authority")
    return r


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--lock-fd", type=int, action="append", default=[])
    args = parser.parse_args()
    if args.worker:
        worker(Path.cwd(), args.worker, args.lock_fd)
    else:
        import json

        r = run(Path.cwd())
        print(
            json.dumps(
                {
                    k: r[k]
                    for k in (
                        "status",
                        "completed_completion_models",
                        "global_actual_fit_invocations",
                        "combined_model_references",
                        "combined_prediction_rows",
                    )
                }
            )
        )
