"""Single-use scenario reservations and immutable preparation/worker bindings."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date
from importlib.metadata import version

import pandas as pd

from quantlab.backtest.run_spec import fingerprint_lifecycle_monitor, fingerprint_risk_facts
from quantlab.data.models import DataValidationError
from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.alpha158_store import atomic_seal, directory_bytes
from quantlab.research.extended_economic_contract import (
    CONTRACT,
    OUT,
    SOURCE_CONFIG,
    scenarios,
    target_manifest,
)
from quantlab.research.extended_economic_inputs import collect
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.weekly_pilot_protocol import HEAVY, job_rss, now, stop_owned_process


def frame_hash(frame):
    digest = hashlib.sha256()
    digest.update("|".join(frame.columns).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes())
    return digest.hexdigest()


def source_summary(root):
    summary, marks, targets, monitor, facts = collect(root)
    summary.update(
        marks_hash=frame_hash(marks),
        lifecycle_hash=fingerprint_lifecycle_monitor(monitor),
        risk_facts_hash=fingerprint_risk_facts(facts),
        scenarios=scenarios(),
        runtime={
            "python": sys.version,
            **{name: version(name) for name in ("numpy", "pandas", "pyarrow")},
        },
    )
    return summary, marks, targets


def verify_code(root, plan, *, historical=False):
    verify_entries(root, plan["code_files"])
    if not historical:
        binding = InputBinding(root)
        head = code_binding(root, binding)
        pushed = subprocess.check_output(
            ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
        ).strip()
        if head != plan["code_head"] or pushed != head:
            raise DataValidationError("economic execution must use the exact clean pushed source")


def prepare(root):
    out = root / OUT
    if (out / "plan.json").exists():
        plan = sealed_read(out / "plan.json")
        verify_code(root, plan)
        verify_entries(out, plan["artifacts"])
        return plan
    if {p.name for p in out.iterdir()} - {"worker.lock"}:
        raise DataValidationError("interrupted economic preparation needs explicit recovery")
    sources = json.loads((root / SOURCE_CONFIG).read_text())
    if sources["contract"] != CONTRACT or sources["scenarios"] != scenarios():
        raise DataValidationError("economic frozen contract changed")
    binding = InputBinding(root)
    head = code_binding(root, binding)
    pushed = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != pushed:
        raise DataValidationError("economic input contract must be pushed first")
    preflight(root)
    atomic_seal(out / "preparing.json", {"source_head": head, "at": now()})
    actual, marks, targets = source_summary(root)
    if actual != sources:
        raise DataValidationError("economic source, targets, marks or declared population changed")
    marks.to_parquet(out / "marks.parquet", index=False)
    files = ["marks.parquet"]
    for name, value in targets.items():
        saved = {
            str(d): {
                "cash_weight": t.cash_weight,
                "positions": {p.instrument_id: p.target_weight for p in t.positions},
            }
            for d, t in value.items()
        }
        filename = f"targets/{name}.json"
        atomic_seal(out / filename, {"name": name, "targets": saved})
        files.append(filename)
    binding.check()
    return atomic_seal(
        out / "plan.json",
        {
            "code_head": head,
            "code_files": binding.entries,
            "sources": sources,
            "artifacts": {
                name: {"sha256": _sha(out / name), "bytes": (out / name).stat().st_size}
                for name in files
            },
        },
    )


def load_targets(out, plan, name):
    if name not in plan["sources"]["targets"]:
        raise DataValidationError("undeclared economic target identity")
    filename = f"targets/{name}.json"
    verify_entries(out, {filename: plan["artifacts"][filename]})
    saved = sealed_read(out / filename)
    if saved["name"] != name:
        raise DataValidationError("prepared target policy name changed")
    targets = {
        date.fromisoformat(d): TargetPortfolio(
            date.fromisoformat(d),
            tuple(TargetWeight(i, w) for i, w in t["positions"].items()),
            t["cash_weight"],
        )
        for d, t in saved["targets"].items()
    }
    if target_manifest({name: targets})[name] != plan["sources"]["targets"][name]:
        raise DataValidationError("prepared economic target contents changed")
    return targets


def preflight(root):
    available = (
        int(
            next(
                s.split()[1]
                for s in (root / "/proc/meminfo").read_text().splitlines()
                if s.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    if available < CONTRACT["minimum_available_memory_bytes"]:
        raise DataValidationError("economic job requires at least 8 GiB available memory")
    if shutil.disk_usage("/mnt/d").free < CONTRACT["reserve_host_D_bytes"]:
        raise DataValidationError("economic job would breach D reserve")
    if directory_bytes(root / OUT) >= CONTRACT["max_generated_bytes"]:
        raise DataValidationError("economic cumulative output ceiling consumed")


def verify_locks(root, descriptors):
    if len(descriptors) != 2 or len(set(descriptors)) != 2:
        raise DataValidationError("economic worker requires both inherited locks")
    for fd, folder in zip(descriptors, (HEAVY, OUT), strict=True):
        actual, wanted = os.fstat(fd), (root / folder / "worker.lock").stat()
        if (actual.st_dev, actual.st_ino) != (wanted.st_dev, wanted.st_ino):
            raise DataValidationError("economic worker inherited an unrelated lock")


class Ledger:
    def __init__(self, out, identity):
        self.out, self.identity = out, identity
        self.names = [s["id"] for s in scenarios()]
        self.base = out / "paths"
        self.base.mkdir(exist_ok=True)
        present = {p.name for p in self.base.iterdir()}
        if present != set(self.names[: len(present)]):
            raise DataValidationError("economic ledger has unknown or skipped scenario identities")

    def read(self, name):
        if name not in self.names:
            raise DataValidationError("scenario outside frozen 60-path budget")
        folder = self.base / name
        if not folder.exists():
            return None
        started = sealed_read(folder / "started.json")
        if (
            started["identity"] != self.identity
            or started["scenario"] != name
            or started["reservation"] != self.names.index(name) + 1
        ):
            raise DataValidationError("economic reservation identity changed")
        if not (folder / "receipt.json").exists():
            return {"status": "interrupted", "scenario": name, "identity": self.identity}
        receipt = sealed_read(folder / "receipt.json")
        if receipt["identity"] != self.identity or receipt["scenario"] != name:
            raise DataValidationError("economic terminal receipt identity changed")
        verify_entries(folder, receipt["artifacts"])
        if {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()} != {
            *receipt["artifacts"],
            "receipt.json",
        }:
            raise DataValidationError("economic terminal receipt hides unexpected artifacts")
        return receipt

    def start(self, name):
        if name not in self.names or self.read(name) is not None:
            raise DataValidationError("economic scenario reservation already consumed")
        index = self.names.index(name)
        if any(
            self.read(n) is None or self.read(n)["status"] != "finished" for n in self.names[:index]
        ):
            raise DataValidationError("economic scenario predecessor is not finished")
        folder = self.base / name
        atomic_seal(
            folder / "started.json",
            {"identity": self.identity, "scenario": name, "reservation": index + 1, "at": now()},
        )
        return folder

    def finish(self, name, status, **values):
        folder = self.base / name
        if self.read(name) is None or (folder / "receipt.json").exists():
            raise DataValidationError("economic finish requires one unfinished reservation")
        artifacts = {
            str(p.relative_to(folder)): {"bytes": p.stat().st_size, "sha256": _sha(p)}
            for p in sorted(folder.rglob("*"))
            if p.is_file()
        }
        return atomic_seal(
            folder / "receipt.json",
            {
                "identity": self.identity,
                "scenario": name,
                "status": status,
                "artifacts": artifacts,
                "at": now(),
                **values,
            },
        )


def monitor(process, root, started):
    peak, violation = 0, None
    try:
        while process.poll() is None:
            peak = max(peak, job_rss(os.getpid()))
            if peak > CONTRACT["max_rss_bytes"]:
                violation = "combined_rss"
            elif time.monotonic() - started > CONTRACT["max_wakeup_seconds"]:
                violation = "segment_time"
            elif directory_bytes(root / OUT) > CONTRACT["max_generated_bytes"]:
                violation = "cumulative_output"
            elif shutil.disk_usage("/mnt/d").free < CONTRACT["reserve_host_D_bytes"]:
                violation = "D_reserve"
            if violation:
                stop_owned_process(process)
                break
            time.sleep(0.25)
        return {
            "combined_peak_rss_bytes": peak,
            "violation": violation,
            "returncode": process.wait(),
        }
    except BaseException:
        stop_owned_process(process)
        raise
