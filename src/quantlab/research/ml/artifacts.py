"""Atomic immutable receipts and a restricted JSON codec for account checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from quantlab.research import quantity_kernel as kernel
from quantlab.research import quantity_scheduler as scheduler

_TYPES = {
    cls.__name__: cls
    for cls in (
        kernel.ResearchBook,
        kernel.ResearchLot,
        kernel.CapacityUsed,
        kernel.ResearchOrder,
        kernel.ResearchTransition,
        kernel.ResearchSession,
        kernel.ResearchFeeScenario,
        kernel.ResearchQuantityRules,
        scheduler.RawCloseMark,
        scheduler.ResearchDay,
        scheduler.ResearchDayRecord,
        scheduler.ScheduledAttempt,
    )
}


def encode(value):
    if is_dataclass(value):
        return {
            "__type__": type(value).__name__,
            "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value) if f.init},
        }
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, (tuple, frozenset, set)):
        kind = "tuple" if isinstance(value, tuple) else "frozenset"
        items = list(value) if kind == "tuple" else sorted(value)
        return {"__collection__": kind, "items": [encode(v) for v in items]}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "__type__" in value:
        cls = _TYPES.get(value["__type__"])
        if cls is None:
            raise ValueError("unrecognized checkpoint type")
        return cls(**{k: decode(v) for k, v in value["fields"].items()})
    if "__date__" in value:
        return date.fromisoformat(value["__date__"])
    if "__decimal__" in value:
        return Decimal(value["__decimal__"])
    if "__collection__" in value:
        cls = {"tuple": tuple, "frozenset": frozenset}[value["__collection__"]]
        return cls(decode(v) for v in value["items"])
    return {k: decode(v) for k, v in value.items()}


def fingerprint(payload):
    return hashlib.sha256(
        json.dumps(encode(payload), sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


def atomic_json(path, payload):
    """No partial final JSON and no overwrite. fsync before publishing by link."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(pending, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        pending.unlink(missing_ok=True)


def checkpoint_write(path, payload):
    encoded = encode(payload)
    atomic_json(path, {"payload": encoded, "sha256": fingerprint(encoded)})


def checkpoint_read(path):
    raw = json.loads(Path(path).read_text())
    if fingerprint(raw["payload"]) != raw["sha256"]:
        raise ValueError("checkpoint checksum mismatch")
    return decode(raw["payload"])


def frame_fingerprint(frame):
    import pandas as pd

    # Includes row order, names and dtypes; never use a Python salted hash.
    digest = hashlib.sha256(pd.util.hash_pandas_object(frame, index=False).values.tobytes())
    digest.update(json.dumps([(str(k), str(v)) for k, v in frame.dtypes.items()]).encode())
    return digest.hexdigest()


def verify_completed(folder):
    from quantlab.research.ml.io import sha256

    folder = Path(folder).resolve()
    receipt = json.loads((folder / "completed.json").read_text())
    for name, digest in receipt["artifacts"].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or sha256(path) != digest:
            raise ValueError(f"completed artifact mismatch:{name}")
    return receipt


def complete(folder):
    from quantlab.research.ml.io import sha256

    folder = Path(folder)
    artifacts = {
        p.relative_to(folder).as_posix(): sha256(p)
        for p in sorted(folder.rglob("*"))
        if p.is_file()
        and p.name != "worker.lock"
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
    }
    atomic_json(folder / "completed.json", {"artifacts": artifacts, "performance_eligible": False})


def write_frame(path, frame):
    """Only regenerable, unsealed aggregate outputs use atomic replacement."""
    path = Path(path)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        frame.to_parquet(pending, index=False)
        with pending.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def publish_ready(work, output, asof, clock):
    """Timestamp AFTER atomic payload publication; a missing receipt is never forward."""
    complete(work)
    work.rename(output)
    return record_publication(output, asof, clock)


def record_publication(output, asof, clock):
    """Conservative recovery after rename: attest availability NOW, never backdate."""
    import pandas as pd

    from quantlab.research.ml.io import sha256

    verify_completed(output)
    published = pd.Timestamp(clock())
    cutoff = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
    forward = published <= cutoff and published.tz_convert("Asia/Shanghai").date() == asof
    receipt = {
        "asof": str(asof),
        "published_at": published.isoformat(),
        "completion_sha256": sha256(output / "completed.json"),
        "forward_eligible": bool(forward),
    }
    atomic_json(output / "published.json", receipt)
    return receipt


def verify_publication(folder, asof, *, require_forward=True):
    import pandas as pd

    from quantlab.research.ml.io import sha256

    verify_completed(folder)
    path = folder / "published.json"
    if not path.exists():
        raise ValueError("publication receipt missing; not a verified forward artifact")
    receipt = json.loads(path.read_text())
    cutoff = pd.Timestamp(asof).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=16)
    stamp = pd.Timestamp(receipt["published_at"])
    valid = stamp <= cutoff and stamp.tz_convert("Asia/Shanghai").date() == asof
    if (
        receipt["asof"] != str(asof)
        or receipt["completion_sha256"] != sha256(folder / "completed.json")
        or receipt["forward_eligible"] != bool(valid)
        or (require_forward and not valid)
    ):
        raise ValueError("publication is not a genuinely archived forward artifact")
    return receipt
