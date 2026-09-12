"""Six prediction-only derived jobs; never restart or overwrite the failed parent."""

from __future__ import annotations

import gc
import hashlib
import os
import time

import numpy as np
import pyarrow as pa

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import resource_preflight
from quantlab.research.alpha158_store import Budget, atomic_seal, peak_rss_bytes
from quantlab.research.extended_frequency_worker import (
    load_reuse,
    predict_union,
    validate_saved_result,
)
from quantlab.research.extended_recovery_protocol import (
    OUTPUT,
    PARENT_REPORT,
    RUNTIME,
    SLOTS,
    check_carried_budget,
    check_outputs,
    ledger,
    prepare_plan,
    publish,
    verify_context,
)
from quantlab.research.round2_dataset import sealed_read
from quantlab.research.weekly_pilot_data import membership
from quantlab.research.weekly_pilot_protocol import HEAVY, inherited_locks, now


def derive_one(root, plan, slot, budget):
    c = plan["config"]
    spec = c["model_specs"][slot]
    if slot not in SLOTS or spec["reuse_slot"] is None:
        raise DataValidationError("recovery has no new-model authority")
    folder = root / OUTPUT / "reuse" / slot
    started = sealed_read(folder / "started.json")
    if (
        started["identity"] != plan["fingerprint"]
        or started["slot"] != slot
        or started["mode"] != "reuse"
    ):
        raise DataValidationError("derived job identity changed")
    if any(
        (folder / name).exists()
        for name in (
            "model.pkl",
            "model_reference.json",
            "result.json",
            "invoked.json",
            "preprocessing.json",
            "predictions.parquet",
            "worker_result.json",
            "groups.json",
        )
    ):
        raise DataValidationError("consumed derived prediction job cannot run again")
    md = sealed_read(root / c["metadata_root"] / "metadata.json")
    groups = []

    def observe(ordinal, part, existing, x):
        budget.check(projected_bytes=1024**2)
        check_outputs(root, plan)
        one = {"metadata_batch": md["counts"][ordinal]["batch"], "partition_ordinal": ordinal}
        for name, mask in (("existing", existing), ("new", ~existing)):
            one[name] = {
                "rows": int(mask.sum()),
                "identity_sha256": membership(part.loc[mask]),
                "float32_feature_sha256": hashlib.sha256(
                    np.ascontiguousarray(x[mask]).tobytes()
                ).hexdigest(),
            }
        groups.append(one)

    model, scale, fitted = load_reuse(root, plan, spec, folder)
    prediction = predict_union(root, c, spec, folder, model, scale, md, batch_observer=observe)
    grouping = atomic_seal(
        folder / "groups.json",
        {
            "identity": plan["fingerprint"],
            "slot": slot,
            "batch_geometry": plan["contract"]["batch_geometry"],
            "partitions": groups,
            "additional_fit_attempts": 0,
            "parent_failed_report": PARENT_REPORT,
        },
    )
    result = {
        "identity": plan["fingerprint"],
        "slot": slot,
        "mode": "reuse",
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
        **prediction,
        **fitted,
        "groups_fingerprint": grouping["fingerprint"],
        "derived_at_utc": now(),
        "peak_rss_bytes": peak_rss_bytes(),
        "ridge_n_iter": getattr(model, "n_iter_", None),
        "boosted_rounds": model.model.current_iteration() if spec["model"] == "lightgbm" else None,
        "history_already_observed": True,
        "performance_evidence": False,
        "execution_authority": False,
    }
    result = atomic_seal(folder / "worker_result.json", result)
    del model
    gc.collect()
    return result


def validated_job(root, plan, slot):
    folder = root / OUTPUT / "reuse" / slot
    summary = validate_saved_result(root, plan, slot, folder)
    group = sealed_read(folder / "groups.json")
    if (
        group["fingerprint"] != summary["groups_fingerprint"]
        or group["identity"] != plan["fingerprint"]
        or group["slot"] != slot
    ):
        raise DataValidationError("recovery group receipt identity changed")
    if (
        group["batch_geometry"] != plan["contract"]["batch_geometry"]
        or type(group["additional_fit_attempts"]) is not int
        or group["additional_fit_attempts"] != 0
        or group["parent_failed_report"] != PARENT_REPORT
    ):
        raise DataValidationError("recovery group computation authority changed")
    values = group["partitions"]
    metadata = sealed_read(root / plan["config"]["metadata_root"] / "metadata.json")
    for value in values:
        ordinal = value["partition_ordinal"]
        if (
            type(ordinal) is not int
            or not 0 <= ordinal < len(metadata["counts"])
            or value["metadata_batch"] != metadata["counts"][ordinal]["batch"]
        ):
            raise DataValidationError("recovery metadata partition identity changed")
    if [v["partition_ordinal"] for v in values] != sorted({v["partition_ordinal"] for v in values}):
        raise DataValidationError("recovery partitions duplicated or unordered")
    for key, target in (
        ("existing", summary["reused_prediction_rows"]),
        ("new", summary["newly_scored_rows"]),
    ):
        if sum(v[key]["rows"] for v in values) != target:
            raise DataValidationError("recovery group population changed")
        for v in values:
            if type(v[key]["rows"]) is not int or v[key]["rows"] < 0:
                raise DataValidationError("recovery group row count invalid")
            for name in ("identity_sha256", "float32_feature_sha256"):
                value = v[key][name]
                if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                    raise DataValidationError("recovery group hash invalid")
    return summary


