"""Atomic formal-run artifacts: streaming exports, staging, verification.

Fail-closed publication state machine (v0.1.3):

- ``export_group`` streams bounded chunks and writes every file via a
  ``.tmp`` sidecar + ``os.replace``, so a crash never leaves a half-written
  formal file.
- The whole run lands in ``<run_id>.incomplete/``. Publication is:
  manifest -> preflight verification (no completion marker involved) ->
  atomic promotion -> completion marker written INSIDE the promoted
  directory (the commit point) -> formal verification.
- A ``COMPLETED.json`` inside a ``.incomplete`` directory is always invalid:
  the formal verifier rejects staged directories unconditionally, and
  ``mark_incomplete`` removes any marker a failed attempt left behind. A
  crash before the marker write leaves a promoted directory that fails
  formal verification for lack of the marker; a crash after it leaves an
  artifact whose claims the verifier re-derives from the bytes on disk.
- The formal verifier cross-binds directory basename, run id, HEAD, schema,
  summary, manifest, marker and the canonical registry; nothing is trusted
  from recorded booleans.
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


def _parse_json_file(path: Path, failures: list[str], label: str) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        failures.append(f"missing {label}")
    except (ValueError, OSError) as exc:
        failures.append(f"{label}: unparseable ({exc})")
    return None


def verify_formal_artifact(
    run_dir: str | Path,
    expected_registry: Mapping,
    expected_run_id: str | None = None,
    expected_head: str | None = None,
    expected_schema: str | None = None,
    formal: bool = True,
) -> dict:
    """Cross-bind and verify a run directory. Fail-hard on any gap.

    ``formal=True`` is the publication gate: it requires the completion
    marker inside a directory whose basename is exactly the run id, and it
    rejects ``.incomplete`` directories unconditionally — a staging
    directory can never pass formal verification, no matter what its
    ``COMPLETED.json`` claims. ``formal=False`` is the publisher-internal
    preflight: identical payload/metadata binding, but the completion
    marker is not required (and must not exist yet).

    Checks (never trusting any recorded boolean — everything is re-derived
    from the bytes on disk):

    - registry: both settlement bounds of the primary strategy AND the
      control are present;
    - manifest: ``run_id``/``head``/``schema``/``summary_head`` match the
      directory, the summary, and the external expectations; the manifest's
      ``groups``/``top_level`` equal the canonical registry exactly and its
      ``files`` keys equal the declared inventory exactly;
    - payload: every declared file's sha256/bytes/header/row count match the
      manifest; the actual file set equals the declared inventory exactly
      (no missing, no extra, no ``.tmp``/``.partial``, no
      ``INCOMPLETE.json``);
    - summary: ``run_id``/``code_version``/``experiment_schema`` present and
      consistent with the externals;
    - marker (formal only): ``status == "complete"`` and
      ``formal_run_valid is True``, ``run_id``/``head``/``schema`` bound,
      ``artifact_manifest_sha256`` present and equal to the manifest file's
      hash, and completed (non-pending) evidence records;
    - directory: basename equals the run id (formal mode).
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

    is_staging = run_dir.name.endswith(".incomplete")
    dir_run_id = run_dir.name[: -len(".incomplete")] if is_staging else run_dir.name
    if formal and is_staging:
        # fail-closed: a staging directory can never pass formal
        # verification, even if it contains a "successful" completion marker
        raise RuntimeError(
            f"fail-closed: {run_dir.name} is a staged .incomplete directory; "
            "formal verification applies only to promoted run directories"
        )
    if expected_run_id is not None and dir_run_id != expected_run_id:
        failures.append(
            f"directory run id {dir_run_id!r} != expected {expected_run_id!r}"
        )
    if (run_dir / INCOMPLETE_MARKER).exists():
        failures.append(f"{INCOMPLETE_MARKER} present: directory is not formal")

    manifest_path = run_dir / ARTIFACT_MANIFEST
    if not manifest_path.exists():
        raise RuntimeError(f"missing {ARTIFACT_MANIFEST}")
    manifest = _parse_json_file(manifest_path, failures, ARTIFACT_MANIFEST)
    if manifest is None:
        raise RuntimeError("artifact verification failed: " + "; ".join(failures))
    manifest_files: Mapping = manifest.get("files") or {}

    declared_inventory = {
        f"{group}_{kind}"
        for group, family in groups.items()
        for kind in family
    } | set(expected_registry["top_level"])

    # -- manifest metadata binding -----------------------------------------
    for field, expected in (
        ("run_id", expected_run_id if expected_run_id is not None else dir_run_id),
        ("head", expected_head),
        ("schema", expected_schema),
    ):
        if expected is not None and manifest.get(field) != expected:
            failures.append(
                f"manifest {field} {manifest.get(field)!r} != {expected!r}"
            )
    if manifest.get("groups") != dict(groups):
        failures.append("manifest groups/top_level registry differs from canonical")
    if set(manifest.get("top_level") or ()) != set(expected_registry["top_level"]):
        failures.append("manifest top_level registry differs from canonical")
    if set(manifest_files) != declared_inventory:
        missing_in_manifest = sorted(declared_inventory - set(manifest_files))
        extra_in_manifest = sorted(set(manifest_files) - declared_inventory)
        failures.append(
            "manifest inventory mismatch: "
            f"missing={missing_in_manifest} extra={extra_in_manifest}"
        )

    # -- actual payload files ------------------------------------------------
    actual_files = {p.name for p in run_dir.iterdir() if p.is_file()}
    declared_with_extras = declared_inventory | {ARTIFACT_MANIFEST}
    if formal:
        declared_with_extras.add(COMPLETION_MARKER)
    missing = sorted(declared_inventory - actual_files)
    if missing:
        failures.append(f"missing required files: {missing}")
    unexpected = sorted(actual_files - declared_with_extras)
    if unexpected:
        failures.append(f"undeclared files present: {unexpected}")
    temp_files = sorted(
        name for name in actual_files
        if name.endswith(".tmp") or name.endswith(".partial")
    )
    if temp_files:
        failures.append(f"temporary/partial files present: {temp_files}")

    # -- payload integrity ---------------------------------------------------
    for name in sorted(declared_inventory & actual_files):
        path = run_dir / name
        recorded = manifest_files.get(name)
        if recorded is None:
            continue  # already reported as a manifest inventory mismatch
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

    # -- summary binding -------------------------------------------------------
    summary = _parse_json_file(run_dir / "summary.json", failures, "summary.json")
    if summary is not None:
        if summary.get("run_id") != dir_run_id:
            failures.append(
                f"summary run_id {summary.get('run_id')!r} != {dir_run_id!r}"
            )
        if (
            expected_head is not None
            and summary.get("code_version") != expected_head
        ):
            failures.append(
                f"summary HEAD {summary.get('code_version')!r} != {expected_head!r}"
            )
        if (
            expected_schema is not None
            and summary.get("experiment_schema") != expected_schema
        ):
            failures.append("summary experiment_schema mismatch")
        if manifest is not None and manifest.get("summary_head") not in (
            None, summary.get("code_version"),
        ):
            failures.append("manifest summary_head disagrees with summary")
    if failures and not formal:
        raise RuntimeError("preflight verification failed: " + "; ".join(failures))
    if failures:
        raise RuntimeError("artifact verification failed: " + "; ".join(failures))

    # -- completion marker (formal only; the commit point) -------------------
    if formal:
        marker = _parse_json_file(
            run_dir / COMPLETION_MARKER, failures, COMPLETION_MARKER
        )
        if marker is None:
            failures.append(f"missing or unparseable {COMPLETION_MARKER}")
        else:
            if marker.get("status") != "complete":
                failures.append("completion marker status is not 'complete'")
            if marker.get("formal_run_valid") is not True:
                failures.append("completion marker does not state formal_run_valid")
            for field, expected in (
                ("run_id", dir_run_id),
                (
                    "head",
                    expected_head if expected_head is not None else manifest.get("head"),
                ),
                (
                    "schema",
                    expected_schema if expected_schema is not None else manifest.get("schema"),
                ),
            ):
                if expected is not None and marker.get(field) != expected:
                    failures.append(
                        f"completion marker {field} {marker.get(field)!r} != {expected!r}"
                    )
            recorded_sha = marker.get("artifact_manifest_sha256")
            if not recorded_sha:
                failures.append("completion marker missing artifact_manifest_sha256")
            elif recorded_sha != sha256_file(manifest_path):
                failures.append("completion marker manifest hash mismatch")
            evidence = marker.get("preflight")
            if not isinstance(evidence, dict) or evidence.get("complete") is not True:
                failures.append(
                    "completion marker evidence is not a completed preflight record"
                )
    if failures:
        raise RuntimeError("artifact verification failed: " + "; ".join(failures))
    return {
        "complete": True,
        "verified_files": len(declared_inventory),
        "artifact_manifest_sha256": sha256_file(manifest_path),
        "mode": "formal" if formal else "preflight",
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
        """Fail-closed publication state machine.

        1. write the artifact manifest into staging;
        2. preflight-verify staging (payload/metadata binding; a completion
           marker inside staging is invalid and is not required here);
        3. atomically promote staging to the final run-id directory;
        4. atomically write ``COMPLETED.json`` INSIDE the final directory
           (the commit point; carries the completed preflight evidence);
        5. formal-verify the final directory with the full metadata binding.

        The marker is never written before its evidence exists, and it is
        always written inside an already-promoted directory — a crash at any
        point leaves a state the formal verifier rejects (no marker inside
        staging is honored; a promoted directory without a marker fails).
        """
        manifest = self.build_artifact_manifest(summary)
        manifest_path = self.staging / ARTIFACT_MANIFEST
        atomic_write_json(manifest_path, manifest)
        try:
            preflight = verify_formal_artifact(
                self.staging,
                self.expected_registry,
                expected_run_id=self.run_id,
                expected_head=self.head,
                expected_schema=self.schema,
                formal=False,
            )
        except BaseException as exc:
            self.mark_incomplete(exc)
            raise

        try:
            os.replace(self.staging, self.final)
        except BaseException as exc:
            self.mark_incomplete(exc)
            raise

        try:
            atomic_write_json(
                self.final / COMPLETION_MARKER,
                {
                    "status": "complete",
                    "formal_run_valid": True,
                    "run_id": self.run_id,
                    "head": self.head,
                    "schema": self.schema,
                    "artifact_manifest_sha256": sha256_file(
                        self.final / ARTIFACT_MANIFEST
                    ),
                    "completed_at": datetime.now().isoformat(),
                    "preflight": preflight,
                },
            )
            verify_formal_artifact(
                self.final,
                self.expected_registry,
                expected_run_id=self.run_id,
                expected_head=self.head,
                expected_schema=self.schema,
                formal=True,
            )
        except BaseException as exc:
            # fail-closed: strip any (possibly half-written) marker, record
            # the explicit incomplete state, and never leave a directory
            # that could be mistaken for formal
            (self.final / COMPLETION_MARKER).unlink(missing_ok=True)
            self._mark_final_incomplete(exc)
            raise
        return self.final

    def mark_incomplete(self, reason: BaseException | str) -> None:
        """Explicitly invalidate the staging attempt: any completion marker
        present is removed — a .incomplete directory can never claim
        formal_run_valid, whatever its contents."""
        (self.staging / COMPLETION_MARKER).unlink(missing_ok=True)
        (self.staging / (COMPLETION_MARKER + ".tmp")).unlink(missing_ok=True)
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

    def _mark_final_incomplete(self, reason: BaseException | str) -> None:
        """Explicitly invalidate a PROMOTED directory whose formal
        verification failed (no valid COMPLETED.json exists at this point,
        so COMPLETED/INCOMPLETE can never coexist)."""
        atomic_write_json(
            self.final / INCOMPLETE_MARKER,
            {
                "status": "incomplete",
                "run_id": self.run_id,
                "head": self.head,
                "reason": str(reason),
                "note": (
                    "this promoted directory failed formal verification and "
                    "is NOT a formal artifact; performance_valid / "
                    "formal_run_valid do not hold"
                ),
            },
        )
