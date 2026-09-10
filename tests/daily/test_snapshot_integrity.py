from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from quantlab.daily.integrity import (
    load_validated_latest_snapshot,
    validate_daily_snapshot_bundle,
)
from quantlab.daily.service import DailySnapshot
from quantlab.data.models import DataValidationError


def _hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot(tmp_path: Path) -> DailySnapshot:
    out = tmp_path / "snapshot"
    out.mkdir()
    ranking = b"instrument_id,alpha_score\n000001.SZ,1.0\n"
    target = b"instrument_id,target_weight\n000001.SZ,1.0\n"
    report_core = {
        "schema": "quantlab_daily_v1",
        "effective_as_of": "2026-09-09",
        "data_status": {
            "status": "complete",
            "inspected_at": "2026-09-10T08:00:00+08:00",
        },
        "model": {"config_id": "test"},
    }
    fingerprint_report = {
        **report_core,
        "data_status": {"status": "complete"},
    }
    fingerprint = _hash(
        {
            "report": fingerprint_report,
            "ranking_sha256": hashlib.sha256(ranking).hexdigest(),
            "target_sha256": hashlib.sha256(target).hexdigest(),
        }
    )
    report = {
        **report_core,
        "generated_at": "2026-09-10T08:00:01+08:00",
        "content_fingerprint": fingerprint,
    }
    report_path = out / "report.json"
    ranking_path = out / "ranking.csv"
    target_path = out / "target_portfolio.csv"
    html_path = out / "report.html"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    ranking_path.write_bytes(ranking)
    target_path.write_bytes(target)
    html_path.write_text("<html></html>", encoding="utf-8")
    return DailySnapshot(
        report_path=report_path,
        ranking_path=ranking_path,
        target_path=target_path,
        html_path=html_path,
        report=report,
        reused=True,
    )


def _active_product_root(tmp_path: Path, snapshot: DailySnapshot) -> Path:
    product_root = tmp_path / "products"
    destination = product_root / "2026-09-09" / "test"
    destination.parent.mkdir(parents=True)
    shutil.copytree(snapshot.report_path.parent, destination)
    fingerprint = snapshot.report["content_fingerprint"]
    (product_root / "ACTIVE.json").write_text(
        json.dumps(
            {
                "schema": "quantlab_daily_active_v1",
                "report_path": "2026-09-09/test/report.json",
                "content_fingerprint": fingerprint,
            }
        ),
        encoding="utf-8",
    )
    return product_root


def test_valid_snapshot_bundle_returns_bound_fingerprint(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert validate_daily_snapshot_bundle(snapshot) == snapshot.report["content_fingerprint"]


def test_validated_loader_checks_active_bundle_before_returning(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    product_root = _active_product_root(tmp_path, snapshot)
    loaded = load_validated_latest_snapshot(product_root)
    assert loaded is not None
    assert loaded.report["content_fingerprint"] == snapshot.report["content_fingerprint"]

    loaded.ranking_path.write_text(
        "instrument_id,alpha_score\n000001.SZ,999.0\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="content fingerprint mismatch"):
        load_validated_latest_snapshot(product_root)


def test_snapshot_integrity_rejects_ranking_mutation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.ranking_path.write_text(
        "instrument_id,alpha_score\n000001.SZ,999.0\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="content fingerprint mismatch"):
        validate_daily_snapshot_bundle(snapshot)


def test_snapshot_integrity_rejects_target_mutation(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.target_path.write_text(
        "instrument_id,target_weight\n000001.SZ,0.5\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="content fingerprint mismatch"):
        validate_daily_snapshot_bundle(snapshot)


def test_snapshot_integrity_rejects_in_memory_report_drift(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.report["effective_as_of"] = "2026-09-10"
    with pytest.raises(DataValidationError, match="in-memory report differs"):
        validate_daily_snapshot_bundle(snapshot)


def test_snapshot_integrity_rejects_missing_bundle_file(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    snapshot.html_path.unlink()
    with pytest.raises(DataValidationError, match="missing files"):
        validate_daily_snapshot_bundle(snapshot)
