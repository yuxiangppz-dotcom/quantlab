"""Read-only catalog over research evidence artifacts.

The catalog indexes immutable JSON evidence already written under an experiment
root. It never interprets a favorable metric as strategy approval and never
rewrites research artifacts. Exact file bytes are hashed so later evidence
references can be bound to a concrete artifact rather than a mutable pathname.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from quantlab.data.models import DataValidationError


@dataclass(frozen=True)
class EvidenceCatalogEntry:
    evidence_id: str
    relative_path: str
    schema: str
    run_id: str
    file_sha256: str
    code_head: str | None
    history_status: str | None
    performance_claim: bool | None
    strategy_promoted: bool | None


@dataclass(frozen=True)
class EvidenceCatalogIssue:
    relative_path: str
    message: str


@dataclass(frozen=True)
class EvidenceCatalog:
    entries: tuple[EvidenceCatalogEntry, ...]
    issues: tuple[EvidenceCatalogIssue, ...]
    catalog_fingerprint: str


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bool_or_none(value: object, field: str, path: Path) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DataValidationError(f"{path}: {field} must be boolean when present")
    return value


def _string_or_none(value: object, field: str, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"{path}: {field} must be a non-empty string when present")
    return value.strip()


def _parse_entry(root: Path, path: Path) -> EvidenceCatalogEntry | None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise DataValidationError(f"experiment artifact escapes evidence root: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataValidationError(f"invalid JSON evidence artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise DataValidationError(f"evidence artifact must contain a JSON object: {path}")

    schema = payload.get("schema")
    run_id = payload.get("run_id")
    # JSON files such as auxiliary manifests may legitimately live under an
    # experiment directory. A catalog entry is intentionally limited to
    # run-addressable evidence with both schema and run_id.
    if schema is None and run_id is None:
        return None
    if not isinstance(schema, str) or not schema.strip():
        raise DataValidationError(f"{path}: evidence schema must be a non-empty string")
    if not isinstance(run_id, str) or not run_id.strip():
        raise DataValidationError(f"{path}: evidence run_id must be a non-empty string")

    relative = resolved_path.relative_to(resolved_root).as_posix()
    file_sha = hashlib.sha256(raw).hexdigest()
    identity = {
        "relative_path": relative,
        "schema": schema.strip(),
        "run_id": run_id.strip(),
        "file_sha256": file_sha,
    }
    return EvidenceCatalogEntry(
        evidence_id=_canonical_hash(identity),
        relative_path=relative,
        schema=schema.strip(),
        run_id=run_id.strip(),
        file_sha256=file_sha,
        code_head=_string_or_none(payload.get("code_head"), "code_head", path),
        history_status=_string_or_none(
            payload.get("history_status"), "history_status", path
        ),
        performance_claim=_bool_or_none(
            payload.get("performance_claim"), "performance_claim", path
        ),
        strategy_promoted=_bool_or_none(
            payload.get("strategy_promoted"), "strategy_promoted", path
        ),
    )


def build_evidence_catalog(
    root: Path,
    *,
    strict: bool = True,
) -> EvidenceCatalog:
    """Index run-addressable JSON artifacts below ``root``.

    In strict mode any malformed evidence stops the scan. In non-strict mode the
    bad artifact is reported in ``issues`` and valid evidence remains visible.
    Neither mode mutates the evidence store.
    """
    if not root.exists():
        return EvidenceCatalog(entries=(), issues=(), catalog_fingerprint=_canonical_hash([]))
    if not root.is_dir():
        raise DataValidationError("evidence catalog root must be a directory")

    entries: list[EvidenceCatalogEntry] = []
    issues: list[EvidenceCatalogIssue] = []
    for path in sorted(root.rglob("*.json")):
        relative = path.relative_to(root).as_posix()
        try:
            entry = _parse_entry(root, path)
        except (OSError, DataValidationError) as exc:
            if strict:
                if isinstance(exc, DataValidationError):
                    raise
                raise DataValidationError(f"cannot read evidence artifact: {path}") from exc
            issues.append(EvidenceCatalogIssue(relative, str(exc)))
            continue
        if entry is not None:
            entries.append(entry)

    keys = [(entry.schema, entry.run_id, entry.relative_path) for entry in entries]
    if len(keys) != len(set(keys)):
        raise DataValidationError("duplicate schema/run_id/path identity in evidence catalog")
    evidence_ids = [entry.evidence_id for entry in entries]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise DataValidationError("duplicate evidence_id in evidence catalog")

    entries.sort(key=lambda item: (item.schema, item.run_id, item.relative_path))
    issues.sort(key=lambda item: item.relative_path)
    fingerprint_payload = {
        "entries": [asdict(item) for item in entries],
        "issues": [asdict(item) for item in issues],
    }
    return EvidenceCatalog(
        entries=tuple(entries),
        issues=tuple(issues),
        catalog_fingerprint=_canonical_hash(fingerprint_payload),
    )


def evidence_by_id(catalog: EvidenceCatalog, evidence_id: str) -> EvidenceCatalogEntry:
    """Resolve one exact evidence artifact by its content-bound ID."""
    matches = [entry for entry in catalog.entries if entry.evidence_id == evidence_id]
    if len(matches) != 1:
        raise DataValidationError(f"evidence_id does not resolve exactly once: {evidence_id}")
    return matches[0]


def summarize_evidence_catalog(catalog: EvidenceCatalog) -> dict:
    """Return claim-neutral counts for UI/CLI presentation."""
    by_schema: dict[str, int] = {}
    promoted_count = 0
    performance_claim_count = 0
    for entry in catalog.entries:
        by_schema[entry.schema] = by_schema.get(entry.schema, 0) + 1
        promoted_count += entry.strategy_promoted is True
        performance_claim_count += entry.performance_claim is True
    return {
        "schema": "quantlab_evidence_catalog_summary_v1",
        "entry_count": len(catalog.entries),
        "issue_count": len(catalog.issues),
        "by_schema": dict(sorted(by_schema.items())),
        "performance_claim_artifact_count": performance_claim_count,
        "strategy_promoted_artifact_count": promoted_count,
        "catalog_fingerprint": catalog.catalog_fingerprint,
        "claim": "catalog_inventory_only_not_strategy_authority",
    }
