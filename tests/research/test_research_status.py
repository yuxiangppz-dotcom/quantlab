from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import quantlab.__main__ as main_module
from quantlab.data.models import DataValidationError
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


def test_existing_malformed_evidence_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "experiments" / "run" / "summary.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not-json", encoding="utf-8")

    with pytest.raises(DataValidationError, match="invalid JSON evidence artifact"):
        build_research_status(
            registry_path=REGISTRY,
            forward_config_path=FORWARD,
            experiment_root=tmp_path / "experiments",
            shadow_root=tmp_path / "missing-shadow",
        )


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
