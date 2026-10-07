import json
from hashlib import sha256

import pytest
from test_daily_contract import inputs, output

from quantlab.scout.models import fingerprint
from quantlab.scout.selection_review import review_saved


def test_saved_review_never_regrades_or_overwrites_original_and_checks_budget(tmp_path):
    packet, raw = inputs(), output()
    run = tmp_path / "saved"
    run.mkdir()
    report = {
        "run_id": "saved",
        "status": "incomplete",
        "finished_at": "2026-10-05T18:00:00+08:00",
        "market": packet["market"],
        "timing": {"target_session": "2026-10-08"},
        "selection": {"selected": []},
        "selection_raw": raw,
        "selection_input_packet": packet,
        "ai_model": "offline-fixture",
        "config_sha256": "fixture",
        "prompt_version": "older-contract",
        "candidates": [
            {
                "instrument_id": c["instrument_id"],
                "name": c["instrument_id"],
                "score": 0.2,
                "metrics": {"return_5d": 0.1},
            }
            for c in packet["candidates"]
        ],
    }
    (run / "report.json").write_text(json.dumps(report))
    (run / "manifest.json").write_text(json.dumps({"report_sha256": fingerprint(report)}))
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    review = tmp_path / "review"
    result = review_saved(run, review)
    audit = json.loads((review / "review.json").read_text())
    assert result["model_calls"] == 0 and result["original_files_unchanged"]
    assert all(r["new_grade"] is None for r in audit["rows"])
    assert "离线展示副本" in (review / "display-replay.md").read_text()
    assert before == {p.name: sha256(p.read_bytes()).hexdigest() for p in run.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        review_saved(run, review)


def test_runtime_receipt_identifies_actual_engine_not_development_prompt(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from quantlab.scout.free_cloud import sealed_identity

    settings = {"release_root": str(tmp_path / "app")}
    engine = {"release_root": str(tmp_path / "engine")}
    monkeypatch.setattr("quantlab.scout.daily_runtime.prediction_settings", lambda s: engine)
    monkeypatch.setattr(
        "quantlab.scout.daily_runtime.verify_release",
        lambda s: {"commit": "app-sha" if s is settings else "old-engine-sha"},
    )
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({"prompt_version": "actual-old-contract"}))

    monkeypatch.setattr("subprocess.run", probe)
    result = sealed_identity(settings)
    assert result["engine_commit"] == "old-engine-sha"
    assert result["prompt_version"] == "actual-old-contract"
    assert calls[0][0] == str(tmp_path / "engine/.venv/bin/python")
