from copy import deepcopy

import pytest
from test_daily_contract import inputs, output

from quantlab.scout.daily_contract import INSTRUCTION, selection_schema, validate_output
from quantlab.scout.daily_stages import investigation_prompt
from quantlab.scout.decision_contract import SCHEMA_VERSION, rule_text


def focus_case():
    packet, result = inputs(), output()
    ref = "fact:000001.SZ:market:relative_return_5d"
    packet["facts"][ref] = ["000001.SZ", "relative_return_5d", "5d", "0.01", "ratio", None]
    row = result["comparisons"][0]
    row.update(
        final_status="focus",
        invalidation_rule={"rule_id": "relative_5d_nonpositive_v1", "reference_id": ref},
    )
    row["analysis"]["h5_mechanism"] = "若行业内相对表现延续且负向日线不再扩张，则量价延续假设待确认"
    row["independent_basis"] = "纯量价假设、未验证，相关指标是同一证据家族"
    row["difference"] = (
        "本股[[fact:000001.SZ:market:return_1d]]；同行[[fact:000002.SZ:market:return_1d]]；"
        "比较差异仅作假设依据"
    )
    return packet, result


def test_explicit_price_hypothesis_can_focus_without_new_announcement():
    packet, result = focus_case()
    assert validate_output(result, packet) == []
    assert "每个完整交易日" in rule_text(result["comparisons"][0]["invalidation_rule"])
    assert "缺数为未知" in rule_text(result["comparisons"][0]["invalidation_rule"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_reliability", "insufficient"),
        ("comparison_strength", "insufficient"),
        ("comparison_strength", "weak"),
        ("difference", "明显更强更好"),
        ("invalidation_rule", None),
    ],
)
def test_insufficient_evidence_comparison_or_rule_cannot_hide_under_valid_type(field, value):
    packet, result = focus_case()
    result["comparisons"][0][field] = value
    errors = validate_output(result, packet)
    assert any(e["code"].startswith("focus_") for e in errors)


def test_old_empty_h5_focus_is_rejected_but_lower_observation_is_allowed():
    packet, result = inputs(), output()
    result["comparisons"][0]["final_status"] = "focus"
    errors = validate_output(result, packet)
    assert {
        "focus_requires_conditional_forward_hypothesis",
        "focus_requires_observable_invalidation",
    } <= {e["code"] for e in errors}
    result["comparisons"][0]["final_status"] = "watch"
    assert validate_output(result, packet) == []


@pytest.mark.parametrize(
    "text", ["只有本股有利好公告", "其他公司没有公告", "全池最高", "唯一新增订单"]
)
def test_legacy_sampled_and_unchecked_superlative_guards_migrated(text):
    packet, result = inputs(), output()
    result["comparisons"][0]["thesis"] = text
    assert validate_output(result, packet)


def test_rule_cannot_invent_threshold_or_reuse_peer_metric():
    packet, result = focus_case()
    row = result["comparisons"][0]
    row["invalidation_rule"]["threshold"] = -0.2
    assert any(e["code"] == "schema" for e in validate_output(result, packet))
    del row["invalidation_rule"]["threshold"]
    row["invalidation_rule"]["reference_id"] = "fact:000002.SZ:market:return_5d"
    assert any(
        e["code"] == "rule_metric_subject_or_type_mismatch" for e in validate_output(result, packet)
    )


def test_actual_daily_prompt_and_schema_share_versioned_decision_requirements():
    packet = inputs()
    for prompt in (INSTRUCTION, investigation_prompt(packet)):
        assert "[SCOUT_DECISION_V2]" in prompt
        assert "报告之后" in prompt and "纯量价" in prompt
    schema = selection_schema(packet["candidates"], packet)
    assert SCHEMA_VERSION in schema["$id"]
    assert {"semantic_claims", "invalidation_rule"} <= set(
        schema["properties"]["comparisons"]["items"]["required"]
    )


def test_candidate_reorder_preserves_subject_bound_semantics_and_original():
    packet, result = focus_case()
    before = deepcopy(result)
    packet["candidates"].reverse()
    assert validate_output(result, packet) == [] and result == before
