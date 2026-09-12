"""Immutable pilot contract and inherited locks for a bounded, restart-safe worker tree."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_rolling_protocol import runtime_manifest
from quantlab.research.alpha158_store import atomic_seal, directory_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.weekly_plan_run import read_report as read_weekly_plan
from quantlab.research.weekly_plan_run import read_verification

CONFIG = "config/alpha158_weekly_pilot_v1.json"
SOURCES = "config/alpha158_weekly_pilot_sources_v1.json"
OUTPUT = "data/products/alpha158_weekly/alpha158_weekly_pilot_20260912"
RUNTIME = "data/runtime/research/alpha158_weekly_pilot_20260912"
HEAVY = "data/runtime/research/heavy_job"


def now():
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@contextmanager
def inherited_locks(folders):
    """A child keeps the same flock open description if its coordinator disappears."""
    streams = []
    try:
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
            stream = (folder / "worker.lock").open("a")
            streams.append(stream)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DataValidationError("another heavy worker holds the pilot locks") from exc
        yield [stream.fileno() for stream in streams]
    finally:
        # Do not LOCK_UN: an orphaned child must continue holding the inherited lock.
        for stream in reversed(streams):
            stream.close()


def verify_inherited_locks(root, descriptors):
    if len(descriptors) != 2 or len(set(descriptors)) != 2:
        raise DataValidationError("worker requires both inherited locks")
    for fd, name in zip(descriptors, (HEAVY, OUTPUT), strict=True):
        actual, expected = os.fstat(fd), (root / name / "worker.lock").stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise DataValidationError("worker inherited another job's lock")


def prepare_plan(root):
    config = json.loads((root / CONFIG).read_text())
    sources = json.loads((root / SOURCES).read_text())
    if sources["config_sha256"] != _sha(root / CONFIG):
        raise DataValidationError("weekly pilot contract changed")
    verify_entries(root, sources["inputs"])
    if runtime_manifest() != sources["runtime"]:
        raise DataValidationError("weekly pilot runtime changed")
    report, _, weekly = read_weekly_plan(root)
    review = read_verification(root, report, weekly)
    if (
        report["fingerprint"] != config["planning_report_fingerprint"]
        or review["fingerprint"] != config["planning_verification_fingerprint"]
        or weekly["rows"] != config["weeks"]
        or weekly["models"] != config["models"]
    ):
        raise DataValidationError("pilot differs from the fixed weekly proposal")
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("weekly pilot source must be pushed before fitting")
    payload = {
        "schema": "alpha158_weekly_pilot_v1",
        "code_head": head,
        "code_files": binding.entries,
        "config": config,
        "sources": sources,
    }
    path = root / OUTPUT / "plan.json"
    if path.exists():
        previous = sealed_read(path)
        if {k: v for k, v in previous.items() if k != "fingerprint"} != payload:
            raise DataValidationError("weekly pilot resume source changed")
        return previous
    return atomic_seal(path, payload)


def invoke_once(folder, identity, slot, action):
    start = sealed_read(folder / "started.json")
    if start["identity"] != identity or start["slot"] != slot:
        raise DataValidationError("fit invocation differs from reserved slot")
    if (folder / "model.pkl").exists() or (folder / "result.json").exists():
        raise DataValidationError("consumed model cannot be fitted again")
    atomic_seal(folder / "invoked.json", {"identity": identity, "slot": slot, "at": now()})
    return action()


def job_rss(pid, proc_root=Path("/proc")):
    """Current RSS of the coordinator and its descendants; threads counted once."""
    total, pending, seen = 0, [pid], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            base = proc_root / str(current)
            total += int((base / "statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
            for child_list in (base / "task").glob("*/children"):
                try:
                    pending += [int(x) for x in child_list.read_text().split()]
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
    return total


def stop_owned_process(process):
    if process.poll() is not None:
        return
    if os.getpgid(process.pid) != process.pid:
        raise DataValidationError("refusing to terminate an unrelated process group")
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


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
