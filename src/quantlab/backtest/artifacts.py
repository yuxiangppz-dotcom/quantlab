"""Atomic formal-run artifacts: streaming exports, staging, verification.

v0.1.1 root causes closed here:

- ``export_group`` materialized the FULL position row list before building a
  DataFrame (unbounded memory); exports now stream bounded chunks.
- Files were written directly to their final names; every file now goes to a
  ``.tmp`` sidecar first and is ``os.replace``d into place only after a clean
  close, so a crash can never leave a half-written formal file.
- ``summary.json`` was written BEFORE the group exports; the whole run now
  lands in ``<run_id>.incomplete/`` and is promoted to ``<run_id>/`` by an
  atomic rename only after an independent verification passes and a
  ``COMPLETED.json`` marker is written. A failed run can therefore never
  publish a formal-looking directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

INCOMPLETE_MARKER = "INCOMPLETE.json"
COMPLETION_MARKER = "COMPLETED.json"
ARTIFACT_MANIFEST = "artifact_manifest.json"

DEFAULT_CHUNK_ROWS = 50_000

# The standard per-group export family produced by ``export_group``: 12 CSVs
# (each with a stable header, even when empty) plus the failed-attempts JSON.
STANDARD_GROUP_FAMILY: tuple[str, ...] = (
    "daily_records.csv",
    "daily_books.csv",
    "daily_positions.csv",
    "rebalance_log.csv",
    "trade_details.csv",
    "lifecycle_events.csv",
    "risk_policy_audit.csv",
    "forced_exit_attempts.csv",
    "successful_forced_exits.csv",
    "pending_no_price.csv",
    "prevented_entry_refill.csv",
    "blocked_before_exit.csv",
    "failed_attempts.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> int:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    size = tmp.stat().st_size
    os.replace(tmp, path)
    return size


def atomic_write_json(path: Path, payload: Any) -> int:
    return atomic_write_text(path, json.dumps(payload, indent=2, default=str))


def stream_csv(
    path: Path,
    columns: list[str],
    rows: Iterator[dict],
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
) -> tuple[int, int]:
    """Write ``rows`` to ``path`` as CSV in bounded chunks, atomically.

    The full row sequence is never materialized: rows are buffered
    ``chunk_rows`` at a time, appended to the temp file, and only renamed to
    the final path after the stream closes cleanly. An empty stream still
    produces the stable header line.
    """
    tmp = path.with_name(path.name + ".tmp")
    rows_written = 0
    try:
        with tmp.open("w", newline="") as fh:
            header_written = False
            buffer: list[dict] = []
            for row in rows:
                buffer.append(row)
                if len(buffer) >= chunk_rows:
                    pd.DataFrame(buffer, columns=columns).to_csv(
                        fh, index=False, header=not header_written,
                        lineterminator="\n",
                    )
                    rows_written += len(buffer)
                    buffer = []
                    header_written = True
            if buffer or not header_written:
                pd.DataFrame(buffer, columns=columns).to_csv(
                    fh, index=False, header=not header_written,
                    lineterminator="\n",
                )
                rows_written += len(buffer)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    size = tmp.stat().st_size
    os.replace(tmp, path)
    return rows_written, size


def record_rows(records, fields: list[str]) -> Iterator[dict]:
    """Stream dataclass records into CSV-ready dict rows (dates ISO-formatted)."""
    for r in records:
        row = {}
        for name in fields:
            value = getattr(r, name)
            row[name] = value.isoformat() if hasattr(value, "isoformat") else value
        yield row


def verify_formal_artifact(
    run_dir: str | Path,
    expected_registry: Mapping,
    expected_head: str | None = None,
    expected_schema: str | None = None,
) -> dict:
    """Independently verify a formal run directory. Fail-hard on any gap.

    Checks (never trusting ``summary.json`` booleans):

    - every registry group has every required file;
    - both settlement bounds of the primary strategy AND the control are
      present in the registry;
    - per-file sha256/bytes/row count/header match ``artifact_manifest.json``;
    - the manifest itself covers exactly the declared inventory;
    - ``COMPLETED.json`` exists with ``formal_run_valid`` and the manifest
      hash;
    - ``summary.json`` schema and HEAD match the expectations.
    """
    run_dir = Path(run_dir)
    failures: list[str] = []

    groups: Mapping[str, list[str]] = expected_registry["groups"]
    for bound in ("recovery_assumption_1", "recovery_assumption_0"):
        if f"primary_strategy_{bound}" not in groups:
            failures.append(f"registry missing primary_strategy_{bound}")
        if f"equal_weight_v1_control_{bound}" not in groups:
            failures.append(f"registry missing equal_weight_v1_control_{bound}")
    if failures:
        raise RuntimeError("registry incomplete: " + "; ".join(failures))

    manifest_path = run_dir / ARTIFACT_MANIFEST
    if not manifest_path.exists():
        raise RuntimeError(f"missing {ARTIFACT_MANIFEST}")
    manifest = json.loads(manifest_path.read_text())
    manifest_files: Mapping = manifest["files"]

    declared_inventory = set(expected_registry["top_level"])
    for group, family in groups.items():
        for kind in family:
            declared_inventory.add(f"{group}_{kind}")

    actual_files = {
        p.name for p in run_dir.iterdir()
        if p.is_file() and not p.name.endswith(".tmp")
    }
    missing = sorted(declared_inventory - actual_files)
    if missing:
        raise RuntimeError(f"missing required files: {missing}")
    unexpected = sorted(actual_files - declared_inventory - {
        ARTIFACT_MANIFEST, COMPLETION_MARKER, INCOMPLETE_MARKER,
    })
    if unexpected:
        raise RuntimeError(f"undeclared files present: {unexpected}")

    recorded_manifest_sha = None
    completion_path = run_dir / COMPLETION_MARKER
    if completion_path.exists():
        completed = json.loads(completion_path.read_text())
        recorded_manifest_sha = completed.get("artifact_manifest_sha256")
        if completed.get("formal_run_valid") is not True:
            failures.append("completion marker does not state formal_run_valid")
        if (
            recorded_manifest_sha
            and recorded_manifest_sha != sha256_file(manifest_path)
        ):
            failures.append("completion marker manifest hash mismatch")
    else:
        failures.append(f"missing {COMPLETION_MARKER}")

    for name in sorted(declared_inventory):
        path = run_dir / name
        recorded = manifest_files.get(name)
        if recorded is None:
            failures.append(f"{name}: not in artifact manifest")
            continue
        if sha256_file(path) != recorded.get("sha256"):
            failures.append(f"{name}: sha256 mismatch")
        if path.stat().st_size != recorded.get("bytes"):
            failures.append(f"{name}: size mismatch")
        if name.endswith(".csv"):
            with path.open() as fh:
                header = fh.readline().rstrip("\n")
                actual_rows = sum(1 for _ in fh)
            if header != recorded.get("header", ""):
                failures.append(f"{name}: header mismatch")
            if actual_rows != recorded.get("rows"):
                failures.append(f"{name}: row count mismatch "
                                f"({actual_rows} != {recorded.get('rows')})")

    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        failures.append("missing summary.json")
    else:
        summary = json.loads(summary_path.read_text())
        if expected_head is not None and summary.get("code_version") != expected_head:
            failures.append(
                f"summary HEAD {summary.get('code_version')} != {expected_head}"
            )
        if (
            expected_schema is not None
            and summary.get("experiment_schema") != expected_schema
        ):
            failures.append("summary experiment_schema mismatch")

    if failures:
        raise RuntimeError("artifact verification failed: " + "; ".join(failures))
    return {
        "complete": True,
        "verified_files": len(declared_inventory),
        "artifact_manifest_sha256": sha256_file(manifest_path),
    }


class ArtifactPublisher:
    """Stage a formal run under ``<run_id>.incomplete`` and promote it
    atomically once every registered artifact verifies."""

    def __init__(
        self,
        out_root: str | Path,
        run_id: str,
        expected_registry: Mapping,
        head: str | None,
        schema: str | None = None,
    ) -> None:
        self.out_root = Path(out_root)
        self.run_id = run_id
        self.staging = self.out_root / f"{run_id}.incomplete"
        self.final = self.out_root / run_id
        self.expected_registry = expected_registry
        self.head = head
        self.schema = schema
        self.staging.mkdir(parents=True, exist_ok=True)
        if self.final.exists():
            raise RuntimeError(
                f"final run directory {self.final} already exists; refusing "
                "to overwrite a published formal artifact"
            )

    # -- file collection ---------------------------------------------------

    def build_artifact_manifest(self, summary: Mapping | None = None) -> dict:
        """Inventory + integrity record for every declared file."""
        summary = summary or {}
        declared_inventory: dict[str, str] = {}
        for group, family in self.expected_registry["groups"].items():
            for kind in family:
                declared_inventory[f"{group}_{kind}"] = "group"
        for name in self.expected_registry["top_level"]:
            declared_inventory[name] = "top_level"

        files: dict[str, dict] = {}
        for name in sorted(declared_inventory):
            path = self.staging / name
            if not path.exists():
                continue
            entry: dict = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
            if name.endswith(".csv"):
                with path.open() as fh:
                    entry["header"] = fh.readline().rstrip("\n")
                    entry["rows"] = sum(1 for _ in fh)
            files[name] = entry
        return {
            "run_id": self.run_id,
            "head": self.head,
            "schema": self.schema,
            "groups": dict(self.expected_registry["groups"]),
            "top_level": list(self.expected_registry["top_level"]),
            "files": files,
            "summary_head": summary.get("code_version"),
        }

    # -- publication -------------------------------------------------------

    def publish(self, summary: Mapping | None = None) -> Path:
        """Hash everything, write the manifest + completion marker, verify,
        and only then atomically promote staging to the final run directory."""
        manifest = self.build_artifact_manifest(summary)
        manifest_path = self.staging / ARTIFACT_MANIFEST
        atomic_write_json(manifest_path, manifest)
        marker_path = self.staging / COMPLETION_MARKER
        atomic_write_json(
            marker_path,
            {
                "status": "complete",
                "formal_run_valid": True,
                "run_id": self.run_id,
                "head": self.head,
                "schema": self.schema,
                "artifact_manifest_sha256": sha256_file(manifest_path),
                "completed_at": datetime.now().isoformat(),
                "verifier": "inline_verify_pending_promotion",
            },
        )
        try:
            result = verify_formal_artifact(
                self.staging,
                self.expected_registry,
                expected_head=self.head,
                expected_schema=self.schema,
            )
        except RuntimeError as exc:
            marker_path.unlink(missing_ok=True)
            self.mark_incomplete(exc)
            raise
        atomic_write_json(
            marker_path,
            {
                "status": "complete",
                "formal_run_valid": True,
                "run_id": self.run_id,
                "head": self.head,
                "schema": self.schema,
                "artifact_manifest_sha256": sha256_file(manifest_path),
                "completed_at": datetime.now().isoformat(),
                "verifier": result,
            },
        )
        os.replace(self.staging, self.final)
        return self.final

    def mark_incomplete(self, reason: BaseException | str) -> None:
        atomic_write_json(
            self.staging / INCOMPLETE_MARKER,
            {
                "status": "incomplete",
                "run_id": self.run_id,
                "head": self.head,
                "reason": str(reason),
                "note": (
                    "this staged directory is NOT a formal artifact; it must "
                    "not be cited as a performance baseline and "
                    "performance_valid / formal_run_valid do not hold"
                ),
            },
        )
