"""Targeted announcement index keeps missing publication time and coverage explicit."""

from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from quantlab.scout.models import SHANGHAI, Evidence, admit_evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG
from quantlab.scout.report import (
    announcement_index_lines,
    hold_candidate_pool,
    screen_notice_risks,
)
from quantlab.scout.sources import (
    CNINFO_QUERY,
    CNINFO_STOCKS,
    collect_announcements,
    collect_cninfo_announcements,
)

NOW = datetime(2026, 1, 10, 12, tzinfo=SHANGHAI)
CODE = "600001.SH"
CONFIG = DEFAULT_CONFIG | {"tushare_announcements": True}


def test_announcements_disabled_never_contacts_provider(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    with patch("tushare.pro_api") as provider:
        evidence, coverage = collect_announcements(DEFAULT_CONFIG, NOW, True, [CODE])
    assert evidence == []
    assert coverage.status == "not_configured"
    provider.assert_not_called()


def test_announcements_are_titles_only_with_unknown_naive_time(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    rows = [
        {
            "ts_code": CODE,
            "ann_date": "20260110",
            "title": "正式公告甲",
            "url": "https://static.cninfo.com.cn/a.pdf",
            "rec_time": "2026-01-10 09:00:00+08:00",
        },
        {
            "ts_code": CODE,
            "ann_date": "20260110",
            "title": "正式公告乙",
            "url": "https://static.cninfo.com.cn/b.pdf",
            "rec_time": "2026-01-10 08:00:00",
        },
        {
            "ts_code": "000001.SZ",
            "ann_date": "20260110",
            "title": "错股公告",
            "url": "https://static.cninfo.com.cn/c.pdf",
        },
        {
            "ts_code": CODE,
            "ann_date": "20260110",
            "title": "未来公告",
            "url": "https://static.cninfo.com.cn/d.pdf",
            "rec_time": "2026-01-11 09:00:00+08:00",
        },
    ]
    api = Mock()
    api.anns_d.return_value = pd.DataFrame(rows)
    with patch("tushare.pro_api", return_value=api):
        evidence, coverage = collect_announcements(CONFIG, NOW, True, [CODE])
    assert len(evidence) == 2
    assert evidence[0].published_at == "2026-01-10T09:00:00+08:00"
    assert evidence[1].published_at is None
    assert evidence[0].kind == "official_announcement_index_unverified"
    assert "PDF正文未读取" in evidence[0].body
    assert evidence[0].event_dates == ("2026-01-10",)
    assert coverage.status == "partial"
    assert "2 rejected rows" in coverage.detail
    assert api.anns_d.call_args.kwargs["ts_code"] == CODE


def test_announcements_query_is_bounded_and_failures_remain_unknown(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    api = Mock()
    api.anns_d.side_effect = RuntimeError("provider permission unavailable")
    with patch("tushare.pro_api", return_value=api):
        evidence, coverage = collect_announcements(
            CONFIG, NOW, True, [f"{i:06d}.SH" for i in range(9)]
        )
    assert evidence == []
    assert api.anns_d.call_count == 8
    assert coverage.status == "failed"
    assert "8 failed queries" in coverage.detail


def test_cninfo_disabled_never_contacts_provider():
    with patch("quantlab.scout.sources.read_cninfo_json") as provider:
        evidence, coverage = collect_cninfo_announcements(DEFAULT_CONFIG, NOW, True, [CODE])
    assert evidence == []
    assert coverage.status == "not_configured"
    provider.assert_not_called()


def test_cninfo_title_is_visible_but_midnight_is_not_publication_time():
    config = DEFAULT_CONFIG | {"cninfo_announcements": True}
    code = "002813.SZ"

    def official_response(url, form=None):
        if url == CNINFO_STOCKS:
            return {"stockList": [{"code": "002813", "orgId": "9900026536"}]}
        assert url == CNINFO_QUERY
        assert form["stock"] == "002813,9900026536"
        assert form["column"] == "szse"
        return {
            "hasMore": False,
            "announcements": [
                {
                    "secCode": "002813",
                    "announcementTitle": "关于控制权变更暨停牌的公告",
                    "announcementTime": 1768003200000,
                    "adjunctUrl": "finalpage/2026-01-10/1225589222.PDF",
                },
                {
                    "secCode": "000513",
                    "announcementTitle": "错股公告",
                    "announcementTime": 1768003200000,
                    "adjunctUrl": "finalpage/2026-01-10/1225589223.PDF",
                },
            ],
        }

    with patch("quantlab.scout.sources.read_cninfo_json", side_effect=official_response):
        evidence, coverage = collect_cninfo_announcements(config, NOW, True, [code])
    assert len(evidence) == 1
    assert evidence[0].published_at is None
    assert evidence[0].event_dates == ("2026-01-10",)
    assert evidence[0].url == "https://static.cninfo.com.cn/finalpage/2026-01-10/1225589222.PDF"
    assert coverage.status == "partial"
    lines = announcement_index_lines([x.to_dict() for x in evidence], code)
    assert "停牌风险线索" in lines[0]
    assert "精确发布时间未知" in lines[1]
    assert "正文未读取" in lines[1]
    later = evidence[0].to_dict() | {"source": "cninfo:official_index_post_selection"}
    assert "模型分级后补查" in announcement_index_lines([later], code)[1]
    assert "未检出" in announcement_index_lines([], code)[0]
    model_selection = {"market_view": "", "selected": [{"instrument_id": code, "status": "focus"}]}
    screened = screen_notice_risks(model_selection, [x.to_dict() for x in evidence])
    assert screened["selected"][0]["status"] == "focus"
    assert screened["selected"][0]["screening_status"] == "hold_for_official_notice_review"
    assert "screening_status" not in model_selection["selected"][0]
    pool = [{"instrument_id": code, "cautions": []}]
    held_pool = hold_candidate_pool(pool, screened)
    assert held_pool[0]["screening_status"] == "hold_for_official_notice_review"
    assert "交易状态待核实" in held_pool[0]["cautions"][0]
    assert pool[0]["cautions"] == []


def test_cninfo_empty_and_page_cap_are_not_full_coverage():
    config = DEFAULT_CONFIG | {"cninfo_announcements": True}

    def response(url, form=None):
        if url == CNINFO_STOCKS:
            return {"stockList": [{"code": "600001", "orgId": "gssh0600001"}]}
        assert form["column"] == "sse"
        return {"hasMore": True, "announcements": None, "totalAnnouncement": 0}

    with patch("quantlab.scout.sources.read_cninfo_json", side_effect=response):
        evidence, coverage = collect_cninfo_announcements(config, NOW, True, [CODE])
    assert evidence == []
    assert coverage.status == "possibly_truncated"
    assert "empty is not proof of absence" in coverage.detail


def test_same_generic_title_for_distinct_stocks_is_not_deduplicated():
    items = [
        Evidence(
            source="cninfo:official_index",
            title="股票交易异常波动公告",
            body="公告索引，正文未读。",
            url=f"https://static.cninfo.com.cn/finalpage/2026-01-10/{number}.PDF",
            published_at=None,
            retrieved_at=NOW.isoformat(),
            kind="official_announcement_index_unverified",
            instrument_ids=(code,),
            event_dates=("2026-01-10",),
        )
        for code, number in (("000011.SZ", "1"), ("002813.SZ", "2"))
    ]
    admitted, counts = admit_evidence(items, NOW, 72)
    assert len(admitted) == 2
    assert counts["duplicate"] == 0
