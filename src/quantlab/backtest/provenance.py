"""Provenance helpers: content fingerprints for code and data."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_manifest(paths: list[Path], root: Path) -> dict:
    """Per-file digest keyed by repo-relative path, plus a combined digest.

    Uses the repo-relative path (not just the basename) so same-named files in
    different directories are distinguishable.
    """
    h = hashlib.sha256()
    entries: list[dict] = []
    for path in sorted(set(paths)):
        rel = str(path.resolve().relative_to(root.resolve()))
        digest = sha256_file(path)
        size = path.stat().st_size
        entries.append({"path": rel, "size_bytes": size, "sha256": digest})
        h.update(rel.encode())
        h.update(digest.encode())
    return {"combined_sha256": h.hexdigest(), "files": entries}


def environment_info() -> dict:
    """Return the Python and key dependency versions used by this process."""
    versions: dict[str, str] = {}
    for name in ("pandas", "pyarrow", "tushare"):
        try:
            mod = __import__(name)
            versions[name] = getattr(mod, "__version__", "unknown")
        except Exception:
            versions[name] = "unavailable"
    return {
        "python": sys.version.split()[0],
        "dependencies": versions,
    }


def formal_reproducibility_evidence(
    inputs_stable_during_run: bool,
    git_clean_before: bool,
    git_clean_after: bool,
    head_unchanged: bool,
    code_manifest_unchanged: bool,
    data_manifest_unchanged: bool,
) -> dict:
    """Assemble the formal reproducibility evidence record.

    Formal reproducibility is stronger than "inputs did not change while the
    process ran": a run from a dirty workspace (or one whose HEAD moved, or
    whose tracked code / canonical data manifests changed) can never publish
    formally reproducible, performance-valid metrics, because the exact code
    that produced the numbers cannot be reconstructed from a commit.

    ``inputs_stable_during_run`` keeps the old within-run semantics (code and
    data manifests identical before and after the backtests) for
    transparency, but on its own it is not sufficient.
    """
    evidence = {
        "inputs_stable_during_run": inputs_stable_during_run,
        "git_clean_before": git_clean_before,
        "git_clean_after": git_clean_after,
        "head_unchanged": head_unchanged,
        "code_manifest_unchanged": code_manifest_unchanged,
        "data_manifest_unchanged": data_manifest_unchanged,
    }
    evidence["formal_reproducible"] = all(evidence.values())
    return evidence
