"""One closed 98-slot new-fit budget, six immutable reuse jobs and inherited locks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_protocol import artifact_entries, runtime_manifest
from quantlab.research.alpha158_store import atomic_seal, directory_bytes
from quantlab.research.extended_plan_review import read_verification
from quantlab.research.extended_plan_run import OUTPUT as PLANNING_OUTPUT
from quantlab.research.extended_plan_run import read_report as read_planning_report
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.weekly_pilot_protocol import HEAVY, job_rss, now, stop_owned_process

CONFIG = "config/alpha158_extended_frequency_v1.json"
SOURCES = "config/alpha158_extended_frequency_sources_v1.json"
OUTPUT = "data/products/alpha158_extended_frequency/alpha158_extended_frequency_20260912"
RUNTIME = "data/runtime/research/alpha158_extended_frequency_20260912"
OLD = "data/products/alpha158_weekly/alpha158_weekly_pilot_20260912"


def prediction_union(weeks, week_id):
    selected = [
        row for row in weeks if row["week_id"] == week_id or row["monthly_anchor"] == week_id
    ]
    return sorted({day for row in selected for day in row["prediction_sessions"]}), sum(
        row["prediction_rows"] for row in selected
    )


def folder_for(root, config, slot):
    if slot not in config["model_specs"]:
        raise DataValidationError("unknown extended worker slot")
    mode = "reuse" if config["model_specs"][slot]["reuse_slot"] is not None else "fits"
    return root / OUTPUT / mode / slot


class Ledger:
    """Ordered single-use starts; fit and reuse counters cannot replenish one another."""

    def __init__(self, out, identity, slots, mode):
        self.identity, self.slots, self.mode = identity, tuple(slots), mode
        if mode not in ("fits", "reuse") or len(self.slots) != (98 if mode == "fits" else 6):
            raise DataValidationError("extended ledger requires 98 new fits or six reuse jobs")
        if len(set(self.slots)) != len(self.slots) or any(
            re.fullmatch(r"[a-z0-9_]+", s) is None for s in self.slots
        ):
            raise DataValidationError("invalid extended slot identities")
        self.base = out / mode
        self.base.mkdir(exist_ok=True)
        present = {p.name for p in self.base.iterdir()}
        if present != set(self.slots[: len(present)]):
            raise DataValidationError("extended ledger has undeclared or skipped slots")

    def read(self, slot):
        if slot not in self.slots:
            raise DataValidationError("slot outside closed extended budget")
        folder = self.base / slot
        if not folder.exists():
            return None
        started = sealed_read(folder / "started.json")
        if (
            started.get("identity") != self.identity
            or started.get("slot") != slot
            or started.get("mode") != self.mode
            or type(started.get("attempt_number")) is not int
            or started["attempt_number"] != self.slots.index(slot) + 1
        ):
            raise DataValidationError("extended reservation identity/sequence changed")
        if not (folder / "result.json").exists():
            return {"slot": slot, "mode": self.mode, "status": "interrupted", "started": started}
        result = sealed_read(folder / "result.json")
        if (
            result.get("identity") != self.identity
            or result.get("slot") != slot
            or result.get("mode") != self.mode
            or result.get("status") not in ("completed", "failed", "interrupted")
        ):
            raise DataValidationError("extended terminal receipt changed")
        verify_entries(folder, result["artifacts"])
        return result

    def start(self, slot):
        if self.read(slot) is not None:
            raise DataValidationError("consumed extended slot cannot be retried")
        number = self.slots.index(slot)
        for earlier in self.slots[:number]:
            previous = self.read(earlier)
            if previous is None or previous["status"] != "completed":
                raise DataValidationError("earlier extended slot is incomplete; no skipping")
        folder = self.base / slot
        folder.mkdir()
        atomic_seal(
            folder / "started.json",
            {
                "identity": self.identity,
                "slot": slot,
                "mode": self.mode,
                "started_at": now(),
                "attempt_number": number + 1,
            },
        )
        return folder

    def finish(self, slot, status, **payload):
        folder = self.base / slot
        if self.read(slot) is None or (folder / "result.json").exists():
            raise DataValidationError("extended finish requires exactly one unfinished reservation")
        if status not in ("completed", "failed", "interrupted"):
            raise DataValidationError("invalid extended finish state")
        if self.mode == "reuse" and (folder / "invoked.json").exists():
            raise DataValidationError("reused model gained a fit invocation")
        return atomic_seal(
            folder / "result.json",
            {
                "identity": self.identity,
                "slot": slot,
                "mode": self.mode,
                "status": status,
                "finished_at": now(),
                "artifacts": artifact_entries(folder, exclude=("result.json",)),
                **payload,
            },
        )


def validate_contract(config, sources, planning, old_plan, old_report):
    if config["weeks"] != planning["weeks"] or config["models"] != planning["models"]:
        raise DataValidationError("extended run differs from fixed schedule/models")
    if (
        config["features"] != old_plan["config"]["features"]
        or sources["runtime"] != old_plan["sources"]["runtime"]
    ):
        raise DataValidationError("extended feature/runtime contract changed")
    planned = planning["fit_slots"]
    order = [s["slot"] for s in planned]
    new = [s["slot"] for s in planned if s["reuse_slot"] is None]
    reuse = [s["slot"] for s in planned if s["reuse_slot"] is not None]
    if (
        config["slot_order"] != order
        or config["new_slots"] != new
        or config["reuse_slots"] != reuse
        or len(new) != 98
        or len(reuse) != 6
        or set(config["model_specs"]) != set(order)
    ):
        raise DataValidationError("extended closed slot inventory changed")
    limits = {
        "max_fit_attempts": 98,
        "max_jobs_per_segment": 6,
        "max_generated_bytes": 8 * 1024**3,
        "max_rss_bytes": 8 * 1024**3,
        "minimum_available_memory_bytes": 10 * 1024**3,
        "reserve_host_D_bytes": 8 * 1024**3,
        "max_wakeup_seconds": 2700,
        "next_partition_time_reserve_seconds": 420,
        "compute_threads": 2,
        "arrow_cpu_io_threads": 2,
        "physical_prediction_rows": 4320562,
        "policy_members_per_model": 1215709,
        "policy_evaluation_rows_per_model": 1214290,
        "shared_signal_days_per_model": 54,
        "comparison_signal_days_per_model": 188,
    }
    if any(type(config.get(k)) is not int or config[k] != v for k, v in limits.items()):
        raise DataValidationError("extended immutable resource/population limit changed")
    if config.get("history_already_observed") is not True or any(
        config.get(k) is not False
        for k in ("allow_refit", "performance_evidence", "execution_authority")
    ):
        raise DataValidationError("extended authority changed")
    originals = {r["slot"]: r["summary"] for r in old_report["attempts"]}
    for entry in planned:
        spec = config["model_specs"][entry["slot"]]
        if any(spec.get(k) != v for k, v in entry.items()):
            raise DataValidationError("extended fit/reuse reference changed")
        dates, count = prediction_union(config["weeks"], entry["week_id"])
        if spec["prediction_sessions"] != dates or spec["prediction_rows"] != count:
            raise DataValidationError("extended prediction union changed")
        for key in ("train_sha256", "prediction_sha256"):
            if re.fullmatch(r"[a-f0-9]{64}", spec[key]) is None:
                raise DataValidationError("invalid extended membership hash")
        week = next(row for row in config["weeks"] if row["week_id"] == entry["week_id"])
        if (
            any(spec[key] != week[key] for key in ("train_start", "train_end", "train_rows"))
            or spec["replay_prediction_start"] != week["prediction_start"]
        ):
            raise DataValidationError("extended training/replay dates changed")
        if (
            spec["reuse_slot"] is not None
            and spec["original_summary"] != originals[spec["reuse_slot"]]
        ):
            raise DataValidationError("extended original model receipt changed")
    if (
        sum(s["prediction_rows"] for s in config["model_specs"].values())
        != config["physical_prediction_rows"]
    ):
        raise DataValidationError("extended prediction total changed")


def prepare_plan(root):
    config, sources = (
        json.loads((root / CONFIG).read_text()),
        json.loads((root / SOURCES).read_text()),
    )
    if sources["config_sha256"] != _sha(root / CONFIG):
        raise DataValidationError("extended config bytes changed")
    verify_entries(root, sources["inputs"])
    if runtime_manifest() != sources["runtime"]:
        raise DataValidationError("extended runtime changed")
    report = read_planning_report(root)
    planning = sealed_read(root / PLANNING_OUTPUT / "schedule.json")
    review = read_verification(root, report, planning)
    if any(
        config[key] != value
        for key, value in (
            ("planning_report_fingerprint", report["fingerprint"]),
            ("planning_schedule_fingerprint", planning["fingerprint"]),
            ("planning_verification_fingerprint", review["fingerprint"]),
        )
    ):
        raise DataValidationError("extended planning fingerprints changed")
    validate_contract(
        config,
        sources,
        planning,
        sealed_read(root / OLD / "plan.json"),
        sealed_read(root / OLD / "report.json"),
    )
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("extended processing source must be pushed")
    payload = {
        "schema": "alpha158_extended_frequency_v1",
        "config": config,
        "sources": sources,
        "code_head": head,
        "code_files": binding.entries,
    }
    path = root / OUTPUT / "plan.json"
    if path.exists():
        existing = sealed_read(path)
        if {k: v for k, v in existing.items() if k != "fingerprint"} != payload:
            raise DataValidationError("extended resume requires the original frozen source")
        return existing
    return atomic_seal(path, payload)


def verify_locks(root, descriptors):
    if len(descriptors) != 2 or len(set(descriptors)) != 2:
        raise DataValidationError("extended worker requires both inherited locks")
    for fd, name in zip(descriptors, (HEAVY, OUTPUT), strict=True):
        actual, expected = os.fstat(fd), (root / name / "worker.lock").stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise DataValidationError("extended worker inherited unrelated locks")


def generated_bytes(root):
    return directory_bytes(root / OUTPUT) + directory_bytes(root / RUNTIME)


def monitor(process, root, config, segment_started, *, rss_reader=job_rss):
    peak, violation = 0, None
    try:
        while process.poll() is None:
            peak = max(peak, rss_reader(os.getpid()))
            if peak > config["max_rss_bytes"]:
                violation = "combined_worker_tree_rss"
            elif time.monotonic() - segment_started > config["max_wakeup_seconds"]:
                violation = "segment_wall_time"
            elif generated_bytes(root) > config["max_generated_bytes"]:
                violation = "cumulative_generated_bytes"
            elif shutil.disk_usage("/mnt/d").free < config["reserve_host_D_bytes"]:
                violation = "host_D_reserve"
            if violation:
                stop_owned_process(process)
                break
            time.sleep(0.1)
    except BaseException:
        stop_owned_process(process)
        raise
    return {"combined_peak_rss_bytes": peak, "violation": violation, "returncode": process.wait()}
