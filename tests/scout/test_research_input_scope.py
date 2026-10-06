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


def test_measured_capacity_retains_order_counterfacts_and_archives_exact_checks(
    tmp_path, monkeypatch
):
    from quantlab.scout.daily_stages import fit_research_input, record_input_capacity

    class NeverCalls:
        model = "offline"

        def ask(self, *args, **kwargs):
            pytest.fail("Preflight must not call a model")

    pool = [
        {"instrument_id": f"000{i:03d}.SZ", "research_budget": {"exploration": i % 4 == 3}}
        for i in range(24)
    ]
    original = deepcopy(pool)
    monkeypatch.setattr("quantlab.scout.daily_contract.selection_instruction", lambda p: "")
    monkeypatch.setattr(
        "quantlab.scout.daily_contract.selection_schema", lambda *a: {"type": "object"}
    )

    def packet(chosen):
        return {
            "candidates": deepcopy(chosen),
            "facts_and_counterevidence": {c["instrument_id"]: "保留反证" for c in chosen},
            # Explicit synthetic capacity pressure, not a new real market fact.
            "size_pressure": "x" * (9000 * len(chosen)),
        }

    client = DailyResearch(NeverCalls(), journal=tmp_path)
    chosen, built, capacity = fit_research_input(pool, packet, client, stage="before_discovery")
    assert 0 < len(chosen) < 24
    assert pool == original
    assert [c["instrument_id"] for c in chosen] == [c["instrument_id"] for c in pool[: len(chosen)]]
    assert capacity["exploration_count"] <= capacity["exploration_cap"]
    assert built["facts_and_counterevidence"] == {c["instrument_id"]: "保留反证" for c in chosen}
    assert client.requests == [] and client.spent == 0
    assert client.input_checks[0]["errors"] == [
        "daily_input_char_budget",
        "daily_input_byte_budget",
    ]
    assert client.input_checks[-1]["errors"] == []
    for index, check in enumerate(client.input_checks, 1):
        saved = json.loads((tmp_path / f"00-research-preflight-{index:02d}.json").read_text())
        assert len(saved["prompt"]) == check["prompt_chars"]
        assert saved["reserved_chars"] == check["reserved_chars"] == 36000
        assert saved["reserved_bytes"] == check["reserved_bytes"] == 85000
    diag = {"funnel": [{"instrument_id": c["instrument_id"], "deep": True} for c in pool]}
    record_input_capacity(diag, capacity)
    assert diag["model_admitted_count"] == len(chosen)
    assert sum(r["deep"] for r in diag["funnel"]) == len(chosen)
    assert all(
        r["reason"] == "input_budget_excluded_not_negative_evidence"
        for r in diag["budget_exclusions"]
    )


def test_measured_capacity_keeps_all_when_fit_and_never_loops_model_calls(tmp_path, monkeypatch):
    from quantlab.scout.daily_stages import fit_research_input

    class NeverCalls:
        model = "offline"

    pool = [{"instrument_id": f"000{i:03d}.SZ"} for i in range(24)]
    monkeypatch.setattr("quantlab.scout.daily_contract.selection_instruction", lambda p: "")
    monkeypatch.setattr(
        "quantlab.scout.daily_contract.selection_schema", lambda *a: {"type": "object"}
    )
    client = DailyResearch(NeverCalls(), journal=tmp_path / "fits")
    chosen, _, capacity = fit_research_input(
        pool, lambda c: {"candidates": c}, client, stage="before_investigation"
    )
    assert chosen == pool and capacity["exclusions"] == [] and len(client.input_checks) == 1
    failing = DailyResearch(NeverCalls(), journal=tmp_path / "fails")
    with pytest.raises(DailyBudgetError, match="daily_input_char_budget"):
        fit_research_input(
            pool,
            lambda c: {"candidates": c, "oversize": "x" * 180001},
            failing,
            stage="before_discovery",
        )
    assert len(failing.input_checks) == 24
    assert failing.requests == [] and failing.spent == 0
    assert len(list((tmp_path / "fails").glob("00-research-preflight-*.json"))) == 24
