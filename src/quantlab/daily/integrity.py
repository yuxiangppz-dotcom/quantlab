"""Integrity checks for materialized Daily product snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from quantlab.data.models import DataValidationError
from quantlab.daily.service import DailySnapshot


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _fingerprint_report(report: dict) -> dict:
    core = {
        key: value
        for key, value in report.items()
        if key not in {"generated_at", "content_fingerprint"}
    }
    data_status = core.get("data_status")
    if isinstance(data_status, dict):
        core["data_status"] = {
            key: value for key, value in data_status.items() if key != "inspected_at"
        }
    return core


def validate_daily_snapshot_bundle(snapshot: DailySnapshot) -> str:
    """Validate report-to-ranking/target binding for a materialized snapshot.

    Daily's content fingerprint is defined over the report core plus the exact
    ranking and target CSV bytes. Consumers that create portfolio plans or
    forward evidence must call this before trusting the snapshot. The check is
    intentionally fail-closed: missing files, report drift, or CSV mutation are
    errors rather than reasons to silently fall back to an older snapshot.

    Returns the validated content fingerprint.
    """
    required_paths = {
        "report": snapshot.report_path,
        "ranking": snapshot.ranking_path,
        "target": snapshot.target_path,
        "html": snapshot.html_path,
    }
    missing = [name for name, path in required_paths.items() if not path.is_file()]
    if missing:
        raise DataValidationError(f"Daily snapshot bundle missing files: {sorted(missing)}")

    try:
        disk_report = json.loads(snapshot.report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataValidationError("Daily snapshot report is unreadable or invalid JSON") from exc
    if disk_report != snapshot.report:
        raise DataValidationError("Daily snapshot in-memory report differs from report.json")

    fingerprint = disk_report.get("content_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise DataValidationError("Daily snapshot content fingerprint is missing or invalid")

    ranking_sha = _sha256_bytes(snapshot.ranking_path.read_bytes())
    target_sha = _sha256_bytes(snapshot.target_path.read_bytes())
    actual = _canonical_hash(
        {
            "report": _fingerprint_report(disk_report),
            "ranking_sha256": ranking_sha,
            "target_sha256": target_sha,
        }
    )
    if actual != fingerprint:
        raise DataValidationError("Daily snapshot content fingerprint mismatch")
    return fingerprint
