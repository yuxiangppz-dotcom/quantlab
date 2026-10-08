import json
from datetime import datetime, timedelta

import pytest

from quantlab.scout import article_pipeline as pipeline
from quantlab.scout.article_review import MODE, research_hash
from quantlab.scout.models import SHANGHAI


def prepared():
    return {
        "research": {
            "signal_date": "2026-10-08",
            "target_date": "2026-10-09",
            "cutoff_at": (datetime.now(SHANGHAI) - timedelta(minutes=1)).isoformat(),
            "deep": [],
            "facts": [],
            "charts": {},
        },
        "environment": {
            "state": "unknown",
            "mode": "unknown",
            "priority_cap": 0,
            "total_cap": 3,
            "metrics": {},
        },
        "groups": [],
        "candidates": [],
        "allocation": {"deep": [], "context": []},
    }


def volume(tmp_path):
    root = tmp_path / "scout_article_test"
    root.mkdir()
    (root / ".scout-article").write_text("quantlab-scout-article-v1")
    return root


def test_prepare_and_freeze_never_call_external_model_and_repeat_is_original(tmp_path, monkeypatch):
    root = volume(tmp_path)
    monkeypatch.setattr(pipeline, "prepare", lambda *a, **k: prepared())
    monkeypatch.setattr(
        pipeline,
        "collect_article_sources",
        lambda *a, **k: pytest.fail("no source requested in offline replay"),
    )
    monkeypatch.setattr(pipeline, "track_article", lambda *a: root / "observation.json")
    folder = pipeline.run_article(root, source_pack={"records": {}, "coverage": []})
    research = json.loads((folder / "research.json").read_text())
    review = {
        "review_mode": MODE,
        "reviewer": "main_agent",
        "research_hash": research_hash(research),
        "reviews": [],
    }
    file = root / "input-review.json"
    file.write_text(json.dumps(review))
    assert not (folder / "report.json").exists()
    pipeline.finalize_article(root, folder, file)
    original = (folder / "report.json").read_bytes()
    report = json.loads(original)
    assert report["external_model_calls"] == 0
    assert not report["candidates"] and not report["production_switched"]
    assert pipeline.finalize_article(root, folder, file) == folder
    assert (folder / "report.json").read_bytes() == original


def test_tracking_failure_resumes_same_frozen_review_without_rewriting_report(
    tmp_path, monkeypatch
):
    root = volume(tmp_path)
    monkeypatch.setattr(pipeline, "prepare", lambda *a, **k: prepared())
    folder = pipeline.run_article(root, source_pack={"records": {}, "coverage": []})
    research = json.loads((folder / "research.json").read_text())
    file = root / "input-review.json"
    file.write_text(
        json.dumps(
            {
                "review_mode": MODE,
                "reviewer": "main_agent",
                "research_hash": research_hash(research),
                "reviews": [],
            }
        )
    )

    def fail(*args):
        raise ValueError("observation interrupted")

    monkeypatch.setattr(pipeline, "track_article", fail)
    with pytest.raises(ValueError, match="interrupted"):
        pipeline.finalize_article(root, folder, file)
    frozen = (folder / "frozen-report.json").read_bytes()
    assert not (folder / "completed.json").exists()
    monkeypatch.setattr(pipeline, "track_article", lambda *a: root / "observation.json")
    pipeline.finalize_article(root, folder, file)
    assert (folder / "frozen-report.json").read_bytes() == frozen
    assert (folder / "report.json").read_bytes() == frozen


def test_different_review_on_partial_delivery_is_rejected(tmp_path, monkeypatch):
    root = volume(tmp_path)
    monkeypatch.setattr(pipeline, "prepare", lambda *a, **k: prepared())
    folder = pipeline.run_article(root, source_pack={"records": {}, "coverage": []})
    research = json.loads((folder / "research.json").read_text())
    file = root / "input-review.json"
    review = {
        "review_mode": MODE,
        "reviewer": "main_agent",
        "research_hash": research_hash(research),
        "reviews": [],
    }
    file.write_text(json.dumps(review))
    pipeline.write_new(
        folder / "codex-review.json",
        review | {"calls": 0, "tokens": 0, "errors": [], "external_model_calls": 0},
    )
    file.write_text(json.dumps(review | {"market_explanation": "a changed review"}))
    with pytest.raises(ValueError, match="different Codex review"):
        pipeline.finalize_article(root, folder, file)
    assert not (folder / "report.json").exists()
