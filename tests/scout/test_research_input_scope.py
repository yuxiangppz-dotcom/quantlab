"""Bound the deep-pool input without dropping scoped provenance or quantitative facts."""

import json
from copy import deepcopy

import pytest
from test_daily_contract import inputs

from quantlab.scout.daily_budget import DailyBudgetError, DailyResearch
from quantlab.scout.daily_contract import (
    compact_fact_refs,
    compact_opportunity_catalog,
    research_packet,
    unpack_candidate_annotations,
    unpack_facts,
    unpack_opportunity_catalog,
)


def test_160_hypotheses_are_scoped_to_actual_deep_subjects_without_mutating_audit():
    raw = {
        "prediction_objective": "next_session",
        "candidates": [{"instrument_id": f"000{i:03d}.SZ", "metrics": {}} for i in range(24)],
        "market": {"session": "2026-09-30"},
        "timing": {},
        "opportunity_hypotheses": [
            {
                "hypothesis_id": f"h-{i}",
                "instrument_ids": [f"000{i:03d}.SZ"],
                "counterargument": "受益关系未经核实",
                "source_ids": [f"ev-{i}"],
                "research_priority_instrument_ids": [f"000{i:03d}.SZ", "999999.SZ"],
            }
            for i in range(160)
        ],
    }
    original = deepcopy(raw)
    scoped = research_packet(raw)
    assert len(scoped["opportunity_hypotheses"]) == 24
    assert raw == original
    actual = {c["instrument_id"] for c in raw["candidates"]}
    for row in scoped["opportunity_hypotheses"]:
        assert set(row["instrument_ids"]) <= actual
        assert set(row["research_priority_instrument_ids"]) <= actual
        assert row["counterargument"] == "受益关系未经核实"


def test_lossless_catalog_preserves_counterfacts_subjects_times_null_and_absence():
    rows = [
        {
            "hypothesis_id": "one",
            "instrument_ids": ["000001.SZ"],
            "counterargument": "尚无订单",
            "source_ids": ["ev-one"],
            "available_by_cutoff": True,
            "published_at": None,
            "count": -1,
            "weight": 0.5,
            "nested": {"statement": "尚无订单", "times": ["2026-09-30", None]},
        },
        {
            "hypothesis_id": "two",
            "instrument_ids": ["000002.SZ"],
            "counterargument": "尚无订单",
            "source_ids": ["ev-one"],
            "available_by_cutoff": False,
        },
    ]
    original = deepcopy(rows)
    packet = {"opportunity_hypotheses": rows}
    compact_opportunity_catalog(packet)
    assert unpack_opportunity_catalog(json.loads(json.dumps(packet))) == original
    assert "published_at" not in unpack_opportunity_catalog(packet)[1]


def test_annotations_codec_preserves_all_gaps_and_quantitative_values():
    packet = inputs()
    packet["prediction_objective"] = "next_session"
    for row in packet["candidates"]:
        row.update(
            cautions=["资金反证不可丢弃"],
            recall_routes=["event"],
            technical_gaps=["ma20"],
            source_gaps=["财联社缺口"],
            type_hints=["event_update"],
        )
    original = deepcopy(packet)
    compressed, _ = compact_fact_refs(packet)
    restored = unpack_candidate_annotations(compressed)
    assert restored == original["candidates"]
    old_values = sorted(
        (f["subject_id"], f["metric"], f["period"], f["value"], f["unit"])
        for f in unpack_facts(original).values()
    )
    new_values = sorted(
        (f["subject_id"], f["metric"], f["period"], f["value"], f["unit"])
        for f in unpack_facts(compressed).values()
    )
    assert new_values == old_values
    assert packet == original


def test_budget_preflight_collects_both_errors_before_any_transport(tmp_path):
    class NeverCalls:
        model = "offline"
        calls = []

        def ask(self, *args, **kwargs):
            pytest.fail("Oversized input must not reach transport")

    research = DailyResearch(NeverCalls(), journal=tmp_path)
    with pytest.raises(DailyBudgetError, match="daily_input_char_budget"):
        research.ask("中" * 180001, {"type": "object"})
    assert research.requests == []
    record = json.loads((tmp_path / "00-budget-check-01.json").read_text())
    assert record["errors"] == ["daily_input_char_budget", "daily_input_byte_budget"]
    assert record["before_request_number"] == 1
