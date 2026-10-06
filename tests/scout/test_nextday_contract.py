"""Explicitly synthetic D1 contract cases, computed technical data and no provider calls."""

from copy import deepcopy
from datetime import date, datetime, timedelta

import pytest
from test_daily_contract import output

from quantlab.data.models import DailyBar
from quantlab.scout.daily_contract import (
    compact,
    render_output,
    research_packet,
    selection_instruction,
    selection_schema,
    unpack_facts,
    validate_output,
)
from quantlab.scout.daily_stages import investigation_contract, investigation_prompt
from quantlab.scout.models import SHANGHAI
from quantlab.scout.nextday_contract import SCHEMA_VERSION, VERSION
from quantlab.scout.pipeline import report_timing
from quantlab.scout.technical import technical_snapshot


def synthetic_history(code, n=120):
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(n)]
    bars = []
    slope = 0.02 + int(code[:6]) * 0.001
    for i, day in enumerate(days):
        previous = 10 + (i - 1) * slope
        close = 10 + i * slope
        bars.append(
            DailyBar(
                code,
                day,
                previous,
                close + 0.1,
                previous - 0.1,
                close,
                previous,
                10_000_000 + i,
                200_000_000 + i,
            )
        )
    factors = {(code, day): 1.0 for day in days}
    return bars, days, technical_snapshot(code, bars, factors, days, days[-1])


def nextday_packet():
    candidates = []
    for code in ("000001.SZ", "000002.SZ"):
        bars, days, snapshot = synthetic_history(code)
        candidates.append(
            {
                "instrument_id": code,
                "name": "合成非真实证券",
                "metrics": {
                    "return_1d": bars[-1].close / bars[-2].close - 1,
                    "return_5d": bars[-1].close / bars[-6].close - 1,
                    "amount_cny": bars[-1].amount,
                },
                "technical_snapshot": snapshot,
                "source_summary": {"industry": "合成行业"},
                "opportunity_record": {"events": [], "type_hints": ["trend_continuation"]},
            }
        )
    return research_packet(
        {
            "prediction_objective": "next_session",
            "timing": {"asof_session": days[-1].isoformat()},
            "market": {"session": days[-1].isoformat()},
            "coverage": [],
            "evidence": [],
            "candidates": candidates,
        }
    )


def adapt_row(code, rank, packet, *, grade="unselected"):
    row = deepcopy(output()["comparisons"][1])
    facts = {f["metric"]: ref for ref, f in unpack_facts(packet).items() if f["subject_id"] == code}
    refs = [facts["close_to_ma20"], facts["macd_hist2_to_close"]]
    row.update(
        {
            "instrument_id": code,
            "rank": rank,
            "final_status": grade,
            "comparator_id": None,
            "ranking_state": "rankable",
            "opportunity_ids": [],
            "participation_status": "observe_only",
            "next_session_condition": None,
            "technical_fact_ids": refs,
            "participation_cancel_rule": None,
            "technical_interpretation": "程序事实：[["
            + refs[0]
            + "]]；程序事实：[["
            + refs[1]
            + "]]",
            "price_reaction": "已有日线变化，未来反应未知",
            "thesis": "合成量价观察假设，未验证",
            "risk": "未知经营催化，不保证可成交",
            "independent_basis": "纯量价假设、未验证，相关技术指标属于同一价格家族",
            "evidence_ids": ["market:" + code],
            "fact_ids": [],
        }
    )
    row["analysis"]["next_session_thesis"] = row["analysis"].pop("h5_mechanism")
    row["analysis"]["next_session_thesis"] = "修复待观察"
    return row


def nextday_result(packet):
    return {
        "market_view": "合成观察，未验证效果",
        "comparisons": [
            adapt_row(c["instrument_id"], i, packet) for i, c in enumerate(packet["candidates"], 1)
        ],
    }


def focus_row(row, packet):
    code = row["instrument_id"]
    own = next(
        ref
        for ref, fact in unpack_facts(packet).items()
        if fact["subject_id"] == code and fact["metric"] == "close_to_ma20"
    )
    row.update(
        final_status="focus",
        comparator_id=next(
            c["instrument_id"] for c in packet["candidates"] if c["instrument_id"] != code
        ),
        next_session_condition={
            "kind": "price_structure_repair",
            "fact_ids": [own],
            "event_id": None,
        },
        invalidation_rule={"rule_id": "ma20_break_v1", "reference_id": own},
    )
    peer = next(
        ref
        for ref, fact in unpack_facts(packet).items()
        if fact["subject_id"] == row["comparator_id"] and fact["metric"] == "close_to_ma20"
    )
    row["difference"] = "本股[[" + own + "]]；同行[[" + peer + "]]；位置不同，均待观察"


