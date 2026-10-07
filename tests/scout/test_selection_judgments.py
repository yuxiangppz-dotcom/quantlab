"""Program formatting with model-owned decisions, and bounded complete validation."""

from copy import deepcopy

import pytest
from test_nextday_contract import nextday_packet

from quantlab.scout.daily_budget import DailyResearch, DailyValidationError
from quantlab.scout.daily_contract import compact_fact_refs, unpack_facts, validate_output
from quantlab.scout.selection_judgments import (
    assemble,
    decode_model_packet,
    errors,
    merge_repairs,
    model_packet,
    prepare_packet,
    provenance,
    repair_plan,
    schema,
)


def packet_and_judgments():
    packet, _ = compact_fact_refs(nextday_packet())
    packet = prepare_packet(packet)
    facts = unpack_facts(packet)
    rows = []
    for candidate in reversed(packet["candidates"]):
        code = candidate["instrument_id"]
        peer = next(c["instrument_id"] for c in packet["candidates"] if c["instrument_id"] != code)
        pair = [
            next(
                ref
                for ref, f in facts.items()
                if f["subject_id"] == subject and f["metric"] == "close_to_ma20"
            )
            for subject in (code, peer)
        ]
        rows.append(
            {
                "instrument_id": code,
                "primary_type": "trend_continuation",
                "final_status": "focus",
                "evidence_reliability": "program_facts",
                "comparison_strength": "medium",
                "comparator_id": peer,
                "condition_id": code + ":price_structure_repair",
                "support_fact_ids": pair[:1],
                "counter_fact_ids": [],
                "reason": "套牢卖压消化可能改善价格位置，仍需目标日观察",
                "counterargument": "仅价格家族支持，经营催化未知，可能回撤",
                "difference": "本股与同行均线位置不同，需求持续性均未验证",
                "independent_basis": "纯量价假设，相关技术信号不是独立催化",
                "unknowns": "目标日实际需求及可参与条件未知",
                "mechanism": "若前期套牢卖压得到消化，目标日才可能维持均线上方",
            }
        )
    return packet, {"market_view": "合成非真实研究，仅用于验证交付协议", "comparisons": rows}


def test_complete_assembly_keeps_model_order_grades_and_original_wire():
    packet, value = packet_and_judgments()
    original, frozen = deepcopy(value), deepcopy(packet)
    assert errors(value, packet) == []
    output = assemble(value, packet)
    assert validate_output(output, packet) == []
    assert [r["instrument_id"] for r in output["comparisons"]] == [
        r["instrument_id"] for r in value["comparisons"]
    ]
    assert [r["rank"] for r in output["comparisons"]] == [1, 2]
    assert all(r["final_status"] == "focus" for r in output["comparisons"])
    assert all(
        r["participation_cancel_rule"]["reference_id"] is None for r in output["comparisons"]
    )
    assert value == original and packet == frozen
    assert provenance(value, packet, output)["program_selection_score"] is None


def test_model_metadata_encoding_is_lossless_and_unused_absolute_facts_not_citable():
    original, _ = compact_fact_refs(nextday_packet())
    raw_ma = next(ref for ref, f in unpack_facts(original).items() if f["metric"] == "ma60")
    original["candidates"][0]["events"] = [
        {
            "record_id": "event-synthetic",
            "title_only": True,
            "published_at": None,
            "relation": "direct_subject",
        }
    ]
    prepared = prepare_packet(original)
    assert decode_model_packet(model_packet(prepared)) == prepared
    assert raw_ma not in prepared["facts"] and raw_ma in original["facts"]
    original["investigation_opportunities"] = [
        {
            "instrument_id": original["candidates"][0]["instrument_id"],
            "analysis": {"scale_fact_ids": [raw_ma]},
        }
    ]
    retained = prepare_packet(original)
    assert retained["facts"][raw_ma] == original["facts"][raw_ma]
    assert unpack_facts(retained)[raw_ma]["calculation_version"]


