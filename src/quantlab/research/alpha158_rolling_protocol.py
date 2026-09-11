"""Fixed source identity, exclusive cumulative fit intents, and runtime provenance."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

OUTPUT = "data/products/alpha158_rolling/alpha158_rolling_20260911"
HISTORY = "data/products/alpha158_staging/alpha158_history_20260911"
CONFIG = "config/alpha158_rolling_v1.json"
SOURCES = "config/alpha158_rolling_sources_v1.json"
SOURCE_FP = "bd599dc16eb30853dc60f7f86266383543c06984d83979629d1a70c7215ea2f3"
INVENTORY_FP = "0869cd29048ef0649838ebd6f6c846e5ccd9174e90498a95941ae1b21cc6649c"


def now():
    return datetime.now(UTC).isoformat()


def runtime_manifest():
    versions = {
        package: importlib.metadata.version(package)
        for package in (
            "pyqlib",
            "lightgbm",
            "scikit-learn",
            "numpy",
            "scipy",
            "pandas",
            "pyarrow",
            "mlflow",
        )
    }
    trees = {}
    for module in ("qlib", "lightgbm", "sklearn", "numpy", "scipy", "mlflow"):
        base = Path(importlib.util.find_spec(module).origin).parent
        entries = {
            p.relative_to(base).as_posix(): _sha(p)
            for p in sorted(base.rglob("*"))
            if p.is_file() and p.suffix in (".py", ".so")
        }
        trees[module] = {"files": len(entries), "sha256": canonical_payload_fingerprint(entries)}
    return {"versions": versions, "python_and_native_source_trees": trees}


def source_manifest(root):
    history = root / HISTORY
    if sealed_read(history / "report.json")["fingerprint"] != SOURCE_FP:
        raise DataValidationError("frozen history report changed")
    if sealed_read(history / "inventory.json")["fingerprint"] != INVENTORY_FP:
        raise DataValidationError("frozen source inventory changed")
    files = [history / name for name in ("plan.json", "inventory.json", "report.json")]
    files += sorted((history / "receipts").glob("*.json"))
    return {
        "history_report": SOURCE_FP,
        "inventory": INVENTORY_FP,
        "runtime": runtime_manifest(),
        "files": {
            p.relative_to(root).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size}
            for p in files
        },
    }


def load_contract(root):
    config = json.loads((root / CONFIG).read_text())
    # Pin complete bytes, not merely the fields that current code happens to use.
    source = json.loads((root / SOURCES).read_text())
    if hashlib.sha256((root / CONFIG).read_bytes()).hexdigest() != source["contract_sha256"]:
        raise DataValidationError("immutable rolling contract changed")
    verify_entries(root, source["files"])
    if runtime_manifest() != source["runtime"]:
        raise DataValidationError("fixed model runtime changed")
    return config, source


def artifact_entries(folder, exclude=()):
    return {
        p.relative_to(folder).as_posix(): {"sha256": _sha(p), "bytes": p.stat().st_size}
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name not in exclude
    }


class FitLedger:
    """Exactly one immutable start per declared slot, failures included, never a retry."""

    def __init__(self, out, identity, slots):
        self.out, self.identity, self.slots = out, identity, tuple(slots)
        if len(self.slots) != 6 or len(set(self.slots)) != 6:
            raise DataValidationError("exactly six unique declared slots required")
        (out / "fits").mkdir(exist_ok=True)
        unknown = {p.name for p in (out / "fits").iterdir()} - set(self.slots)
        if unknown:
            raise DataValidationError("unknown fit outside cumulative budget")

    def read(self, slot):
        if slot not in self.slots:
            raise DataValidationError("fit outside fixed six slots")
        folder = self.out / "fits" / slot
        if not folder.exists():
            return None
        start = sealed_read(folder / "started.json")
        if start["identity"] != self.identity or start["slot"] != slot:
            raise DataValidationError("fit source or slot identity changed")
        result = folder / "result.json"
        if not result.exists():
            return {"slot": slot, "status": "interrupted", "started": start}
        value = sealed_read(result)
        if value["identity"] != self.identity or value["slot"] != slot:
            raise DataValidationError("fit result identity changed")
        verify_entries(folder, value["artifacts"])
        return value

    def start(self, slot):
        if self.read(slot) is not None:
            raise DataValidationError("consumed fit cannot be retried")
        folder = self.out / "fits" / slot
        folder.mkdir(exist_ok=False)
        atomic_seal(
            folder / "started.json",
            {
                "slot": slot,
                "identity": self.identity,
                "started_at": now(),
                "attempt_number": 1
                + sum((self.out / "fits" / s / "started.json").exists() for s in self.slots),
            },
        )
        return folder

    def finish(self, slot, status, **payload):
        folder = self.out / "fits" / slot
        current = self.read(slot)
        if current is None or (folder / "result.json").exists():
            raise DataValidationError("fit finish requires one unfinished start")
        if status not in ("completed", "failed", "interrupted"):
            raise DataValidationError("unsupported terminal fit state")
        return atomic_seal(
            folder / "result.json",
            {
                "slot": slot,
                "identity": self.identity,
                "status": status,
                "finished_at": now(),
                "artifacts": artifact_entries(folder, exclude=("result.json",)),
                **payload,
            },
        )