def run(root):
    from threadpoolctl import threadpool_limits

    out = root / OUTPUT
    with inherited_locks([root / HEAVY, out]), threadpool_limits(limits=2):
        if (out / "report.json").exists():
            return load_report(root)
        plan = prepare_plan(root)
        verify_context(root, plan)
        book = ledger(root, plan)
        budget = Budget(out, plan["contract"])
        started = now()
        jobs = 0
        pa.set_cpu_count(2)
        pa.set_io_thread_count(2)
        runtime = root / RUNTIME
        runtime.mkdir(parents=True, exist_ok=True)
        os.environ["XDG_CACHE_HOME"] = str(runtime / "cache")
        os.environ["TMPDIR"] = str(runtime / "tmp")
        (runtime / "tmp").mkdir(exist_ok=True)
        with budget.watchdog():
            for slot in SLOTS:
                previous = book.read(slot)
                if previous is not None:
                    if previous["status"] != "completed":
                        if not (out / "reuse" / slot / "result.json").exists():
                            book.finish(
                                slot,
                                "interrupted",
                                error="unsealed derived job retained; no automatic retry",
                            )
                        return publish(root, plan, started)
                    if validated_job(root, plan, slot) != previous["summary"]:
                        raise DataValidationError("completed derived result changed")
                    continue
                if jobs >= 6 or not budget.can_start():
                    break
                resource_preflight(plan["contract"])
                budget.check(projected_bytes=64 * 1024**2)
                check_outputs(root, plan)
                verify_context(root, plan)
                publish(root, plan, started)
                book.start(slot)
                jobs += 1
                before = time.monotonic()
                print(f"deriving {slot} without fitting", flush=True)
                try:
                    summary = derive_one(root, plan, slot, budget)
                    if validated_job(root, plan, slot) != summary:
                        raise DataValidationError("derived result differs from saved evidence")
                    verify_context(root, plan)
                    check_outputs(root, plan)
                except Exception as exc:
                    book.finish(
                        slot,
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        seconds=time.monotonic() - before,
                    )
                    return publish(root, plan, started)
                book.finish(slot, "completed", summary=summary, seconds=time.monotonic() - before)
                publish(root, plan, started)
            verify_context(root, plan)
            check_outputs(root, plan)
            return publish(root, plan, started)


def load_report(root):
    out = root / OUTPUT
    path = out / "report.json"
    if not path.exists():
        paths = sorted((out / "checkpoints").glob("*.json"))
        if not paths:
            return None
        path = paths[-1]
    report = sealed_read(path)
    plan = sealed_read(out / "plan.json")
    verify_context(root, plan, historical=True)
    check_outputs(root, plan)
    if (
        report["identity"] != plan["fingerprint"]
        or report["source_head"] != plan["code_head"]
        or report["parent_report"] != PARENT_REPORT
        or report["parent_status"] != "failed"
    ):
        raise DataValidationError("derived recovery report lineage changed")
    check_carried_budget(report["carried_budget"])
    book = ledger(root, plan)
    values = report["attempts"]
    if [v["slot"] for v in values] != SLOTS[: len(values)]:
        raise DataValidationError("recovery report skips or duplicates jobs")
    for value in values:
        if value != book.read(value["slot"]):
            raise DataValidationError("recovery checkpoint receipt changed")
        if (
            value["status"] == "completed"
            and validated_job(root, plan, value["slot"]) != value["summary"]
        ):
            raise DataValidationError("recovery completed artifact changed")
    complete = [v for v in values if v["status"] == "completed"]
    expected = {
        "completed_recovery_jobs": len(complete),
        "consumed_recovery_jobs": len(values),
        "combined_model_references": 94 + len(complete),
        "required_model_references": 104,
        "prediction_rows": sum(v["summary"]["prediction_rows"] for v in complete),
        "reused_rows": sum(v["summary"]["reused_prediction_rows"] for v in complete),
        "newly_scored_rows": sum(v["summary"]["newly_scored_rows"] for v in complete),
        "additional_fit_attempts": 0,
        "provider_calls": 0,
    }
    if any(type(report.get(k)) is not int or report[k] != v for k, v in expected.items()):
        raise DataValidationError("recovery report counters changed")
    failed = any(v["status"] != "completed" for v in values)
    status = "failed" if failed else "complete" if len(complete) == 6 else "checkpoint"
    if report["status"] != status or (
        status == "complete"
        and (report["prediction_rows"], report["reused_rows"], report["newly_scored_rows"])
        != (314804, 253806, 60998)
    ):
        raise DataValidationError("recovery completion/population claim invalid")
    if (
        any(
            report.get(k) is not False
            for k in ("performance_evidence", "execution_authority", "complete_annual_comparison")
        )
        or report.get("history_already_observed") is not True
    ):
        raise DataValidationError("recovery report gained financial authority")
    return report
