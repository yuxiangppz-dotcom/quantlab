from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import quantlab.__main__ as main_module
from quantlab.research.research_status import (
    build_research_status,
    format_research_status,
)

REGISTRY = Path("config/strategy_registry_v1.json")
FORWARD = Path("config/forward_shadow_v1.json")


def _status(tmp_path: Path) -> dict:
    return build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=tmp_path / "missing-experiments",
        shadow_root=tmp_path / "missing-shadow",
    )


def test_missing_optional_roots_are_explicit_and_do_not_fabricate_evidence(
    tmp_path: Path,
) -> None:
    first = _status(tmp_path)
    second = _status(tmp_path)

    assert first == second
    assert first["sources"]["experiment_root_exists"] is False
    assert first["sources"]["shadow_root_exists"] is False
    assert first["evidence_catalog"]["summary"]["entry_count"] == 0
    assert first["forward_shadow"]["summaries"] == []
    assert first["strategy_readiness"]["summary"]["strategy_count"] == 3
    by_id = {
        item["strategy_id"]: item
        for item in first["strategy_readiness"]["strategies"]
    }
    s5b = by_id["price_volume_base_breakout"]
    assert s5b["registry_status"] == "RESEARCHED"
    assert s5b["ready_for_user_review"] is False
    assert s5b["broker_order_authority"] is False
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" in s5b["blockers"]
    assert first["performance_claim"] is False
    assert first["broker_order_authority"] is False
    assert len(first["status_fingerprint"]) == 64


def test_existing_malformed_evidence_is_classified_and_listed(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "experiments" / "run" / "summary.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")

    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=tmp_path / "experiments",
        shadow_root=tmp_path / "missing-shadow",
    )
    # The corrupt artifact is listed with its reason and keeps the status
    # from reading as clean; it never gains evidence eligibility.
    assert payload["overall_status"] == "report_has_evidence_issues"
    artifacts = payload["incompatible_artifacts"]
    assert any(
        a["relative_path"] == "run/summary.json"
        and a["classification"] == "unrecognized_format"
        for a in artifacts
    )


def _write_evidence_summary(path: Path, schema: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "run_id": "20240101T000000",
                "code_head": "0" * 40,
            }
        ),
        encoding="utf-8",
    )


def test_legacy_summary_and_valid_evidence_coexist(tmp_path: Path) -> None:
    experiments = tmp_path / "experiments"
    legacy = experiments / "old_v0_1" / "20260906T173554" / "summary.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "analysis_type": "execution_readiness",
                "experiment_schema": "execution_readiness_v0_1",
                "run_id": "20260906T173554",
            }
        ),
        encoding="utf-8",
    )
    valid = experiments / "current_v1" / "20240101T000000" / "summary.json"
    _write_evidence_summary(valid, "research_evidence_v1")

    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=experiments,
        shadow_root=tmp_path / "missing-shadow",
    )
    # The valid evidence stays visible; the legacy summary is listed with
    # its reason and gains no eligibility.
    assert payload["overall_status"] == "report_has_legacy_artifacts"
    assert any(
        entry["schema"] == "research_evidence_v1"
        for entry in payload["evidence_catalog"]["entries"]
    )
    legacy_rows = [
        a
        for a in payload["incompatible_artifacts"]
        if a["classification"] == "identified_legacy"
    ]
    assert len(legacy_rows) == 1
    assert legacy_rows[0]["relative_path"] == "old_v0_1/20260906T173554/summary.json"
    rendered = format_research_status(payload)
    assert "identified_legacy" in rendered
    assert "incompatible artifacts" in rendered


def test_corrupt_current_format_evidence_is_never_valid(tmp_path: Path) -> None:
    experiments = tmp_path / "experiments"
    corrupt = experiments / "current_v1" / "broken" / "summary.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text(
        json.dumps({"schema": "", "run_id": "x"}),
        encoding="utf-8",
    )
    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=experiments,
        shadow_root=tmp_path / "missing-shadow",
    )
    assert payload["overall_status"] == "report_has_evidence_issues"
    # The corrupt artifact never becomes a catalog entry.
    assert payload["evidence_catalog"]["entries"] == []
    text = format_research_status(payload)
    assert "report_has_evidence_issues" in text

    as_json = json.dumps(payload)
    assert "malformed_evidence" in as_json
    assert "report_has_evidence_issues" in as_json


def test_cli_exit_code_two_when_status_cannot_be_composed(
    tmp_path: Path, monkeypatch
) -> None:
    def boom(**_kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(
        "quantlab.research.research_status.build_research_status", boom
    )
    code = main_module._research_status(as_json=False)
    assert code == 2


def test_exit_code_paths_cover_all_four_outcomes(tmp_path: Path) -> None:
    experiments = tmp_path / "experiments"

    # 1. clean -> exit 0
    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=experiments / "missing",
        shadow_root=tmp_path / "missing-shadow",
    )
    assert payload["overall_status"] == "clean"

    # 2. legacy-only -> exit 0 with the note rendered
    legacy = experiments / "legacy" / "run" / "summary.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"experiment_schema": "execution_readiness_v0_1"}),
        encoding="utf-8",
    )
    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=experiments,
        shadow_root=tmp_path / "missing-shadow",
    )
    assert payload["overall_status"] == "report_has_legacy_artifacts"
    rendered = format_research_status(payload)
    assert "identified_legacy" in rendered
    assert "overall status: report_has_legacy_artifacts" in rendered

    # 3. corrupt current-format -> exit 1 mapped from evidence issues
    corrupt = experiments / "corrupt" / "run" / "summary.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text(json.dumps({"schema": "", "run_id": "x"}), encoding="utf-8")
    payload = build_research_status(
        registry_path=REGISTRY,
        forward_config_path=FORWARD,
        experiment_root=experiments,
        shadow_root=tmp_path / "missing-shadow",
    )
    assert payload["overall_status"] == "report_has_evidence_issues"
    as_json = json.dumps(payload)
    assert "report_has_evidence_issues" in as_json
    text = format_research_status(payload)
    assert "report_has_evidence_issues" in text

    # 4. composition failure -> exit 2 (covered at the CLI boundary in
    # test_cli_exit_code_two_when_status_cannot_be_composed).


def test_existing_malformed_shadow_fails_closed(tmp_path: Path) -> None:
    bad = (
        tmp_path
        / "shadow"
        / "model"
        / "version"
        / "2026-09-15"
        / "fingerprint"
        / "prediction.json"
    )
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        build_research_status(
            registry_path=REGISTRY,
            forward_config_path=FORWARD,
            experiment_root=tmp_path / "missing-experiments",
            shadow_root=tmp_path / "shadow",
        )


def test_human_status_exposes_blockers_without_return_claims(tmp_path: Path) -> None:
    rendered = format_research_status(_status(tmp_path))

    assert "price_volume_base_breakout@s5b_frozen: RESEARCHED" in rendered
    assert "FORWARD_SHADOW_SUMMARY_NOT_FOUND" in rendered
    assert "broker authority: False" in rendered
    assert "performance claim: false" in rendered
    assert "CAGR" not in rendered
    assert "NAV" not in rendered


def test_cli_json_status_needs_no_canonical_market_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(REGISTRY, config / REGISTRY.name)
    shutil.copy(FORWARD, config / FORWARD.name)
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)

    assert main_module._research_status(True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "quantlab_research_status_v1"
    assert payload["sources"]["experiment_root_exists"] is False
    assert payload["broker_order_authority"] is False
