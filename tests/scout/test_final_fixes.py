"""Final Scout regressions: typed facts, attention recall, complete limit records."""

import json
from datetime import datetime
from pathlib import Path
from runpy import run_path

import pytest

from quantlab.scout.ai import semantic_numeric_issue, validate_selection
from quantlab.scout.facts import extract_core_claims, program_facts
from quantlab.scout.models import SHANGHAI, Candidate, Evidence
from quantlab.scout.pipeline import build_pool, evidence_packet
from quantlab.scout.report import present_selection, render_report

NOW = datetime(2026, 10, 1, 15, tzinfo=SHANGHAI).isoformat()
OWN = "600001.SH"
PEER = "600002.SH"


def candidate(code=OWN, name="甲公司", one_day=0.0345):
    return {
        "instrument_id": code,
        "name": name,
        "metrics": {
            "return_1d": one_day,
            "return_5d": -0.1234,
            "amount_cny": 200_000_000,
            "one_price_session": False,
            "return_20d": 0.02,
            "amount_ratio_5d": 1.5,
            "breakout_20d": 0.01,
            "close_location": 0.8,
            "close": 10.0,
            "up_limit": 11.0,
        },
        "routes": ["量价异动"],
        "evidence_ids": [],
        "cautions": [],
        "source_summary": {
            "moneyflow": {"net_5d_wan_cny": -12345},
            "limit_history": [{"trade_date": "20260930", "limit_times": 2, "open_times": 1}],
        },
    }


def declared(row, own, candidates):
    declarations = []
    for field in ("thesis", "risk", "invalidation"):
        for claim in extract_core_claims(field, row[field], own, candidates):
            scope = (
                f"limit:limit_times:{claim.period}"
                if claim.metric == "limit_times"
                else f"moneyflow:{claim.metric}"
                if claim.metric.startswith("net_")
                else f"market:{claim.metric}"
            )
            declarations.append(
                {
                    "field": field,
                    "text": claim.text,
                    "fact_id": f"fact:{claim.subject_id}:{scope}",
                    "subject_id": claim.subject_id,
                    "metric": claim.metric,
                    "period": claim.period,
                    "value": str(claim.value),
                    "unit": claim.unit,
                    "direction": claim.direction,
                }
            )
    row["quant_claims"] = declarations
    row["fact_ids"] = [claim["fact_id"] for claim in declarations]
    return row


def selection(row):
    return {"market_view": "等待后续观察", "selected": [row]}


def test_typed_market_claims_bind_sign_period_unit_and_actual_prompt_source():
    own = candidate()
    facts = program_facts(own, "2026-09-30")
    assert {fact["metric"] for fact in facts} >= {
        "return_1d",
        "return_5d",
        "amount_cny",
        "net_5d_wan_cny",
        "limit_times",
    }
    assert all(
        fact["subject_id"] == OWN and fact["source_location"].startswith("candidate:")
        for fact in facts
    )
    wrong = (
        "近5日上涨12.34%",
        "近5日资金净流入1.23亿元",
        "昨日涨幅为6.78%",
        "当日成交额200000000亿元",
        "目前已经连续20板",
        "回购5次",
    )
    for phrase in wrong:
        assert semantic_numeric_issue("thesis", phrase, own, []) is not None, phrase
    right = (
        "近5日下跌12.34%",
        "近5日资金净流出约1.23亿元",
        "昨日涨幅为3.45%",
        "当日成交额2亿元",
        "连续2板",
        "观察5个交易日或H5",
    )
    for phrase in right:
        assert semantic_numeric_issue("thesis", phrase, own, []) is None, phrase
    corrupted = {**own, "program_facts": program_facts(own, "2026-09-30")}
    corrupted["program_facts"][0] = {**corrupted["program_facts"][0], "value": "999"}
    with pytest.raises(ValueError, match="differs from visible"):
        semantic_numeric_issue("thesis", "昨日涨幅为3.45%", corrupted, [])
    unknown = candidate()
    unknown["source_summary"]["moneyflow"] = {}
    assert semantic_numeric_issue("thesis", "近5日资金净流入0万元", unknown, []).code == (
        "core_fact_missing"
    )


