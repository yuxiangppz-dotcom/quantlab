from copy import deepcopy

import pytest

from quantlab.scout.daily_budget import DailyResearch, DailyValidationError
from quantlab.scout.daily_contract import (
    compact,
    compact_fact_refs,
    format_fact,
    remap_generated_refs,
    render_output,
    research_packet,
    selection_schema,
    unpack_facts,
    validate_output,
)
from quantlab.scout.daily_correction import apply_patch_output, patch_plan
from quantlab.scout.daily_stages import investigation_contract, investigation_errors


def inputs():
    candidates = []
    for code, value in (("000001.SZ", 0.02), ("000002.SZ", -0.01)):
        candidates.append(
            {
                "instrument_id": code,
                "name": code,
                "metrics": {"return_1d": value, "return_5d": value, "amount_cny": 200000000},
                "source_summary": {"industry": "示例行业"},
                "opportunity_record": {
                    "events": [],
                    "industry_context": {},
                    "pullback_qualified": False,
                    "type_hints": [],
                },
            }
        )
    packet = research_packet(
        {
            "timing": {"asof_session": "2026-09-30"},
            "market": {"session": "2026-09-30", "rejected_by_code": {"x": "drop"}},
            "candidates": candidates,
            "coverage": [
                {
                    "source": "test",
                    "status": "failed",
                    "count": 0,
                    "detail": "snapshot=/home/private",
                }
            ],
            "industry_background": {
                "industries": {"示例行业": {"positive_fraction_1d": 0.5, "eligible_count": 2}}
            },
            "evidence": [
                {
                    "evidence_id": "ev-one",
                    "instrument_ids": ["000001.SZ"],
                    "body": "反证：存在风险",
                    "snapshot_refs": ["/private"],
                }
            ],
            "hypotheses": [
                {
                    "summary": "待核实经营线索",
                    "counterargument": "缺少新增订单",
                    "instrument_ids": ["000001.SZ"],
                    "evidence_ids": ["ev-one"],
                },
                {
                    "summary": "待核实经营线索",
                    "counterargument": "缺少新增订单",
                    "instrument_ids": ["000002.SZ"],
                    "evidence_ids": ["ev-hidden"],
                },
            ],
        }
    )
    return packet


def output():
    rows = []
    for i, code in enumerate(("000001.SZ", "000002.SZ"), 1):
        ref = f"fact:{code}:market:return_1d"
        rows.append(
            {
                "instrument_id": code,
                "primary_type": "trend_continuation",
                "type_labels": ["trend_continuation"],
                "rank": i,
                "final_status": "focus" if i == 1 else "unselected",
                "comparator_id": "000002.SZ" if i == 1 else None,
                "comparison_strength": "medium",
                "evidence_reliability": "program_facts",
                "trade_conditions": {"known": "日线事实已归档", "unknown": "未知：封单和排队"},
                "thesis": "当时日线：[[" + ref + "]]",
                "risk": "没有新增经营证据",
                "invalidation": "假设需未来行情验证",
                "difference": "独立依据仍是量价假设",
                "independent_basis": "程序事实支持量价假设",
                "unknowns": "未知：可成交性",
                "analysis": {
                    "novelty": "price_only",
                    "event_ids": [],
                    "incremental_change": "没有核实新事件",
                    "economic_link": "未知",
                    "exposure": "price_only",
                    "importance": "需观察",
                    "scale_fact_ids": [],
                    "h5_mechanism": "待验证",
                    "next_observation_date": None,
                    "next_node_basis": "未取得日程",
                    "next_node_is_hypothesis": False,
                },
                "evidence_ids": ["market:" + code],
                "fact_ids": [ref],
            }
        )
    return {"market_view": "仅研究观察，不确定性仍高", "comparisons": rows}


def test_research_input_keeps_counterevidence_and_gaps_without_paths():
    packet = inputs()
    body = compact(packet)
    assert "反证：存在风险" in body
    assert "failed" in body
    assert "/home/" not in body and "snapshot_refs" not in body
    assert "rejected_by_code" not in body
    assert len(packet["research_notes"]) == 1
    assert packet["research_notes"][0]["counterargument"] == "缺少新增订单"
    assert packet["research_notes"][0]["omitted_source_ids"] == ["ev-hidden"]
    facts = unpack_facts(packet)
    assert facts["fact:industry:示例行业:positive_fraction_1d"]["period"] == "1d"
    assert facts["fact:industry:示例行业:eligible_count"]["unit"] == "count"