@pytest.mark.parametrize(
    "change,code",
    [
        ("foreign_condition", "condition_option_not_shown_for_subject"),
        ("foreign_fact", "requires_shown_nonnull_own_fact"),
        ("bad_peer", "comparator_not_shown"),
        ("numeric_prose", "quantitative_prose_requires_fact_placeholder"),
        ("vague_mechanism", "focus_requires_specific_next_session_mechanism"),
        ("missing_condition", "focus_requires_structured_next_session_condition"),
        ("insufficient", "insufficient_cannot_rank_or_select"),
    ],
)
def test_no_favorable_coercion_of_invalid_judgment(change, code):
    packet, value = packet_and_judgments()
    row, other = value["comparisons"]
    if change == "foreign_condition":
        row["condition_id"] = other["condition_id"]
    elif change == "foreign_fact":
        row["support_fact_ids"] = other["support_fact_ids"]
    elif change == "bad_peer":
        row["comparator_id"] = "999999.SH"
    elif change == "numeric_prose":
        row["counterargument"] = "已上涨40%，注意回撤"
    elif change == "vague_mechanism":
        row["mechanism"] = "已有走势较强，继续观察"
    elif change == "missing_condition":
        row["condition_id"] = None
    else:
        row["evidence_reliability"] = "insufficient"
    issues = errors(value, packet)
    assert any(e["code"] == code for e in issues), issues


def test_missing_own_conditions_are_not_invented_and_halts_keep_research():
    packet, value = packet_and_judgments()
    row = value["comparisons"][0]
    candidate = next(c for c in packet["candidates"] if c["instrument_id"] == row["instrument_id"])
    candidate["trading_status"] = "hold_for_official_notice_review"
    assert errors(value, packet) == []
    assert assemble(value, packet)["comparisons"][0]["participation_status"] == "restricted"
    assert assemble(value, packet)["comparisons"][0]["final_status"] == "focus"
    for ref, f in unpack_facts(packet).items():
        if f["subject_id"] == row["instrument_id"] and f["metric"] in {
            "close_to_ma20",
            "relative_return_1d",
        }:
            packet["facts"][ref][3] = None
    packet = prepare_packet(packet)
    assert not candidate["condition_options"] or errors(value, packet)
    assert any(e["code"] == "condition_option_not_shown_for_subject" for e in errors(value, packet))


def test_directed_repair_keeps_unreported_rows_and_order_byte_for_byte():
    packet, value = packet_and_judgments()
    value["comparisons"][0]["counterargument"] = "涨幅40%"
    issues = errors(value, packet)
    plan = repair_plan(value, issues, schema(packet))
    changed = deepcopy(value["comparisons"][0])
    changed["counterargument"] = "价格已扩张，可能回撤"
    result, problems = merge_repairs(value, {"repairs": [changed]}, plan)
    assert not problems and not errors(result, packet)
    assert result["comparisons"][1] == value["comparisons"][1]
    assert value["comparisons"][0]["counterargument"] == "涨幅40%"
    assert merge_repairs(value, {"repairs": [value["comparisons"][1]]}, plan)[1]


@pytest.mark.parametrize("repair_valid", [True, False])
def test_daily_repair_is_one_call_with_new_wire_not_old_report_fields(tmp_path, repair_valid):
    packet, value = packet_and_judgments()
    value["comparisons"][0]["counterargument"] = "涨幅40%"

    class Model:
        model, max_output_tokens = "fake-non-real-model", 131072

        def __init__(self):
            self.calls = []

        def ask(self, prompt, contract):
            self.calls.append((prompt, contract))
            if len(self.calls) == 1:
                result = deepcopy(value)
            else:
                assert contract["$id"].endswith(":judgments-repairs")
                fixed = deepcopy(value["comparisons"][0])
                if repair_valid:
                    fixed["counterargument"] = "价格已扩张，可能回撤"
                result = {"repairs": [fixed]}
            return result, {"usage": {"total_tokens": 100}, "choices": []}

    client = DailyResearch(Model(), journal=tmp_path / "journal")
    if repair_valid:
        final, _ = client.ask(
            "[PROGRAM_ASSEMBLED_SELECTION_V1]固定事实",
            schema(packet),
            validator=lambda x: errors(x, packet),
        )
        assert errors(final, packet) == []
        assert final["comparisons"][1] == value["comparisons"][1]
    else:
        with pytest.raises(DailyValidationError):
            client.ask("固定事实", schema(packet), validator=lambda x: errors(x, packet))
    assert len(client.calls) == 2 and client.repairs == 1
    assert client.correction["mode"] == "single_directed_judgment_repair"
