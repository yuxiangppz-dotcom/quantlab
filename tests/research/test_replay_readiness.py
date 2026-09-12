import json

import pytest

from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.research import replay_readiness as module


@pytest.fixture
def saved(tmp_path, monkeypatch):
    # Small synthetic summaries; never installed as real source evidence.
    sources = {}
    monkeypatch.setattr(module, "SOURCES", sources)
    bodies = {}

    def write(key, body):
        bodies[key] = body
        name = key + ".json"
        result = {**body, "fingerprint": canonical_payload_fingerprint(body)}
        (tmp_path / name).write_text(json.dumps(result), encoding="utf-8")
        sources[key] = (name, result["fingerprint"])
        return result["fingerprint"]

    flags = dict(execution_authority=False, cashflow_eligible=False, historical_pit_certified=False)
    terms = write(
        "terms",
        {
            **flags,
            "implementation": {
                "occurrences": 3,
                "cash_fields_complete": 2,
                "quantity_fields_complete": 3,
                "cash_state": {"known_positive": 2, "unknown": 1},
            },
        },
    )
    write("terms_proof", {"report_fingerprint": terms, "all_checks_passed": True})
    annotations = write("annotations", {**flags, "cash_before_tax": None})
    write(
        "recipients",
        {
            **flags,
            "annotations_fingerprint": annotations,
            "results": [{"provider_cash_before_tax": None}],
            "cash_annotations": {"unknown": 1},
            "share_annotations": {"unknown": 1},
            "ordinary_recipient_evidence_complete": 0,
            "ordinary_recipient_evidence_blocked": 1,
        },
    )
    rules = write(
        "rules",
        {
            "execution_authority": False,
            "performance_evidence": False,
            "comparison": {"covered_2022": 4, "unchanged_rows": 8},
            "summary": [],
        },
    )
    write("rules_proof", {"report_fingerprint": rules, "all_checks_passed": True})
    return tmp_path, bodies, write


def test_unavailable_sources_do_not_become_zero_progress(tmp_path):
    assert module.read_preparation(tmp_path) is None


def test_partial_source_bundle_is_explicitly_blocked(saved):
    root, _, _ = saved
    (root / "rules_proof.json").unlink()
    with pytest.raises(DataValidationError, match="不完整"):
        module.read_preparation(root)


def test_summary_keeps_distinct_denominators_unknowns_and_no_authority(saved):
    root, _, _ = saved
    result = module.read_preparation(root)
    assert result["terms"]["occurrences"] == 3
    assert result["recipient_occurrences"] == 1
    assert result["cash_notice_annotations"] == {"unknown": 1}
    assert result["ordinary_recipient_evidence_complete"] == 0
    assert not result["performance_evidence"] and not result["cashflow_eligible"]
    assert not result["historical_pit_certified"] and not result["execution_authority"]
    assert len(result["source_evidence"]) == 6
    assert all(s["recorded_at"] is None for s in result["source_evidence"])


def test_byte_corruption_is_not_rendered_as_progress(saved):
    root, _, _ = saved
    path = root / "terms.json"
    path.write_text(path.read_text().replace('"occurrences": 3', '"occurrences": 99'))
    with pytest.raises(DataValidationError):
        module.read_preparation(root)


def test_newly_sealed_unreviewed_revision_is_rejected(saved):
    root, bodies, _ = saved
    body = {**bodies["terms"], "new_unreviewed_field": True}
    (root / "terms.json").write_text(
        json.dumps(
            {
                **body,
                "fingerprint": canonical_payload_fingerprint(body),
            }
        )
    )
    with pytest.raises(DataValidationError, match="版本不同"):
        module.read_preparation(root)


@pytest.mark.parametrize(
    "key,updates",
    [
        ("terms_proof", {"report_fingerprint": "other"}),
        ("rules_proof", {"all_checks_passed": False}),
        ("recipients", {"annotations_fingerprint": "other"}),
        ("terms", {"execution_authority": True}),
        ("recipients", {"cashflow_eligible": True}),
        ("annotations", {"historical_pit_certified": True}),
        ("rules", {"performance_evidence": True}),
    ],
)
def test_bad_proof_or_authority_cannot_appear_as_completed_work(saved, key, updates):
    root, bodies, write = saved
    new = write(key, {**bodies[key], **updates})
    if key in ("terms", "rules"):
        write(key + "_proof", {**bodies[key + "_proof"], "report_fingerprint": new})
    elif key == "annotations":
        write("recipients", {**bodies["recipients"], "annotations_fingerprint": new})
    with pytest.raises(DataValidationError):
        module.read_preparation(root)
