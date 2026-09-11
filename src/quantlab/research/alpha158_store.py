"""Exclusive, sealed research partitions and cumulative resource limits (Linux worker)."""

from __future__ import annotations

import fcntl
import os
import re
import resource
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, sealed_write, verify_entries


def atomic_seal(path: Path, payload: dict):
    """Publish by exclusive hard link; readers never see an incomplete JSON receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.pending-{uuid.uuid4().hex}")
    result = sealed_write(pending, payload)
    os.link(pending, path)
    pending.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return result


@contextmanager
def exclusive_job(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with (out / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataValidationError("another staging worker owns this job") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def directory_bytes(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def peak_rss_bytes():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


class Budget:
    def __init__(self, out, config, *, disk_root=Path("/mnt/d")):
        self.out, self.config, self.disk_root = out, config, disk_root
        self.started = time.monotonic()

    def check(self, projected_bytes=0, projected_memory=0):
        used = directory_bytes(self.out)
        if used + projected_bytes > self.config["max_generated_bytes"]:
            raise DataValidationError("cumulative staging disk budget would be exceeded")
        remaining = self.config["max_generated_bytes"] - used
        if shutil.disk_usage(self.disk_root).free < remaining + self.config["reserve_host_D_bytes"]:
            raise DataValidationError("host D drive reserve would be exhausted")
        if peak_rss_bytes() + projected_memory > self.config["max_rss_bytes"]:
            raise DataValidationError("staging RSS budget would be exceeded")
        available = dict(
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
        )["MemAvailable"].split()[0]
        if int(available) * 1024 < projected_memory + 1024**3:
            raise DataValidationError("insufficient available memory for next partition")
        return used

    def can_start(self):
        return time.monotonic() - self.started < (
            self.config["max_wakeup_seconds"] - self.config["next_partition_time_reserve_seconds"]
        )

    @contextmanager
    def watchdog(self):
        """Stop only this isolated worker on its hard RSS/wall limit; partials survive."""
        stop = threading.Event()

        def watch():
            while not stop.wait(0.1):
                rss = peak_rss_bytes()
                elapsed = time.monotonic() - self.started
                if (
                    rss > self.config["max_rss_bytes"]
                    or elapsed > self.config["max_wakeup_seconds"]
                ):
                    atomic_seal(
                        self.out / "interruptions" / f"{uuid.uuid4().hex}.json",
                        {"reason": "resource_watchdog", "peak_rss_bytes": rss, "seconds": elapsed},
                    )
                    os._exit(73)

        worker = threading.Thread(target=watch, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            worker.join()


class Partitions:
    """A terminal error blocks retries; abandoned attempts may resume within one budget."""

    def __init__(self, out: Path, identity: str, budget: Budget, max_attempts=2):
        self.out, self.identity, self.budget, self.max_attempts = (
            out,
            identity,
            budget,
            max_attempts,
        )

    def receipt_path(self, name):
        if re.fullmatch(r"[a-z0-9_-]+", name) is None:
            raise DataValidationError("unsafe research partition name")
        return self.out / "receipts" / f"{name}.json"

    def read(self, name):
        path = self.receipt_path(name)
        if not path.exists():
            return None
        value = sealed_read(path)
        if value.get("identity") != self.identity or value.get("name") != name:
            raise DataValidationError("staging partition identity changed")
        verify_entries(self.out, value["artifacts"])
        return value

    def execute(self, name, projected_bytes, projected_memory, action):
        prior = self.read(name)
        if prior is not None:
            return prior
        if not self.budget.can_start():
            return None
        self.budget.check(projected_bytes, projected_memory)
        parent = self.out / "attempts" / name
        parent.mkdir(parents=True, exist_ok=True)
        attempts = sorted(parent.iterdir())
        for path in attempts:
            started = sealed_read(path / "started.json")
            if started["identity"] != self.identity:
                raise DataValidationError("abandoned staging attempt has another identity")
            if (path / "failed.json").exists():
                raise DataValidationError(
                    "prior terminal partition failure requires explicit recovery"
                )
        if len(attempts) >= self.max_attempts:
            raise DataValidationError("cumulative partition attempt limit reached")
        folder = parent / f"{len(attempts) + 1:04d}"
        folder.mkdir()
        atomic_seal(folder / "started.json", {"identity": self.identity, "name": name})
        before = time.monotonic()
        try:
            result = action(folder)
            self.budget.check()
            artifacts = {
                p.relative_to(self.out).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size}
                for p in sorted(folder.rglob("*"))
                if p.is_file()
            }
            return atomic_seal(
                self.receipt_path(name),
                {
                    "identity": self.identity,
                    "name": name,
                    "result": result,
                    "attempt": len(attempts) + 1,
                    "seconds": time.monotonic() - before,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "artifacts": artifacts,
                },
            )
        except BaseException as exc:
            atomic_seal(folder / "failed.json", {"type": type(exc).__name__, "error": str(exc)})
            raise
