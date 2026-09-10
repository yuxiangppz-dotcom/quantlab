from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.evidence_catalog import (
    build_evidence_catalog,
    evidence_by_id,
    summarize_evidence_catalog,
)


def _write(
    root: Path,
    relative: str,
    *,
    schema: str = "portfolio_translation_audit_v1_1",
    run_id: str = "run-1",
    performance_claim: bool = False,
    strategy_promoted: bool = False,
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "run_id": run_id,
                "code_head": "a" * 40,
                "history_status": "retrospective_not_fresh_oos",
                "performance_claim": performance_claim,
                "strategy_promoted": strategy_promoted,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_catalog_indexes_content_bound_evidence_and_ignores_auxiliary_json(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    first = _write(root, "factor/run-1/summary.json")
    _write(
        root,
        "product/run-2/summary.json",
        schema="daily_product_strategy_audit_v1",
        run_id="run-2",
    )
    auxiliary = root / "factor" / "run-1" / "manifest.json"
    auxiliary.write_text(json.dumps({"kind": "auxiliary"}), encoding="utf-8")

    catalog = build_evidence_catalog(root)

    assert len(catalog.entries) == 2
    assert catalog.issues == ()
    entry = next(item for item in catalog.entries if item.run_id == "run-1")
    assert entry.relative_path == "factor/run-1/summary.json"
    assert entry.file_sha256 != entry.evidence_id
    assert len(entry.file_sha256) == 64
    assert len(entry.evidence_id) == 64
    assert evidence_by_id(catalog, entry.evidence_id) == entry

    before = entry.evidence_id
    first.write_text(first.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = build_evidence_catalog(root)
    changed_entry = next(item for item in changed.entries if item.run_id == "run-1")
    assert changed_entry.evidence_id != before
    assert changed.catalog_fingerprint != catalog.catalog_fingerprint


def test_catalog_summary_is_inventory_not_strategy_authority(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    _write(root, "one/summary.json", run_id="one")
    _write(
        root,
        "two/summary.json",
        run_id="two",
        performance_claim=True,
        strategy_promoted=True,
    )

    summary = summarize_evidence_catalog(build_evidence_catalog(root))

    assert summary["entry_count"] == 2
    assert summary["issue_count"] == 0
    assert summary["performance_claim_artifact_count"] == 1
    assert summary["strategy_promoted_artifact_count"] == 1
    assert summary["claim"] == "catalog_inventory_only_not_strategy_authority"


def test_strict_scan_fails_closed_and_non_strict_scan_surfaces_issue(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    _write(root, "good/summary.json")
    bad = root / "bad" / "summary.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")

    with pytest.raises(DataValidationError, match="invalid JSON evidence artifact"):
        build_evidence_catalog(root)

    catalog = build_evidence_catalog(root, strict=False)
    assert len(catalog.entries) == 1
    assert len(catalog.issues) == 1
    assert catalog.issues[0].relative_path == "bad/summary.json"
    assert "invalid JSON evidence artifact" in catalog.issues[0].message


def test_catalog_rejects_claim_fields_with_non_boolean_types(tmp_path: Path) -> None:
    root = tmp_path / "experiments"
    path = root / "run" / "summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "test",
                "run_id": "run",
                "performance_claim": "false",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DataValidationError, match="performance_claim must be boolean"):
        build_evidence_catalog(root)


def test_missing_root_is_a_stable_empty_catalog_and_unknown_id_fails(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    first = build_evidence_catalog(root)
    second = build_evidence_catalog(root)
    assert first == second
    assert first.entries == ()
    assert first.issues == ()
    with pytest.raises(DataValidationError, match="does not resolve exactly once"):
        evidence_by_id(first, "missing")
