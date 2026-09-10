"""Immutable publication of fully staged Daily snapshot bundles."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from quantlab.daily import _service_impl as _impl
from quantlab.data.models import DataValidationError

_BUNDLE_NAMES = {
    "report.json": "report_path",
    "ranking.csv": "ranking_path",
    "target_portfolio.csv": "target_path",
    "report.html": "html_path",
}


def _validate_fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise DataValidationError("Daily snapshot content_fingerprint must be lowercase SHA-256")
    return value


def _snapshot_from_dir(
    out_dir: Path,
    *,
    reused: bool,
) -> _impl.DailySnapshot:
    report_path = out_dir / "report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataValidationError("published Daily report is unreadable or invalid JSON") from exc
    return _impl.DailySnapshot(
        report_path=report_path,
        ranking_path=out_dir / "ranking.csv",
        target_path=out_dir / "target_portfolio.csv",
        html_path=out_dir / "report.html",
        report=report,
        reused=reused,
    )


def _validate_published(out_dir: Path, fingerprint: str, *, reused: bool) -> _impl.DailySnapshot:
    if not out_dir.is_dir():
        raise DataValidationError("Daily content-addressed snapshot path is not a directory")
    snapshot = _snapshot_from_dir(out_dir, reused=reused)
    if snapshot.report.get("content_fingerprint") != fingerprint:
        raise DataValidationError("Daily content-addressed directory fingerprint mismatch")
    # Local import avoids a module-import cycle: integrity imports the public
    # service facade, while publication is invoked only after service import is complete.
    from quantlab.daily.integrity import validate_daily_snapshot_semantics

    validate_daily_snapshot_semantics(snapshot)
    return snapshot


def _write_exact_bundle(staged: _impl.DailySnapshot, temp_dir: Path) -> None:
    paths = {
        "report.json": staged.report_path,
        "ranking.csv": staged.ranking_path,
        "target_portfolio.csv": staged.target_path,
        "report.html": staged.html_path,
    }
    for name, source in paths.items():
        if not source.is_file():
            raise DataValidationError(f"staged Daily bundle missing {name}")
        destination = temp_dir / name
        with source.open("rb") as reader, destination.open("wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())


def commit_staged_daily_snapshot(
    staged: _impl.DailySnapshot,
    product_root: Path,
) -> _impl.DailySnapshot:
    """Publish one complete staged snapshot without overwriting prior evidence."""
    from quantlab.daily.integrity import validate_daily_snapshot_semantics

    validate_daily_snapshot_semantics(staged)
    fingerprint = _validate_fingerprint(staged.report.get("content_fingerprint"))
    effective = staged.report.get("effective_as_of")
    if not isinstance(effective, str) or not effective:
        raise DataValidationError("staged Daily effective_as_of is missing")

    config_version = staged.report_path.parent.name
    if not config_version or config_version in {".", ".."}:
        raise DataValidationError("staged Daily config version is invalid")
    base_dir = product_root / effective / config_version
    out_dir = base_dir / fingerprint

    if out_dir.exists():
        published = _validate_published(out_dir, fingerprint, reused=True)
        _impl._activate_snapshot(product_root, published.report_path, fingerprint)
        return published

    base_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{fingerprint}.tmp-", dir=base_dir))
    try:
        _write_exact_bundle(staged, temp_dir)
        staged_report = json.loads((temp_dir / "report.json").read_text(encoding="utf-8"))
        if staged_report.get("content_fingerprint") != fingerprint:
            raise DataValidationError("staged Daily report fingerprint changed during publication")
        try:
            os.rename(temp_dir, out_dir)
        except FileExistsError:
            # A concurrent publisher won the race. Never replace it; validate
            # the winning immutable bundle and reuse it instead.
            shutil.rmtree(temp_dir, ignore_errors=True)
            published = _validate_published(out_dir, fingerprint, reused=True)
            _impl._activate_snapshot(product_root, published.report_path, fingerprint)
            return published
    except BaseException:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    published = _validate_published(out_dir, fingerprint, reused=False)
    _impl._activate_snapshot(product_root, published.report_path, fingerprint)
    return published
