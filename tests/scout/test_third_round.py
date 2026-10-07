"""Third-round regressions for fact boundaries and timed evidence."""

import json
from datetime import date, datetime
from pathlib import Path
from runpy import run_path

import pytest

from quantlab.scout.ai import (
    asserts_unsupported_microstructure,
    semantic_numeric_issue,
    unsupported_numeric_claims,
    validate_selection,
)
from quantlab.scout.models import SHANGHAI, Candidate, Evidence, fingerprint
from quantlab.scout.pipeline import build_pool, evidence_packet
from quantlab.scout.tushare_upgrade import (
    TusharePack,
    collect_deep_pack,
    collect_market_pack,
    report_periods,
)

revalidate = run_path(str(Path(__file__).resolve().parents[2] / "scripts/scout_revalidate.py"))[
    "revalidate"
]

CODE = "600001.SH"
NOW = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)


def test_signed_return_amount_unit_and_order_are_contextual():
    candidate = {"metrics": {"return_1d": -0.0345, "amount_cny": 200_000_000}}
    wrong_direction = semantic_numeric_issue("thesis", "昨日上涨3.45%。", candidate, [])
    assert wrong_direction.code == "metric_direction_or_value"
    wrong_unit = semantic_numeric_issue("thesis", "昨日成交额200000000亿元。", candidate, [])
    assert wrong_unit.code == "amount_unit_or_value"
    assert semantic_numeric_issue("thesis", "昨日成交额2亿元。", candidate, []) is None
    missing_ref = semantic_numeric_issue(
        "thesis", "昨日成交额2亿元。", {**candidate, "instrument_id": CODE}, [], []
    )
    assert missing_ref.code == "missing_fact_reference"
    assert (
        semantic_numeric_issue(
            "thesis",
            "昨日成交额2亿元。",
            {**candidate, "instrument_id": CODE},
            [],
            [f"fact:{CODE}:market:amount_cny"],
        )
        is None
    )
    assert unsupported_numeric_claims("昨日成交额2亿元。", candidate, []) == []
    order = semantic_numeric_issue("thesis", "公司新获20亿元订单。", candidate, [])
    assert order.code == "unsupported_company_order"
    assert asserts_unsupported_microstructure("封单很强但次日成交未知。")
    assert not asserts_unsupported_microstructure("缺少封单数据，次日成交未知。")


def test_stock_codes_dates_and_profit_ranges_do_not_corrupt_numeric_tokens():
    source = Evidence(
        "tushare:forecast_vip",
        "业绩预告",
        '{"net_profit_min":8500,"net_profit_max":9000}',
        None,
        None,
        NOW.isoformat(),
        instrument_ids=(CODE,),
    )
    candidate = {
        "instrument_id": CODE,
        "source_summary": {"named_comparators": [{"instrument_id": "000678.SZ"}]},
    }
    text = "600001与000678对比，9/28-9/30期间预告净利8500-9000万元"
    assert unsupported_numeric_claims(text, candidate, [source]) == []
    assert unsupported_numeric_claims("公司新获20亿元订单", candidate, [source])
    limit = Evidence(
        "tushare:limit_list_d",
        "涨停记录",
        '{"fd_amount":120629190,"unit_note":"provider fields; unknown values remain null"}',
        None,
        None,
        NOW.isoformat(),
        instrument_ids=(CODE,),
    )
    claim = semantic_numeric_issue("thesis", "fd_amount约1.21亿元", candidate, [limit])
    assert claim.code == "unknown_provider_unit"


def test_sample_scope_peer_binding_and_weak_theme_grade():
    own = {
        "instrument_id": CODE,
        "name": "测试甲",
        "routes": ["信息关联:third_party_theme_membership_unverified"],
        "metrics": {},
        "evidence_ids": [],
    }
    peer = {
        "instrument_id": "600002.SH",
        "name": "测试乙",
        "routes": [],
        "metrics": {},
        "evidence_ids": [],
    }
    own_pdf = Evidence(
        "cninfo:official_pdf_text",
        "公告正文",
        "已机器提取",
        None,
        None,
        NOW.isoformat(),
        instrument_ids=(CODE,),
    )
    peer_pdf = Evidence(
        "cninfo:official_pdf_text",
        "同行公告",
        "风险提示",
        None,
        None,
        NOW.isoformat(),
        instrument_ids=(peer["instrument_id"],),
    )
    own["evidence_ids"] = [own_pdf.evidence_id]
    row = {
        "instrument_id": CODE,
        "status": "focus",
        "thesis": "本次输入中该公司仅有这一份公告正文，测试乙另有风险提示",
        "risk": "题材归类尚未核实",
        "invalidation": "若公告事实变化则重估",
        "evidence_ids": [f"market:{CODE}", own_pdf.evidence_id, peer_pdf.evidence_id],
    }
    result = {"market_view": "待观察", "selected": [row]}
    with pytest.raises(ValueError, match="discussion-only"):
        validate_selection(result, [own, peer], [own_pdf, peer_pdf])
    row["status"] = "watch"
    assert validate_selection(result, [own, peer], [own_pdf, peer_pdf]) == result
    row["thesis"] = "公司只有这一份公告，测试乙另有风险提示"
    with pytest.raises(ValueError, match="exhaustive disclosure"):
        validate_selection(result, [own, peer], [own_pdf, peer_pdf])
    row["thesis"] = "本次输入中该公司仅有这一份公告正文"
    with pytest.raises(ValueError, match="not shown or bound"):
        validate_selection(result, [own, peer], [own_pdf, peer_pdf])