def test_full_selection_and_presentation_keep_verified_claims_and_reject_wrong_subject():
    own = candidate()
    peer = candidate(PEER, "乙公司", -0.0678)
    good = declared(
        {
            "instrument_id": OWN,
            "status": "watch",
            "thesis": "乙公司昨日下跌6.78%，本股昨日上涨3.45%；当日成交额2亿元",
            "risk": "近5日资金净流出约1.23亿元，观察5个交易日",
            "invalidation": "若价格结构改变则重估",
            "evidence_ids": [f"market:{OWN}"],
        },
        own,
        [own, peer],
    )
    result = selection(good)
    assert validate_selection(result, [own, peer], []) == result
    shown = present_selection(result, [own, peer])["selected"][0]
    assert shown["quant_claims"] == good["quant_claims"]
    assert shown["reason_provenance"] == (
        "typed_core_numbers_checked_other_model_inference_unverified"
    )
    document = render_report(
        {
            "status": "demo",
            "market": {"session": "2026-09-30"},
            "finished_at": NOW,
            "ai_provider": None,
            "selection": {"market_view": "待观察", "selected": [shown]},
            "candidates": [own],
            "evidence": [],
            "coverage": [],
            "limitations": [],
        }
    )
    assert "4条核心量化断言" in document
    assert "其余机会判断仍为模型推断" in document
    bad = dict(good, thesis="本股昨日下跌6.78%，当日成交额2亿元")
    bad = declared(bad, own, [own, peer])
    with pytest.raises(ValueError, match="metric_direction_or_value"):
        validate_selection(selection(bad), [own, peer], [])
    bad = dict(good, quant_claims=[])
    with pytest.raises(ValueError, match="core_claim_not_declared"):
        validate_selection(selection(bad), [own, peer], [])
    bad = dict(good, quant_claims=[{**x, "subject_id": PEER} for x in good["quant_claims"]])
    with pytest.raises(ValueError, match="core_claim_structure_mismatch"):
        validate_selection(selection(bad), [own, peer], [])


def test_attention_hypotheses_enter_unique_budget_and_keep_weak_grade():
    codes = [f"60000{i}.SH" for i in range(1, 7)]
    universe = {
        code: Candidate(code, code, {"amount_ratio_5d": 1.0, "close_location": 0.5}, 0.1)
        for code in codes
    }
    hypotheses = [
        {
            "instrument_ids": [codes[0], codes[1], "999999.SH"],
            "route_type": "attention",
            "relation": "public_discussion",
            "evidence_ids": ["ev-discussion"],
        },
        {
            "instrument_ids": [codes[2]],
            "route_type": "attention",
            "relation": "unverified_user_clue",
            "evidence_ids": ["ev-user"],
        },
    ]
    pool = build_pool(universe, hypotheses, [], 3, attention_codes=[codes[1]])
    assert [x["instrument_id"] for x in pool] == [codes[1], codes[0], codes[2]]
    assert all(x["allocation_route"] == "attention" for x in pool)
    assert "ev-discussion" in universe[codes[0]].evidence_ids
    assert "ev-user" in universe[codes[2]].evidence_ids
    row = {
        "instrument_id": codes[0],
        "status": "focus",
        "thesis": "关注线索待核实",
        "risk": "未经独立证实",
        "invalidation": "若线索失效",
        "evidence_ids": [f"market:{codes[0]}"],
        "fact_ids": [],
    }
    with pytest.raises(ValueError, match="discussion-only"):
        validate_selection(selection(row), [universe[codes[0]].to_dict()], [])


def test_limit_history_keeps_recent_complete_rows_and_exposes_omissions():
    dates = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    items = []
    for index, day in enumerate(dates):
        items.append(
            Evidence(
                "tushare:limit_list_d",
                "涨停结构",
                json.dumps(
                    {
                        "trade_date": day.replace("-", ""),
                        "limit": "U",
                        "limit_times": index + 1,
                        "open_times": index,
                        "first_time": "100300",
                        "last_time": "100300",
                        "unit_note": "provider fields; unknown values remain null",
                        "long_optional": "x" * 100,
                    }
                ),
                None,
                None,
                NOW,
                kind="historical_limit_structure",
                instrument_ids=(OWN,),
                event_dates=(day,),
            )
        )
    first = evidence_packet(items, {OWN}, max_chars=5000, max_body_chars=650)
    assert first == evidence_packet(items, {OWN}, max_chars=5000, max_body_chars=650)
    summary = first[0]
    body = json.loads(summary["body"])
    assert len(summary["body"]) <= 650
    assert "2026-09-30" in summary["shown_event_dates"]
    assert list(summary["event_dates"]) == summary["shown_event_dates"]
    assert summary["source_event_dates"] == dates
    assert summary["omitted_event_dates"]
    assert all("source_id" in row for row in body["records"])
    assert (
        "long_optional" not in body["records"][0] or body["records"][0]["trade_date"] == "20260930"
    )
    tiny = evidence_packet(items, {OWN}, max_chars=5000, max_body_chars=60)[0]
    assert json.loads(tiny["body"])["omitted"] == "budget"
    assert tiny["shown_event_dates"] == []
    assert tiny["omitted_event_dates"] == dates


def test_revalidation_rejects_claimed_commit_that_is_not_current_head(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text("{}")
    revalidate = run_path(str(Path(__file__).resolve().parents[2] / "scripts/scout_revalidate.py"))[
        "revalidate"
    ]
    with pytest.raises(ValueError, match="Validator commit does not match"):
        revalidate(source, tmp_path / "canonical", tmp_path / "derived", "0" * 40)
