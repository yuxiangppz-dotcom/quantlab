"""Bounded resumable native Alpha158 history staging; no labels, providers or fits."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_history_checks import causal_sample, frozen_overlap
from quantlab.research.alpha158_history_data import (
    discover_inventory,
    ingest_month,
    map_history_inputs,
)
from quantlab.research.alpha158_native import (
    CONTRACT_SHA,
    KEYS,
    RAW_FIELDS,
    apply_evidence_mask,
    evaluate_native,
    load_contract,
    verify_library,
    write_binary_cache,
)
from quantlab.research.alpha158_store import (
    Budget,
    Partitions,
    atomic_seal,
    directory_bytes,
    exclusive_job,
    peak_rss_bytes,
)
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries

CONFIG_PATH = "config/alpha158_staging_v1.json"
CONFIG_SHA = "e25fd138b4fdfccd504d80a76ac71301f2f72f7f7214a01253618430d5750a59"
OUTPUT_DIRECTORY = "data/products/alpha158_staging/alpha158_history_20260911"


def load_config(root=PROJECT_ROOT):
    raw = (root / CONFIG_PATH).read_bytes()
    if hashlib.sha256(raw).hexdigest() != CONFIG_SHA:
        raise DataValidationError("predeclared historical staging contract changed")
    config = json.loads(raw)
    if config["native_contract_sha256"] != CONTRACT_SHA:
        raise DataValidationError("historical staging mapping contract mismatch")
    return config


def verify_sources(root, inventory):
    verify_entries(root, inventory["source_files"])
    for item in inventory["sources"]:
        if item["status"] == "missing" and (root / item["path"]).exists():
            raise DataValidationError("missing historical source appeared")


def checked_plan(out, payload):
    path = out / "plan.json"
    if path.exists():
        plan = sealed_read(path)
        if {k: v for k, v in plan.items() if k != "fingerprint"} != payload:
            raise DataValidationError("staging resume source/code/config/runtime identity mismatch")
        return plan
    return atomic_seal(path, payload)


def partition_folder(root, out, receipt):
    path = (root / receipt["result"]["folder"]).resolve()
    if not path.is_relative_to(out.resolve()):
        raise DataValidationError("raw partition path escaped staging directory")
    return path


def compute_batch(
    root, out, folder, number, codes, inventory, config, native_contract, raw_receipts
):
    parts = []
    for receipt in raw_receipts:
        path = partition_folder(root, out, receipt) / f"batch-{number:04d}.parquet"
        if str(number) in receipt["result"]["batch_rows"]:
            frame = pd.read_parquet(path, use_threads=False)
            if len(frame) != receipt["result"]["batch_rows"][str(number)]:
                raise DataValidationError("raw staging partition row count changed")
            parts.append(frame)
        elif path.exists():
            raise DataValidationError("unrecorded raw staging partition")
    raw = (
        pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=[*KEYS, *RAW_FIELDS])
    )
    mapped, evidence = map_history_inputs(raw, inventory["sessions"], codes, inventory)
    del raw, parts
    write_binary_cache(mapped, folder / "native_cache", inventory["sessions"])
    native = evaluate_native(mapped, folder / "native_cache", native_contract)
    usable, coverage = apply_evidence_mask(native, mapped, native_contract)
    overlap = frozen_overlap(root, native, usable, codes, config, native_contract)
    sample = causal_sample(mapped, folder / "causal", native_contract)
    for name, frame in (
        ("input_evidence", evidence),
        ("mapped_fields", mapped),
        ("native_features", native),
        ("usable_features", usable),
    ):
        frame.to_parquet(folder / f"{name}.parquet", index=False)
    target = usable.trade_date.between(config["start"], config["end"])
    ev = evidence.loc[target]
    names = [item["name"] for item in native_contract["features"]]
    if not usable.loc[~evidence.lifecycle_active.fillna(False), names].isna().all().all():
        raise DataValidationError("inactive/unknown code acquired usable factor evidence")
    target_coverage = [
        {
            **item,
            "target_native_finite": int(np.isfinite(native.loc[target, item["name"]]).sum()),
            "target_usable_rows": int(np.isfinite(usable.loc[target, item["name"]]).sum()),
        }
        for item in coverage
    ]
    years = []
    complete = np.isfinite(usable[names]).all(axis=1)
    for year in sorted(usable.loc[target].trade_date.dt.year.unique()):
        selected = target & usable.trade_date.dt.year.eq(year)
        years.append(
            {
                "year": int(year),
                "grid_rows": int(selected.sum()),
                "active_rows": int(evidence.loc[selected].lifecycle_active.fillna(False).sum()),
                "observed_rows": int(evidence.loc[selected].daily_observed.sum()),
                "all158_usable_rows": int((complete & selected).sum()),
            }
        )
    return {
        "number": number,
        "codes": codes,
        "grid_rows_with_warmup": len(mapped),
        "target_grid_rows": int(target.sum()),
        "target_observed_rows": int(ev.daily_observed.sum()),
        "target_active_rows": int(ev.lifecycle_active.fillna(False).sum()),
        "target_unknown_lifecycle_rows": int((~ev.lifecycle_known).sum()),
        "target_all158_usable_rows": int((complete & target).sum()),
        "target_exclusions": ev.exclusion_reason.value_counts().to_dict(),
        "features": target_coverage,
        "years": years,
        "sample": sample,
        "frozen_overlap": overlap,
        "folder": folder.relative_to(root).as_posix(),
        "new_fit_attempts": 0,
    }


def aggregate(plan, receipts):
    counts = {
        name: 0
        for name in (
            "target_grid_rows",
            "target_observed_rows",
            "target_active_rows",
            "target_unknown_lifecycle_rows",
            "target_all158_usable_rows",
        )
    }
    names = [x["name"] for x in load_contract()["features"]]
    features = {
        name: {"name": name, "target_native_finite": 0, "target_usable_rows": 0} for name in names
    }
    exclusions, years, overlaps = {}, {}, []
    for receipt in receipts:
        result = receipt["result"]
        if result["new_fit_attempts"] != 0 or not all(
            result["sample"][x]
            for x in ("provider_parity", "prefix_invariant", "future_and_label_invariant")
        ):
            raise DataValidationError("invalid completed native partition evidence")
        for key in counts:
            counts[key] += result[key]
        for key, value in result["target_exclusions"].items():
            exclusions[key] = exclusions.get(key, 0) + value
        for item in result["features"]:
            for key in ("target_native_finite", "target_usable_rows"):
                features[item["name"]][key] += item[key]
        for item in result["years"]:
            annual = years.setdefault(
                item["year"],
                {
                    "year": item["year"],
                    "grid_rows": 0,
                    "active_rows": 0,
                    "observed_rows": 0,
                    "all158_usable_rows": 0,
                },
            )
            for key in annual.keys() - {"year"}:
                annual[key] += item[key]
        if result["frozen_overlap"]:
            overlaps.append(result["frozen_overlap"])
    return {
        **counts,
        "features": list(features.values()),
        "target_exclusions": exclusions,
        "years": list(years.values()),
        "frozen_overlaps": overlaps,
        "completed_batches": len(receipts),
        "expected_batches": len(plan["batches"]),
    }


def run_staging(root=PROJECT_ROOT, *, progress=print):
    config, native_contract = load_config(root), load_contract(root)
    out = root / OUTPUT_DIRECTORY
    with exclusive_job(out):
        if (out / "report.json").exists():
            return load_staging_report(out, full_verify=True)
        budget = Budget(out, config)
        budget.check(projected_memory=512 * 1024**2)
        with budget.watchdog():
            inventory = sealed_read(out / "inventory.json")
            if inventory["fingerprint"] != config["inventory_fingerprint"] or inventory[
                "scope"
            ] != {key: config[key] for key in ("start", "end", "warmup_sessions")}:
                raise DataValidationError("historical inventory differs from predeclared sources")
            verify_sources(root, inventory)
            binding = InputBinding(root)
            head = code_binding(root, binding)
            runtime = verify_library(native_contract)
            from threadpoolctl import threadpool_info

            pools = threadpool_info()
            if any(pool["num_threads"] > config["max_compute_threads"] for pool in pools):
                raise DataValidationError("historical staging computation thread limit exceeded")
            codes = inventory["instruments"]
            batches = [
                codes[i : i + config["batch_codes"]]
                for i in range(0, len(codes), config["batch_codes"])
            ]
            plan = checked_plan(
                out,
                {
                    "config": config,
                    "inventory_fingerprint": inventory["fingerprint"],
                    "code_head": head,
                    "code_files": binding.entries,
                    "qlib_runtime": runtime,
                    "batches": batches,
                },
            )
            store = Partitions(out, plan["fingerprint"], budget, config["max_partition_attempts"])
            try:
                months = sorted({day[:7] for day in inventory["sessions"]})
                raw_receipts, native_receipts = [], []
                for month in months:
                    row_upper = sum(
                        x["rows"]
                        for x in inventory["sources"]
                        if x["kind"] == "daily" and x["date"].startswith(month)
                    )
                    receipt = store.execute(
                        f"raw-{month}",
                        row_upper * 1024 + 4 * 1024**2,
                        512 * 1024**2,
                        lambda folder, month=month: ingest_month(
                            root, folder, month, inventory, config
                        ),
                    )
                    if receipt is None:
                        break
                    raw_receipts.append(receipt)
                    if len(raw_receipts) % 12 == 0:
                        progress(
                            f"source staging {len(raw_receipts)}/{len(months)} months", flush=True
                        )
                if len(raw_receipts) == len(months):
                    for number, batch in enumerate(batches):
                        rows = len(batch) * len(inventory["sessions"])
                        projected_bytes = rows * (158 * 2 * 8 + 300) + 32 * 1024**2
                        projected_memory = rows * 158 * 32 + 256 * 1024**2
                        receipt = store.execute(
                            f"features-{number:04d}",
                            projected_bytes,
                            projected_memory,
                            lambda folder, number=number, batch=batch: compute_batch(
                                root,
                                out,
                                folder,
                                number,
                                batch,
                                inventory,
                                config,
                                native_contract,
                                raw_receipts,
                            ),
                        )
                        if receipt is None:
                            break
                        native_receipts.append(receipt)
                        count = sum(len(r["result"]["codes"]) for r in native_receipts)
                        progress(
                            f"native Alpha158 {len(native_receipts)}/{len(batches)} batches; "
                            f"{count} codes",
                            flush=True,
                        )
                summary = aggregate(plan, native_receipts)
                complete = len(native_receipts) == len(batches)
                verify_sources(root, inventory)
                binding.check()
                if verify_library(native_contract) != runtime:
                    raise DataValidationError("native runtime changed during staging")
                if complete and sorted(
                    code for x in summary["frozen_overlaps"] for code in x["codes"]
                ) != sorted(config["sample_codes"]):
                    raise DataValidationError("frozen overlap coverage incomplete")
                for receipt in raw_receipts + native_receipts:
                    verify_entries(out, receipt["artifacts"])
                used = budget.check()
                elapsed = time.monotonic() - budget.started
                prior_seconds = sum(
                    sealed_read(path).get("wake_seconds", sealed_read(path).get("seconds", 0))
                    for kind in ("wakeups", "failures", "interruptions")
                    for path in (out / kind).glob("*.json")
                )
                payload = {
                    "status": "complete" if complete else "paused_at_checkpoint",
                    "at": datetime.now(UTC).isoformat(),
                    "plan_fingerprint": plan["fingerprint"],
                    "inventory_fingerprint": inventory["fingerprint"],
                    "config": config,
                    "code_head": head,
                    "instrument_count": len(codes),
                    "sessions_with_warmup": len(inventory["sessions"]),
                    "raw_months_completed": len(raw_receipts),
                    "raw_months_expected": len(months),
                    **summary,
                    "generated_bytes_before_receipt": used,
                    "peak_rss_bytes": max(
                        [
                            peak_rss_bytes(),
                            *[r["peak_rss_bytes"] for r in raw_receipts + native_receipts],
                        ]
                    ),
                    "wake_seconds": elapsed,
                    "cumulative_wake_seconds": prior_seconds + elapsed,
                    "partition_attempts": len(list((out / "attempts").glob("*/*/started.json"))),
                    "computation_pools": pools,
                    "new_fit_attempts": 0,
                    "performance_eligible": False,
                    "execution_authority": False,
                    "strategy_promoted": False,
                    "fresh_forward_evidence": False,
                    "source_and_runtime_unchanged": True,
                    "partition_receipts": {
                        r["name"]: r["fingerprint"] for r in raw_receipts + native_receipts
                    },
                }
                event = out / "wakeups" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json"
                atomic_seal(event, payload)
                if complete:
                    return atomic_seal(out / "report.json", payload)
                return sealed_read(event)
            except BaseException as exc:
                atomic_seal(
                    out / "failures" / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}.json",
                    {
                        "type": type(exc).__name__,
                        "error": str(exc),
                        "plan_fingerprint": plan["fingerprint"],
                        "peak_rss_bytes": peak_rss_bytes(),
                        "wake_seconds": time.monotonic() - budget.started,
                        "generated_bytes": directory_bytes(out),
                        "new_fit_attempts": 0,
                    },
                )
                raise


def load_staging_report(out: Path, *, full_verify=False):
    report = sealed_read(out / "report.json")
    plan = sealed_read(out / "plan.json")
    inventory = sealed_read(out / "inventory.json")
    if (
        report["config"] != load_config()
        or plan["config"] != report["config"]
        or inventory["fingerprint"] != report["config"]["inventory_fingerprint"]
        or report["inventory_fingerprint"] != inventory["fingerprint"]
        or report["plan_fingerprint"] != plan["fingerprint"]
        or report["status"] != "complete"
        or report["new_fit_attempts"] != 0
        or report["completed_batches"] != report["expected_batches"]
        or report["expected_batches"] != len(plan["batches"])
        or report["instrument_count"] != len(inventory["instruments"])
        or report["raw_months_completed"] != report["raw_months_expected"]
        or not (
            0
            <= report["target_all158_usable_rows"]
            <= report["target_active_rows"]
            <= report["target_grid_rows"]
        )
    ):
        raise DataValidationError("historical staging report is incomplete or has another identity")
    for name in (
        "performance_eligible",
        "execution_authority",
        "strategy_promoted",
        "fresh_forward_evidence",
    ):
        if report.get(name) is not False:
            raise DataValidationError("staging coverage cannot gain financial authority")
    expected_names = {f"raw-{day[:7]}" for day in inventory["sessions"]} | {
        f"features-{number:04d}" for number in range(len(plan["batches"]))
    }
    feature_names = [item["name"] for item in load_contract()["features"]]
    target_sessions = sum(
        report["config"]["start"] <= day <= report["config"]["end"] for day in inventory["sessions"]
    )
    if (
        set(report["partition_receipts"]) != expected_names
        or [item["name"] for item in report["features"]] != feature_names
        or report["target_grid_rows"] != len(inventory["instruments"]) * target_sessions
        or any(
            not (0 <= item["target_usable_rows"] <= report["target_active_rows"])
            for item in report["features"]
        )
    ):
        raise DataValidationError("staging coverage or partition set is incomplete")
    if full_verify:
        native_receipts = []
        for name, fingerprint in report["partition_receipts"].items():
            receipt = sealed_read(out / "receipts" / f"{name}.json")
            if receipt["fingerprint"] != fingerprint or receipt["identity"] != plan["fingerprint"]:
                raise DataValidationError("staging receipt identity mismatch")
            verify_entries(out, receipt["artifacts"])
            if name.startswith("features-"):
                native_receipts.append(receipt)
        rebuilt = aggregate(plan, native_receipts)
        if any(report[key] != value for key, value in rebuilt.items()):
            raise DataValidationError("staging summary differs from committed partitions")
    return report


def staging_progress(out: Path):
    """Read only small sealed summaries; never load the full factor cube in the UI."""
    if (out / "report.json").exists():
        return load_staging_report(out)
    if not (out / "plan.json").exists():
        return None
    plan = sealed_read(out / "plan.json")
    if plan["config"] != load_config():
        raise DataValidationError("staging progress contract mismatch")
    receipts = []
    for path in sorted((out / "receipts").glob("features-*.json")):
        receipt = sealed_read(path)
        if receipt["identity"] != plan["fingerprint"]:
            raise DataValidationError("staging progress identity mismatch")
        receipts.append(receipt)
    return {
        "status": "partial",
        **aggregate(plan, receipts),
        "config": plan["config"],
        "instrument_count": sum(len(batch) for batch in plan["batches"]),
        "terminal_failure_recorded": any((out / "failures").glob("*.json")),
        "new_fit_attempts": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inventory", "run", "verify"))
    args = parser.parse_args()
    if args.action == "verify":
        result = load_staging_report(PROJECT_ROOT / OUTPUT_DIRECTORY, full_verify=True)
    elif args.action == "inventory":
        # Read-only source discovery precedes the final contract pin and any factors.
        config = json.loads((PROJECT_ROOT / CONFIG_PATH).read_text())
        out = PROJECT_ROOT / OUTPUT_DIRECTORY
        with exclusive_job(out):
            budget = Budget(out, config)
            budget.check(projected_memory=512 * 1024**2)
            result = atomic_seal(out / "inventory.json", discover_inventory(PROJECT_ROOT, config))
    else:
        import pyarrow as pa
        from threadpoolctl import threadpool_limits

        pa.set_cpu_count(2)
        pa.set_io_thread_count(2)
        with threadpool_limits(limits=2):
            result = run_staging()
    print(
        json.dumps(
            {
                "action": args.action,
                "status": result.get("status"),
                "fingerprint": result["fingerprint"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