def test_theme_member_does_not_take_company_event_quota():
    event_code = "600100.SH"
    universe = {
        code: Candidate(code, code, {"amount_ratio_5d": 1.2, "close_location": 0.5}, 0.5)
        for code in [event_code] + [f"600{i:03d}.SH" for i in range(101, 129)]
    }
    for i, code in enumerate(list(universe)[1:]):
        universe[code].routes = [["回撤放量", "趋势突破", "热度观察"][i % 3]]
    hypotheses = [
        {
            "instrument_ids": [code],
            "relation": "third_party_theme_membership_unverified",
            "route_type": "sector",
            "theme_id": "theme-a",
            "evidence_ids": ["ev-theme"],
        }
        for code in list(universe)[1:9]
    ] + [
        {
            "instrument_ids": [event_code],
            "relation": "new_company_event_date_only",
            "route_type": "event",
            "event_date": "2026-10-07",
            "evidence_ids": ["ev-event"],
        }
    ]
    chosen = build_pool(universe, hypotheses, list(universe)[1:9], 24)
    event = next(row for row in chosen if row["instrument_id"] == event_code)
    assert event["allocation_route"] == "event"
    assert sum(row["allocation_route"] == "event" for row in chosen) == 1


def test_dynamic_financial_periods_cover_previous_year_and_current_quarters():
    assert report_periods(date(2026, 5, 8))[:2] == ["20260331", "20251231"]
    assert report_periods(date(2026, 11, 8))[0] == "20260930"
    assert report_periods(date(2026, 10, 8), include_next=True)[0] == "20261231"


def test_holiday_company_event_uses_evidence_cutoff_not_last_market_day(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)
    requested = []

    def fetch(api, params):
        if api == "repurchase":
            requested.append(params)
            rows = [
                {"ts_code": CODE, "ann_date": "20261007", "amount": 20_000_000},
                {"ts_code": CODE, "ann_date": "20261009", "amount": 20_000_000},
            ]
            return [
                row for row in rows if params["start_date"] <= row["ann_date"] <= params["end_date"]
            ]
        return []

    monkeypatch.setattr(pack, "fetch", fetch)
    evidence, leads, _ = collect_market_pack(pack, date(2026, 9, 30), [], {CODE}, {CODE: "测试甲"})
    assert any(row["end_date"] == "20261008" for row in requested)
    assert [item.event_dates for item in evidence if item.source == "tushare:repurchase"] == [
        ("2026-10-07",)
    ]
    assert any(item["route_type"] == "event" and item["instrument_ids"] == [CODE] for item in leads)


def test_weekend_disclosure_is_kept_inside_natural_date_risk_window(tmp_path, monkeypatch):
    pack = TusharePack(tmp_path, NOW, False)

    def fetch(api, params):
        if api == "disclosure_date":
            return [
                {"ts_code": CODE, "ann_date": "20261007", "pre_date": "20261011"},
                {"ts_code": CODE, "ann_date": "20261007", "pre_date": "20261111"},
                {
                    "ts_code": CODE,
                    "ann_date": "20261007",
                    "pre_date": "20261012",
                    "actual_date": "20261008",
                },
            ]
        return []

    monkeypatch.setattr(pack, "fetch", fetch)
    sessions = [date(2026, 10, day) for day in (8, 9, 12, 13, 14, 15, 16, 19, 20, 21)]
    evidence, _ = collect_deep_pack(pack, [{"instrument_id": CODE}], sessions)
    planned = [item for item in evidence if item.kind == "scheduled_disclosure"]
    assert len(planned) == 1
    assert planned[0].event_dates == ("2026-10-11",)


def test_risk_and_compacted_limit_history_reach_final_evidence_packet():
    items = []
    for day in range(24, 29):
        items.append(
            Evidence(
                "tushare:limit_list_d",
                f"涨停 {day}",
                f'{{"trade_date":"202609{day}"}}',
                None,
                None,
                NOW.isoformat(),
                kind="historical_limit_structure",
                instrument_ids=(CODE,),
            )
        )
    for kind in (
        "moneyflow_context",
        "financial_background",
        "known_future_unlock",
        "official_pdf_text_unverified",
        "scheduled_disclosure",
    ):
        items.append(
            Evidence(
                "test",
                kind,
                "已取得材料",
                None,
                None,
                NOW.isoformat(),
                kind=kind,
                instrument_ids=(CODE,),
            )
        )
    packet = evidence_packet(items, {CODE}, max_chars=10_000, max_body_chars=800)
    kinds = [item["kind"] for item in packet]
    assert "scheduled_disclosure" in kinds
    assert kinds.count("historical_limit_summary") == 1
    assert "historical_limit_structure" not in kinds


def test_revalidation_checks_saved_model_output_integrity_before_using_it(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    report = {"status": "incomplete", "selection_raw": {"market_view": "待观察", "selected": []}}
    raw = [{"stage": "selection", "output": {}}]
    (source / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (source / "ai_responses.json").write_text(json.dumps(raw), encoding="utf-8")
    manifest = {"report_sha256": fingerprint(report), "ai_responses_sha256": fingerprint(raw)}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    raw.append({"stage": "tampered"})
    (source / "ai_responses.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="AI response hash mismatch"):
        revalidate(source, tmp_path / "canonical", tmp_path / "out")
    (source / "ai_responses.json").write_text(json.dumps(raw[:1]), encoding="utf-8")
    with pytest.raises(ValueError, match="exact final prompt evidence"):
        revalidate(source, tmp_path / "canonical", tmp_path / "out")