def test_explicit_peer_and_industry_references_render_with_subject_and_period():
    packet, result = inputs(), output()
    row = result["comparisons"][0]
    peer = "fact:000002.SZ:market:return_5d"
    industry = "fact:industry:示例行业:positive_fraction_1d"
    row["fact_ids"] += [peer, industry]
    row["difference"] = "同行[[" + peer + "]]；行业[[" + industry + "]]"
    before = deepcopy(result)
    assert validate_output(result, packet) == []
    view = render_output(result, packet)
    assert "000002.SZ · 5d" in view["comparisons"][0]["difference"]
    assert "-1.00%" in view["comparisons"][0]["difference"]
    assert "industry:示例行业 · 1d" in view["comparisons"][0]["difference"]
    assert result == before


def test_real_failure_shape_cannot_treat_evidence_id_as_fact_or_event():
    packet, result = inputs(), output()
    result["market_view"] = "行业[[fact:industry:示例行业:positive_fraction_1d]]"
    row = result["comparisons"][0]
    row["analysis"]["event_ids"] = ["ev-one"]
    row["thesis"] = "公告[[ev-one]]"
    errors = validate_output(result, packet)
    assert any(e["code"] == "market_view_qualitative_only" for e in errors)
    assert any(e["code"] == "event_not_shown_for_subject:ev-one" for e in errors)
    assert any(e["code"] == "unknown_or_wrong_subject_fact:ev-one" for e in errors)


def test_short_references_keep_every_fact_and_provider_text_unchanged():
    packet = inputs()
    packet["evidence"][0]["body"] += "原文[[fact:000001.SZ:market:return_1d]]"
    before = deepcopy(packet)
    short, aliases = compact_fact_refs(packet)
    assert len(short["facts"]) == len(packet["facts"])
    assert all(len(ref) == 17 for ref in short["facts"])
    assert all(short["facts"][aliases[key]] == value for key, value in packet["facts"].items())
    assert short["evidence"] == packet["evidence"] and packet == before
    assert compact_fact_refs(short)[0] == short
    assert compact_fact_refs(inputs())[1] == aliases


def test_program_collects_short_own_peer_and_industry_refs_without_duplicate_list():
    packet = inputs()
    result = output()
    row = result["comparisons"][0]
    peer = "fact:000002.SZ:market:return_5d"
    industry = "fact:industry:示例行业:positive_fraction_1d"
    row["risk"] = "同行反证：[[" + peer + "]]"
    row["difference"] = "行业背景：[[" + industry + "]]"
    for item in result["comparisons"]:
        item.pop("fact_ids")
    packet, aliases = compact_fact_refs(packet)
    result = remap_generated_refs(result, aliases)
    before = deepcopy(result)
    assert validate_output(result, packet) == []
    view = render_output(result, packet)
    assert set(view["comparisons"][0]["fact_ids"]) == {
        aliases[peer],
        aliases[industry],
        aliases["fact:000001.SZ:market:return_1d"],
    }
    assert "-1.00%" in view["comparisons"][0]["risk"]
    assert result == before
    result["comparisons"][1]["risk"] = "[[" + aliases["fact:000001.SZ:market:return_1d"] + "]]"
    assert any(
        e["code"].startswith("unknown_or_wrong_subject_fact:")
        for e in validate_output(result, packet)
    )


def test_real_unknown_lists_and_fixed_horizon_are_not_factual_assertions():
    packet, result = inputs(), output()
    row = result["comparisons"][0]
    row["unknowns"] = "未知：盘口、逐笔、封单、排队、次日可成交性"
    row["trade_conditions"]["unknown"] = row["unknowns"]
    row["analysis"]["h5_mechanism"] = "等待H5趋势验证"
    assert validate_output(result, packet) == []
    row["unknowns"] = "无封单、承接与可成交性数据"
    assert validate_output(result, packet) == []
    row["unknowns"] = "未知：封单很强，可以成交"
    assert any(
        e["code"] == "unsupported_microstructure_assertion" for e in validate_output(result, packet)
    )


def test_all_rows_errors_collected_without_favorable_substitution():
    packet, result = inputs(), output()
    result["comparisons"][0]["thesis"] = "上涨20%"
    result["comparisons"][0]["fact_ids"].append("fact:foreign:wrong")
    result["comparisons"][1]["risk"] = "封单强，可以成交"
    result["comparisons"][1]["quant_claims"] = []  # Extra legacy field also diagnosed.
    errors = validate_output(result, packet)
    assert any(e["code"] == "schema" for e in errors)
    assert any(e["code"] == "quantitative_prose_requires_fact_placeholder" for e in errors)
    assert any(e["code"].startswith("unknown_or_wrong_subject_fact") for e in errors)
    assert any(e["path"] == ["000002.SZ", "risk"] for e in errors)


