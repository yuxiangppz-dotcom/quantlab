import json

import pytest
from streamlit.testing.v1 import AppTest

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research import s4_observation_progress as m
from quantlab.research.input_audit import _sha


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "BASE", "synthetic")
    sources, bodies = {}, {}
    monkeypatch.setattr(m, "SOURCES", sources)
    out = tmp_path / m.BASE
    out.mkdir()
    artifact = out / "bound.bin"
    artifact.write_bytes(b"synthetic observation, not real data")

    def write(key, body):
        bodies[key] = body
        result = {**body, "fingerprint": canonical_payload_fingerprint(body)}
        name = key + ".json"
        (out / name).write_text(json.dumps(result), encoding="utf-8")
        sources[key] = (name, result["fingerprint"])
        return result["fingerprint"]

    r = write(
        "observation",
        {
            "broker_order": False,
            "performance_evidence": False,
            "execution_authority": False,
            "candidate_promoted": False,
            "future_diagnostic_pending": True,
            "economic_paths": 0,
            "model_fits": 0,
            "rows": 256,
            "known_S4_A": 245,
            "known_reference20": 244,
            "timing": {
                "created_at": "2026-09-13T01:27:31+00:00",
                "source_available_at": "2026-09-13T01:27:30+00:00",
                "publication_cutoff": "2026-09-14T09:30:00+08:00",
                "price_as_of": "2026-09-11",
                "old_same_day_forward_shadow_eligible": False,
                "future_window_not_started": True,
            },
            "label_dates": [
                "2026-09-14",
                "2026-09-15",
                "2026-09-16",
                "2026-09-17",
                "2026-09-18",
                "2026-09-21",
            ],
            "files": {"bound.bin": {"sha256": _sha(artifact), "bytes": artifact.stat().st_size}},
        },
    )
    write(
        "proof",
        {
            "report_fingerprint": r,
            "checked_codes": 256,
            "checked_grid_rows": 5376,
            "raw_unit_mapping_reconciled": True,
            "all_scores_and_masks_reconciled": True,
            "performance_evidence": False,
            "execution_authority": False,
        },
    )
    return tmp_path, bodies, write


def test_missing_is_unknown_not_zero_or_success(tmp_path):
    assert m.read_observation(tmp_path) is None


def test_summary_uses_actual_china_time_and_keeps_pending_state(saved):
    root, _, _ = saved
    r = m.read_observation(root)
    assert r["created_at_china"] == "2026-09-13 09:27:31"
    assert (r["cohort"], r["known_S4_A"], r["known_reference20"]) == (256, 245, 244)
    assert r["state"] == "已登记，未来结果尚未评价"
    assert r["performance_evidence"] is r["execution_authority"] is False
    assert r["reference_dates"][-1] == "2026-09-21"


def test_partial_bundle_does_not_display_a_verified_observation(saved):
    root, _, _ = saved
    (root / m.BASE / "proof.json").unlink()
    with pytest.raises(DataValidationError, match="不齐全"):
        m.read_observation(root)


@pytest.mark.parametrize("defect", ["byte_corruption", "new_revision", "output_corruption"])
def test_unreviewed_or_corrupted_sources_are_rejected(saved, defect):
    root, bodies, _ = saved
    path = root / m.BASE / "observation.json"
    if defect == "byte_corruption":
        path.write_text(path.read_text().replace('"rows": 256', '"rows": 257'))
    elif defect == "new_revision":
        body = {**bodies["observation"], "new": True}
        path.write_text(json.dumps({**body, "fingerprint": canonical_payload_fingerprint(body)}))
    else:
        (root / m.BASE / "bound.bin").write_bytes(b"changed")
    with pytest.raises(DataValidationError):
        m.read_observation(root)


@pytest.mark.parametrize(
    "defect",
    [
        "proof_link",
        "proof_check",
        "authority",
        "profit",
        "evaluated",
        "sample",
        "count",
        "naive",
        "late",
        "backdate",
        "shadow",
        "window",
        "output_bytes",
    ],
)
def test_semantic_claims_cannot_upgrade_the_observation(saved, defect):
    root, bodies, write = saved
    r, p = bodies["observation"], bodies["proof"]
    if defect == "proof_check":
        p["all_scores_and_masks_reconciled"] = False
    elif defect == "authority":
        r["execution_authority"] = True
    elif defect == "profit":
        p["performance_evidence"] = True
    elif defect == "evaluated":
        r["future_diagnostic_pending"] = False
    elif defect == "sample":
        r["rows"] = 255
    elif defect == "count":
        r["known_S4_A"] = 257
    elif defect == "naive":
        r["timing"]["created_at"] = "2026-09-13T01:27:31"
    elif defect == "late":
        r["timing"]["created_at"] = "2026-09-14T01:30:00+00:00"
    elif defect == "backdate":
        r["timing"]["created_at"] = "2026-09-11T08:00:00+00:00"
    elif defect == "shadow":
        r["timing"]["old_same_day_forward_shadow_eligible"] = True
    elif defect == "window":
        r["label_dates"][-1] = "2026-09-18"
    elif defect == "output_bytes":
        r["files"]["bound.bin"]["bytes"] += 1
    p["report_fingerprint"] = write("observation", r)
    if defect == "proof_link":
        p["report_fingerprint"] = "other"
    write("proof", p)
    with pytest.raises(DataValidationError):
        m.read_observation(root)


def test_visible_observation_counts_are_not_stock_recommendations(saved, monkeypatch):
    from quantlab.ui import research_program

    root, _, _ = saved
    monkeypatch.setattr(research_program, "PROJECT_ROOT", root)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_prospective_observation\n"
        "render_prospective_observation()"
    ).run(timeout=20)
    assert not app.exception and not app.error
    assert app.dataframe[0].value["数量"].tolist() == [256, 245, 244]
    text = " ".join(x.value for x in [*app.markdown, *app.caption])
    assert "未来结果尚未评价" in text and "不是推荐持仓" in text
    assert "2026-09-14" in text and "2026-09-21" in text


def test_ui_shows_unknown_for_absent_bundle_without_zero_counts(tmp_path, monkeypatch):
    from quantlab.ui import research_program

    monkeypatch.setattr(research_program, "PROJECT_ROOT", tmp_path)
    app = AppTest.from_string(
        "from quantlab.ui.research_program import render_prospective_observation\n"
        "render_prospective_observation()"
    ).run(timeout=20)
    assert not app.exception and not app.error
    assert len(app.dataframe) == 0 and len(app.info) == 1
