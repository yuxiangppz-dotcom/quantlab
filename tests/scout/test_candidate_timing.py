"""New Scout discovery and report timing boundaries."""

from datetime import date, datetime
from unittest.mock import patch

from quantlab.scout.ai import bind_hypotheses
from quantlab.scout.demo import make_demo_market
from quantlab.scout.hot import INTERFACE, SOURCE
from quantlab.scout.models import SHANGHAI, Candidate, Coverage, Evidence
from quantlab.scout.pipeline import (
    DEFAULT_CONFIG,
    build_pool,
    report_timing,
    run_scout,
    scoped_leads,
)
from quantlab.scout.report import present_selection
from quantlab.scout.sources import collect_cninfo_market_index


def _candidate(code: str, score: float, routes: list[str]) -> Candidate:
    return Candidate(
        code,
        code,
        {"amount_ratio_5d": 1.2, "close_location": 0.5},
        score,
        routes=routes,
        evidence_ids=[f"market:{code}"],
    )


def test_unmoved_event_stock_enters_deep_pool_and_overlap_deduplicates():
    universe = {
        "600001.SH": _candidate("600001.SH", 0.99, ["趋势突破"]),
        "000002.SZ": _candidate("000002.SZ", 0.05, []),
        "600003.SH": _candidate("600003.SH", 0.4, ["回撤放量"]),
    }
    event = [
        {
            "instrument_ids": ["000002.SZ"],
            "relation": "announcement_index_unverified",
            "evidence_ids": ["ev-event"],
        }
    ]
    rows = build_pool(universe, event, ["000002.SZ"], 8, ["000002.SZ"])
    codes = [row["instrument_id"] for row in rows]
    assert codes.count("000002.SZ") == 1
    assert codes[0] == "000002.SZ"
    assert "ev-event" in rows[0]["evidence_ids"]
    assert "热度观察" in rows[0]["routes"]


def test_pool_allocates_unique_names_across_overlapping_routes():
    common = [f"600{i:03d}.SH" for i in range(3)]
    event_only = [f"600{i:03d}.SH" for i in range(3, 16)]
    sector_only = "601001.SH"
    pullback_only = "601002.SH"
    attention_only = "601003.SH"
    momentum_only = "601004.SH"
    universe = {
        code: _candidate(code, 1 - i / 100, ["趋势突破"] if code == momentum_only else [])
        for i, code in enumerate(
            common + event_only + [sector_only, pullback_only, attention_only, momentum_only]
        )
    }
    universe[pullback_only].routes = ["回撤放量"]
    event = [
        {"instrument_ids": [code], "relation": "direct", "evidence_ids": ["ev-event"]}
        for code in common + event_only
    ]
    rows = build_pool(
        universe,
        event,
        common + [sector_only],
        10,
        common + [attention_only],
    )
    codes = [row["instrument_id"] for row in rows]
    assert len(codes) == len(set(codes)) == 10
    assert {sector_only, pullback_only, attention_only, momentum_only} <= set(codes)


def test_original_model_claim_must_fail_before_presentation():
    from quantlab.scout.ai import retain_valid_selection, validate_selection

    code = "600001.SH"
    candidate = _candidate(code, 0.8, ["趋势突破"]).to_dict()
    original = {
        "market_view": "待核查",
        "selected": [
            {
                "instrument_id": code,
                "status": "focus",
                "thesis": "封单很强，明日容易买到",
                "risk": "未知",
                "invalidation": "若走势逆转则失效",
                "evidence_ids": [f"market:{code}"],
            }
        ],
    }
    from pytest import raises

    with raises(ValueError, match="order-book, execution or causal"):
        validate_selection(original, [candidate], [])
    with raises(ValueError, match="order-book, execution or causal"):
        validate_selection(present_selection(original, [candidate]), [candidate], [])
    valid, rejected = retain_valid_selection(original, [candidate], [])
    assert valid["selected"] == []
    assert rejected[0]["instrument_id"] == code
    assert rejected[0]["status"] == "focus"


def test_validated_model_reason_is_preserved_in_presentation():
    from quantlab.scout.ai import retain_valid_selection

    code = "600001.SH"
    candidate = _candidate(code, 0.8, ["趋势突破"]).to_dict()
    original = {
        "market_view": "节后行情方向仍不确定",
        "selected": [
            {
                "instrument_id": code,
                "status": "watch",
                "thesis": "观察趋势能否延续，尚无公司级催化证据",
                "risk": "既有涨幅可能已经反映预期",
                "invalidation": "若相对强度转弱则重估",
                "evidence_ids": [f"market:{code}"],
            }
        ],
    }
    valid, rejected = retain_valid_selection(original, [candidate], [])
    shown = present_selection(valid, [candidate])
    assert rejected == []
    assert shown["selected"][0]["thesis"] == original["selected"][0]["thesis"]
    assert shown["selected"][0]["invalidation"] == original["selected"][0]["invalidation"]


def test_event_recall_uses_entire_admitted_index_not_prompt_first_80():
    now = datetime(2026, 9, 30, 20, tzinfo=SHANGHAI).isoformat()
    items = [
        Evidence(
            "cninfo:market_index",
            f"普通公告{i}",
            "标题索引",
            None,
            None,
            now,
            kind="official_announcement_index_unverified",
            instrument_ids=("600001.SH",),
        )
        for i in range(80)
    ]
    items.append(
        Evidence(
            "cninfo:market_index",
            "关于重大合同的公告",
            "标题索引",
            None,
            None,
            now,
            kind="official_announcement_index_unverified",
            instrument_ids=("000002.SZ",),
        )
    )
    leads = scoped_leads(items, {"600001.SH", "000002.SZ"})
    assert any(h["instrument_ids"] == ["000002.SZ"] for h in leads)
    assert leads[0]["instrument_ids"] == ["000002.SZ"]


