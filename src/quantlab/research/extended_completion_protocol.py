"""Four remaining reservations in the original 98-fit budget, with immutable parents."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling import resource_preflight
from quantlab.research.alpha158_rolling_protocol import runtime_manifest
from quantlab.research.alpha158_store import atomic_seal, directory_bytes
from quantlab.research.extended_frequency_protocol import Ledger
from quantlab.research.extended_recovery import load_report as recovery_report
from quantlab.research.extended_recovery_protocol import (
    OLD,
    PARENT,
    PARENT_PLAN,
    PARENT_REPORT,
    immutable_tree,
    verify_tree,
)
from quantlab.research.extended_recovery_protocol import OUTPUT as RECOVERY
from quantlab.research.extended_recovery_review import read_verification
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.signal_feasibility import verify_historical_inputs
from quantlab.research.weekly_pilot_protocol import HEAVY, job_rss, now, stop_owned_process

CONFIG = "config/alpha158_extended_completion_v1.json"
OUTPUT = "data/products/alpha158_extended_completion/alpha158_extended_completion_20260912"
RUNTIME = "data/runtime/research/alpha158_extended_completion_20260912"
RECOVERY_REPORT = "9fa8181a2b6fb3ed873c399548309017137c3c9b856b7b4dc84041e020e85bde"
RECOVERY_PROOF = "eb2ad642eb5032b588f4d8df9cc51390518e53135c9ddb50a1f0a9b04311e9d7"
SLOTS = [f"2026w{week}_{kind}" for week in (35, 36) for kind in ("ridge", "lightgbm")]
LIMITS = {
    "schema": "alpha158_extended_completion_v1",
    "experiment_id": "alpha158_extended_completion_20260912",
    "parent_plan": PARENT_PLAN,
    "parent_report": PARENT_REPORT,
    "recovery_report": RECOVERY_REPORT,
    "recovery_proof": RECOVERY_PROOF,
    "slots": SLOTS,
    "global_fit_ceiling": 98,
    "inherited_consumed_fits": 94,
    "remaining_slot_authority": 4,
    "global_attempt_numbers": [95, 96, 97, 98],
    "prediction_rows": 60998,
    "max_jobs_per_segment": 4,
    "max_generated_bytes": 1024**3,
    "max_rss_bytes": 8 * 1024**3,
    "minimum_available_memory_bytes": 10 * 1024**3,
    "reserve_host_D_bytes": 8 * 1024**3,
    "max_wakeup_seconds": 2700,
    "next_partition_time_reserve_seconds": 420,
    "threads": 2,
}


def contract(root):
    c = json.loads((root / CONFIG).read_text())
    if c != LIMITS or any(type(c[k]) is not type(v) for k, v in LIMITS.items()):
        raise DataValidationError("remaining-slot completion contract changed")
    if any(type(n) is not int for n in c["global_attempt_numbers"]):
        raise DataValidationError("global attempt numbers must be exact integers")
    return c


def basis(root):
    report = recovery_report(root)
    proof = read_verification(root, report)
    if (
        report["fingerprint"] != RECOVERY_REPORT
        or proof is None
        or proof["fingerprint"] != RECOVERY_PROOF
    ):
        raise DataValidationError(
            "completion requires the independently closed six-reference recovery"
        )
    plan = sealed_read(root / RECOVERY / "plan.json")
    if plan["parent_plan"] != PARENT_PLAN or plan["config"]["new_slots"][94:] != SLOTS:
        raise DataValidationError("completion changed original remaining slots")
    budget = report["carried_budget"]
    if budget != {
        "global_fit_ceiling": 98,
        "inherited_consumed_fits": 94,
        "recovery_fit_invocations": 0,
        "remaining_unconsumed_fits": 4,
    }:
        raise DataValidationError("completion cannot replenish consumed fit budget")
    return plan


class CarryLedger(Ledger):
    """Original global attempts95--98; local ordinal is only a path/checkpoint index."""

    def __init__(self, out, identity):
        self.identity, self.slots, self.mode = identity, tuple(SLOTS), "fits"
        self.base = out / "fits"
        self.base.mkdir(exist_ok=True)
        present = {p.name for p in self.base.iterdir()}
        if present != set(SLOTS[: len(present)]):
            raise DataValidationError("completion has undeclared or skipped remaining slots")

    def read(self, slot):
        result = super().read(slot)
        if result is not None:
            start = sealed_read(self.base / slot / "started.json")
            expected = {
                "global_attempt_number": 95 + SLOTS.index(slot),
                "global_fit_ceiling": 98,
                "inherited_consumed_fits": 94,
                "parent_plan": PARENT_PLAN,
                "parent_failed_report": PARENT_REPORT,
                "recovery_report": RECOVERY_REPORT,
            }
            if any(type(start.get(k)) is not type(v) or start[k] != v for k, v in expected.items()):
                raise DataValidationError("completion reservation changed original global budget")
        return result

    def start(self, slot):
        if slot not in SLOTS or self.read(slot) is not None:
            raise DataValidationError("remaining original slot cannot be retried or replaced")
        for earlier in SLOTS[: SLOTS.index(slot)]:
            value = self.read(earlier)
            if value is None or value["status"] != "completed":
                raise DataValidationError("earlier completion reservation incomplete; no skipping")
        folder = self.base / slot
        folder.mkdir()
        atomic_seal(
            folder / "started.json",
            {
                "identity": self.identity,
                "slot": slot,
                "mode": "fits",
                "attempt_number": SLOTS.index(slot) + 1,
                "global_attempt_number": 95 + SLOTS.index(slot),
                "global_fit_ceiling": 98,
                "inherited_consumed_fits": 94,
                "parent_plan": PARENT_PLAN,
                "parent_failed_report": PARENT_REPORT,
                "recovery_report": RECOVERY_REPORT,
                "started_at": now(),
            },
        )
        return folder


def prepare_plan(root):
    c = contract(root)
    old = basis(root)
    resource_preflight(c)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
        != head
    ):
        raise DataValidationError("completion source must be pushed before reserving fits")
    if runtime_manifest() != old["sources"]["runtime"]:
        raise DataValidationError("completion must use the original model runtime")
    payload = {
        "schema": c["schema"],
        "contract": c,
        "contract_sha256": _sha(root / CONFIG),
        "code_head": head,
        "code_files": binding.entries,
        "config": old["config"],
        "sources": old["sources"],
        "parent_plan": PARENT_PLAN,
        "parent_report": PARENT_REPORT,
        "recovery_report": RECOVERY_REPORT,
        "recovery_proof": RECOVERY_PROOF,
        "immutable_trees": {n: immutable_tree(root, n) for n in (PARENT, OLD, RECOVERY)},
    }
    out = root / OUTPUT
    out.mkdir(parents=True, exist_ok=True)
    path = out / "plan.json"
    if path.exists():
        plan = sealed_read(path)
        if {k: v for k, v in plan.items() if k != "fingerprint"} != payload:
            raise DataValidationError(
                "completion resume requires its exact frozen source and parents"
            )
        return plan
    return atomic_seal(path, payload)


def verify_context(root, plan, *, historical=False):
    old = basis(root)
    if plan["contract"] != contract(root) or plan["contract_sha256"] != _sha(root / CONFIG):
        raise DataValidationError("completion contract changed")
    if (
        plan["config"] != old["config"]
        or plan["sources"] != old["sources"]
        or plan["schema"] != LIMITS["schema"]
        or plan["parent_plan"] != PARENT_PLAN
        or plan["parent_report"] != PARENT_REPORT
        or plan["recovery_report"] != RECOVERY_REPORT
        or plan["recovery_proof"] != RECOVERY_PROOF
        or set(plan["immutable_trees"]) != {PARENT, OLD, RECOVERY}
    ):
        raise DataValidationError("completion source/population/budget lineage changed")
    for name, entries in plan["immutable_trees"].items():
        verify_tree(root, name, entries)
    if historical:
        verify_historical_inputs(
            root, {"code_head": plan["code_head"], "inputs": plan["code_files"]}
        )
    else:
        verify_entries(root, plan["code_files"])
        if runtime_manifest() != plan["sources"]["runtime"]:
            raise DataValidationError("completion runtime changed")


def verify_locks(root, descriptors):
    if len(descriptors) != 2 or len(set(descriptors)) != 2:
        raise DataValidationError("completion worker needs both inherited locks")
    for fd, name in zip(descriptors, (HEAVY, OUTPUT), strict=True):
        actual, expected = os.fstat(fd), (root / name / "worker.lock").stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise DataValidationError("completion inherited an unrelated lock")


def generated_bytes(root):
    return directory_bytes(root / OUTPUT) + directory_bytes(root / RUNTIME)


def monitor(process, root, c, segment_started, *, rss_reader=job_rss):
    peak, violation = 0, None
    try:
        while process.poll() is None:
            peak = max(peak, rss_reader(os.getpid()))
            if peak > c["max_rss_bytes"]:
                violation = "combined_rss"
            elif time.monotonic() - segment_started > c["max_wakeup_seconds"]:
                violation = "segment_time"
            elif generated_bytes(root) > c["max_generated_bytes"]:
                violation = "cumulative_output"
            elif shutil.disk_usage("/mnt/d").free < c["reserve_host_D_bytes"]:
                violation = "D_reserve"
            if violation:
                stop_owned_process(process)
                break
            time.sleep(0.1)
    except BaseException:
        stop_owned_process(process)
        raise
    return {"combined_peak_rss_bytes": peak, "violation": violation, "returncode": process.wait()}


def validate_outputs(root, c):
    if generated_bytes(root) > c["max_generated_bytes"] or (root / OUTPUT / "reuse").exists():
        raise DataValidationError("completion output budget or authority changed")
    # Qlib records one byte-identical copy of each saved model inside its own run.
    copies = set()
    for path in (root / OUTPUT).rglob("*.pkl"):
        parts = path.relative_to(root / OUTPUT).parts
        if parts in [("fits", slot, "model.pkl") for slot in SLOTS]:
            continue
        if (
            len(parts) != 8
            or parts[0] != "fits"
            or parts[1] not in SLOTS
            or parts[2:4] != ("qlib", "mlruns")
            or re.fullmatch(r"[0-9]+", parts[4]) is None
            or re.fullmatch(r"[a-f0-9]{32}", parts[5]) is None
            or parts[6:] != ("artifacts", "model.pkl")
            or parts[1] in copies
            or _sha(path) != _sha(root / OUTPUT / "fits" / parts[1] / "model.pkl")
        ):
            raise DataValidationError("completion gained an undeclared model artifact")
        copies.add(parts[1])
    for path in (root / OUTPUT).rglob("invoked.json"):
        if path.relative_to(root / OUTPUT).parts not in [
            ("fits", slot, "invoked.json") for slot in SLOTS
        ]:
            raise DataValidationError("completion gained an undeclared invocation")