def test_subject_and_period_cannot_be_replaced_by_model():
    packet, result = inputs(), output()
    result["comparisons"][1]["thesis"] = "[[fact:000001.SZ:market:return_1d]]"
    result["comparisons"][1]["fact_ids"] = ["fact:000001.SZ:market:return_1d"]
    assert any(
        e["code"].startswith("unknown_or_wrong_subject_fact")
        for e in validate_output(result, packet)
    )
    assert selection_schema(packet["candidates"])["properties"]["comparisons"]["maxItems"] == 2


def test_schema_failure_does_not_hide_other_safe_row_errors():
    packet, result = inputs(), output()
    row = result["comparisons"][0]
    row["primary_type"] = "insufficient_evidence"
    row["type_labels"] = ["insufficient_evidence"]
    row["thesis"] = "净流出" + "不确定" * 50
    row["risk"] = "封单强，可以成交"
    errors = validate_output(result, packet)
    codes = {e["code"] for e in errors}
    assert "schema" in codes and "insufficient_cannot_rank_or_select" in codes
    assert "quantitative_prose_requires_fact_placeholder" in codes
    assert "unsupported_microstructure_assertion" in codes


def test_unknown_financial_units_are_not_formatted_as_money():
    assert "单位待核实" in format_fact(
        {
            "value": "100",
            "unit": "provider_unit_unknown",
            "subject_id": "stock",
            "period": "20260630",
            "metric": "profit_dedt",
        }
    )


def test_directed_patch_preserves_valid_decisions_and_counterevidence(tmp_path):
    packet, previous = inputs(), output()
    previous["comparisons"][0]["risk"] = "资金净流出，需留意资金反证"
    before = deepcopy(previous)
    patch = {
        "patches": [
            {
                "path": "/comparisons/0/risk",
                "op": "replace",
                "value": "资金反证仍待核验，不忽略风险",
            }
        ]
    }

    class Replay:
        model = "offline-fixture"

        def __init__(self):
            self.calls = []

        def ask(self, prompt, schema):
            self.calls.append(prompt)
            value = previous if len(self.calls) == 1 else patch
            return value, {"usage": {"total_tokens": 0}}

    client = DailyResearch(Replay(), journal=tmp_path)
    result, _ = client.ask(
        "fixed real-input shape",
        selection_schema(packet["candidates"]),
        validator=lambda value: validate_output(value, packet),
    )
    expected = deepcopy(before)
    expected["comparisons"][0]["risk"] = patch["patches"][0]["value"]
    assert result == expected and previous == before
    assert client.repairs == 1 and len(client.calls) == 2
    assert client.summary()["correction"]["mode"] == "single_directed_model_patch"
    assert (tmp_path / "02-assembled-output.json").is_file()


def test_patch_cannot_change_unreported_selection_or_duplicate_path():
    packet, previous = inputs(), output()
    previous["comparisons"][0]["risk"] = "资金净流出"
    before = deepcopy(previous)
    plan = patch_plan(
        previous, validate_output(previous, packet), selection_schema(packet["candidates"])
    )
    assert plan is not None
    malicious = {
        "patches": [{"path": "/comparisons/0/final_status", "op": "replace", "value": "unselected"}]
    }
    result, errors = apply_patch_output(previous, malicious, plan)
    assert result is None and errors and previous == before
    duplicate = {
        "patches": [
            {"path": "/comparisons/0/risk", "op": "replace", "value": "反证待核验"},
            {"path": "/comparisons/0/risk", "op": "replace", "value": "反证仍未知"},
        ]
    }
    result, errors = apply_patch_output(previous, duplicate, plan)
    assert result is None and errors[0]["code"] == "duplicate_patch_path"


def test_patch_cannot_delete_referenced_counterfact_to_pass_validation():
    packet, previous = inputs(), output()
    ref = previous["comparisons"][0]["fact_ids"][0]
    previous["comparisons"][0]["risk"] = "净流出风险；[[" + ref + "]]"
    plan = patch_plan(
        previous, validate_output(previous, packet), selection_schema(packet["candidates"])
    )
    assert plan is not None
    removed = {"patches": [{"path": "/comparisons/0/risk", "op": "replace", "value": "反证未知"}]}
    result, errors = apply_patch_output(previous, removed, plan)
    assert result is None and errors
    removed = {"patches": [{"path": "/comparisons/0/fact_ids", "op": "replace", "value": []}]}
    result, errors = apply_patch_output(previous, removed, plan)
    assert result is None and errors