def test_market_wide_official_index_recalls_without_price_seed():
    now = datetime(2026, 9, 30, 20, tzinfo=SHANGHAI)

    def fake_query(_, form):
        if form["plate"] == "sh":
            return {
                "announcements": [
                    {
                        "secCode": "600001",
                        "announcementTime": int(now.timestamp() * 1000),
                        "announcementTitle": "关于重大合同的公告",
                        "adjunctUrl": "finalpage/2026-09-30/123.PDF",
                    }
                ],
                "hasMore": False,
            }
        return {"announcements": [], "hasMore": False}

    with patch("quantlab.scout.sources.read_cninfo_json", side_effect=fake_query):
        found, coverage = collect_cninfo_market_index(
            DEFAULT_CONFIG | {"cninfo_market_index": True}, now, True, {"600001.SH"}
        )
    assert coverage.status == "sampled"
    assert [item.instrument_ids for item in found] == [("600001.SH",)]
    assert found[0].published_at is None


def test_company_notice_does_not_bind_to_another_company_by_model_url():
    now = datetime(2026, 9, 30, 20, tzinfo=SHANGHAI).isoformat()
    notice = Evidence(
        "cninfo:market_index",
        "甲公司公告",
        "标题索引",
        "https://static.cninfo.com.cn/finalpage/2026-09-30/123.PDF",
        None,
        now,
        kind="official_announcement_index_unverified",
        instrument_ids=("600001.SH",),
    )
    model = {
        "hypotheses": [
            {
                "instrument_ids": ["000002.SZ"],
                "relation": "direct",
                "source_urls": [notice.url],
                "summary": "错误跨公司关系",
                "counterargument": "未知",
            }
        ]
    }
    assert bind_hypotheses(model, [notice], {"000002.SZ"}) == []


def test_hot_rank_beyond_first_40_has_scoped_evidence_and_name_check(tmp_path):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    snapshot = {
        "schema_version": 1,
        "source": SOURCE,
        "interface": INTERFACE,
        "source_url": "https://guba.eastmoney.com/rank/",
        "scope": "current_top100",
        "retrieved_at": datetime.now(SHANGHAI).isoformat(),
        "raw_rows": [],
        "normalized": [
            {
                "instrument_id": "600007.SH",
                "name": "样例七",
                "rank": 50,
                "previous_rank": None,
                "rank_delta": None,
                "last_price": None,
                "change_status": "unknown_no_previous_snapshot",
            },
            {
                "instrument_id": "600006.SH",
                "name": "不匹配的公司",
                "rank": 60,
                "previous_rank": None,
                "rank_delta": None,
                "last_price": None,
                "change_status": "unknown_no_previous_snapshot",
            },
        ],
    }
    # Use the actual demo name to distinguish a true code/name match from a guess.
    from quantlab.scout.market import scan_market

    universe, _ = scan_market(canonical, day)
    snapshot["normalized"][0]["name"] = universe["600007.SH"].name
    with patch(
        "quantlab.scout.pipeline.collect_hot_rank",
        return_value=(snapshot, Coverage(SOURCE, "ok", 1)),
    ):
        _, report = run_scout(
            canonical,
            tmp_path / "runs",
            DEFAULT_CONFIG | {"akshare_hot_rank": True},
            session=day,
        )
    candidate = report["market_universe"]["600007.SH"]
    assert candidate["context"]["hot_rank"]["rank"] == 50
    assert any(
        item["kind"] == "attention_rank_unverified" and item["instrument_ids"] == ("600007.SH",)
        for item in report["evidence"]
    )
    assert "600006.SH" in report["hot_identity_conflicts"]
    assert "hot_rank" not in report["market_universe"]["600006.SH"]["context"]


def test_report_timing_uses_exchange_sessions_and_actual_completion():
    days = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)]
    evening = report_timing(days, datetime(2026, 10, 8, 20, tzinfo=SHANGHAI), days[0], True)
    morning = report_timing(days, datetime(2026, 10, 9, 9, 29, tzinfo=SHANGHAI), days[0], True)
    late = report_timing(days, datetime(2026, 10, 9, 9, 31, tzinfo=SHANGHAI), days[0], False)
    assert (evening["target_session"], evening["report_kind"]) == (
        "2026-10-09",
        "next_session_prep",
    )
    assert (morning["target_session"], morning["report_kind"]) == ("2026-10-09", "premarket")
    assert (late["target_session"], late["report_kind"]) == ("2026-10-12", "next_session_prep")
    assert not late["primary_eligible"]


def test_empty_focus_has_explicit_reason():
    result = present_selection({"market_view": "证据不足", "selected": []}, [])
    assert result["selected"] == []
    assert result["no_recommendation_reason"]


def test_same_live_inputs_reuse_frozen_analysis_without_second_model_call(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    day = make_demo_market(canonical)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    config = DEFAULT_CONFIG | {
        "provider": "openai",
        "model": "test-model",
        "tushare_news_sources": [],
        "tushare_industry": False,
        "tushare_disclosures": False,
    }
    calls = []

    def fake_ask(client, prompt, schema, search=False):
        calls.append(search)
        client.calls.append({"status": "completed", "search": search})
        return (
            ({"hypotheses": []} if search else {"market_view": "证据不足", "selected": []}),
            {"status": "completed"},
        )

    with (
        patch("quantlab.scout.pipeline.latest_completed_session", return_value=day),
        patch("quantlab.scout.ai.OpenAIResearch.ask", new=fake_ask),
    ):
        first, _ = run_scout(canonical, tmp_path / "runs", config, online=True)
        second, _ = run_scout(canonical, tmp_path / "runs", config, online=True)
    assert first == second
    assert calls == [True, True, False]