def test_actual_stage_templates_and_schemas_migrate_objective_together():
    packet = nextday_packet()
    selection, study = selection_instruction(packet), investigation_prompt(packet)
    assert all("[SCOUT_NEXT_SESSION_V1]" in p and "H3/H5仅辅助" in p for p in (selection, study))
    assert "[SCOUT_DECISION_V2]" not in selection + study
    schemas = [
        selection_schema(packet["candidates"], packet),
        investigation_contract(packet["candidates"], packet),
    ]
    for schema in schemas:
        assert SCHEMA_VERSION in schema["$id"]
        assert "next_session_thesis" in compact(schema) and "h5_mechanism" not in compact(schema)
    assert packet["version"] == VERSION


def test_calculated_technical_facts_are_visible_cited_validated_and_rendered():
    packet, original = nextday_packet(), None
    result = nextday_result(packet)
    original = deepcopy(result)
    assert validate_output(result, packet) == []
    ref = result["comparisons"][0]["technical_fact_ids"][0]
    assert ref in unpack_facts(packet) and ref in selection_instruction(packet) + compact(
        packet
    )
    assert unpack_facts(packet)[ref]["calculation_version"]
    view = render_output(result, packet)
    assert "close_to_ma20" in view["comparisons"][0]["technical_interpretation"]
    assert "[[" not in view["comparisons"][0]["technical_interpretation"]
    assert result == original
    result["comparisons"][0]["technical_fact_ids"] = result["comparisons"][1]["technical_fact_ids"]
    errors = validate_output(result, packet)
    assert any(e["code"].startswith("technical_requires_own_calculated_fact") for e in errors)


def test_selected_strong_peer_allowed_and_short_mechanism_not_keyword_gate():
    packet = nextday_packet()
    result = nextday_result(packet)
    for row in result["comparisons"]:
        focus_row(row, packet)
        row["analysis"]["next_session_thesis"] = "修复"
    assert validate_output(result, packet) == []
    row = result["comparisons"][0]
    row["next_session_condition"] = None
    assert any(
        e["code"] == "focus_requires_structured_next_session_condition"
        for e in validate_output(result, packet)
    )


def test_unique_direct_event_can_have_no_comparable_without_invented_peer():
    packet, result = nextday_packet(), None
    result = nextday_result(packet)
    candidate = packet["candidates"][0]
    event_id = "event-synthetic-explicit-not-a-real-announcement"
    candidate.update(
        comparable_ids=[],
        events=[
            {
                "record_id": event_id,
                "novelty": "new_event",
                "title_only": False,
                "official_source": True,
                "relation": "direct_subject",
                "content_excerpt": "合成公司特有变化，仅用于契约测试，非真实公告",
            }
        ],
    )
    row = result["comparisons"][0]
    row.update(
        primary_type="event_update",
        type_labels=["event_update"],
        final_status="focus",
        comparator_id=None,
        comparison_strength="insufficient",
        difference="独特合成事件；无合适可比对象，不能比较经济规模",
        next_session_condition={
            "kind": "official_event_progress",
            "fact_ids": row["technical_fact_ids"][:1],
            "event_id": event_id,
        },
        invalidation_rule={"rule_id": "event_cancelled_d1_v1", "reference_id": event_id},
    )
    row["analysis"].update(novelty="new_event", exposure="direct", event_ids=[event_id])
    assert validate_output(result, packet) == []


def test_not_rankable_is_kept_but_cannot_be_selected():
    packet = nextday_packet()
    result = nextday_result(packet)
    row = result["comparisons"][1]
    row.update(
        primary_type="insufficient_evidence",
        type_labels=["insufficient_evidence"],
        ranking_state="not_rankable",
        rank=None,
        evidence_reliability="insufficient",
    )
    assert validate_output(result, packet) == []
    row["final_status"] = "focus"
    errors = validate_output(result, packet)
    assert any(e["code"] == "not_rankable_cannot_select" for e in errors)


@pytest.mark.parametrize("hour,minute", [(9, 0), (9, 35)])
def test_late_completion_keeps_original_target_instead_of_new_target(hour, minute):
    days = [date(2026, 10, 8), date(2026, 10, 9)]
    timing = report_timing(
        days,
        datetime(2026, 10, 8, hour, minute, tzinfo=SHANGHAI),
        date(2026, 9, 30),
        True,
        nextday=True,
        frozen_target="2026-10-08",
    )
    assert timing["target_session"] == "2026-10-08"
    assert timing["report_kind"] == "late_research" and not timing["primary_eligible"]
    assert timing["publication_deadline"] == "2026-10-08T09:00:00+08:00"