def test_invalid_patch_ends_the_only_repair_without_full_regeneration():
    packet, previous = inputs(), output()
    previous["comparisons"][0]["risk"] = "资金净流出"

    class Invalid:
        model = "offline-fixture"

        def __init__(self):
            self.calls = []

        def ask(self, prompt, schema):
            self.calls.append(prompt)
            return (previous if len(self.calls) == 1 else {"patches": []}), {
                "usage": {"total_tokens": 0}
            }

    client = DailyResearch(Invalid())
    with pytest.raises(DailyValidationError):
        client.ask(
            "fixed",
            selection_schema(packet["candidates"]),
            validator=lambda v: validate_output(v, packet),
        )
    assert len(client.calls) == 2 and client.repairs == 1


def test_investigation_patch_collects_enum_and_subject_errors_without_rewriting():
    packet, _ = compact_fact_refs(inputs())
    schema = investigation_contract(packet["candidates"], packet)
    previous = {
        "hypotheses": [],
        "opportunities": [
            {"instrument_id": r["instrument_id"], "analysis": deepcopy(r["analysis"])}
            for r in output()["comparisons"]
        ],
    }
    previous["opportunities"][0]["analysis"]["event_ids"] = ["event-wrong-subject"]
    previous["opportunities"][0]["analysis"]["scale_fact_ids"] = [
        next(k for k, v in packet["facts"].items() if v[0] == "000002.SZ")
    ]
    before = deepcopy(previous)
    errors = investigation_errors(previous, packet, schema)
    assert any(e["code"].startswith("event_not_shown") for e in errors)
    assert any(e["code"].startswith("scale_requires_own_fact") for e in errors)
    plan = patch_plan(previous, errors, schema)
    assert plan and set(plan["targets"]) == {
        "/opportunities/0/analysis/event_ids",
        "/opportunities/0/analysis/scale_fact_ids",
    }
    patch = {"patches": [{"path": p, "op": "replace", "value": []} for p in plan["targets"]]}
    result, errors = apply_patch_output(previous, patch, plan)
    assert errors == [] and investigation_errors(result, packet, schema) == []
    expected = deepcopy(before)
    expected["opportunities"][0]["analysis"]["event_ids"] = []
    expected["opportunities"][0]["analysis"]["scale_fact_ids"] = []
    assert result == expected and previous == before


def test_only_own_and_declared_peer_short_ticker_are_not_quantitative_assertions():
    packet, result = inputs(), output()
    row = result["comparisons"][0]
    row["comparator_id"] = "000002.SZ"
    row["difference"] = "000001与000002的日线结构不同，效果待观察"
    assert validate_output(result, packet) == []
    for value in ["上涨000002%", "第三股000003", "交易量12345678"]:
        row["difference"] = value
        assert any(
            e["code"] == "quantitative_prose_requires_fact_placeholder"
            for e in validate_output(result, packet)
        )


def test_invalid_subject_reference_patch_keeps_valid_counterfacts_and_decisions():
    packet, previous = inputs(), output()
    row = previous["comparisons"][0]
    own = row["fact_ids"][0]
    wrong = "fact:000003.SZ:market:return_1d"
    packet["facts"][wrong] = ["000003.SZ", *packet["facts"][own][1:]]
    row["risk"] = "本股反证[[" + own + "]]；错误主体[[" + wrong + "]]"
    row["analysis"]["scale_fact_ids"] = [wrong]
    before = deepcopy(previous)
    errors = validate_output(previous, packet)
    plan = patch_plan(previous, errors, selection_schema(packet["candidates"], packet))
    assert plan is not None
    patch = {
        "patches": [
            {
                "path": "/comparisons/0/risk",
                "op": "replace",
                "value": "本股反证[[" + own + "]]；其他主体无法支持本股",
            },
            {"path": "/comparisons/0/fact_ids", "op": "replace", "value": [own]},
            {"path": "/comparisons/0/analysis/scale_fact_ids", "op": "replace", "value": []},
        ]
    }
    result, errors = apply_patch_output(previous, patch, plan)
    assert errors == [] and validate_output(result, packet) == []
    assert previous == before and result["comparisons"][0]["rank"] == row["rank"]
    patch["patches"][0]["value"] = "丢弃全部反证"
    result, errors = apply_patch_output(previous, patch, plan)
    assert result is None and errors
