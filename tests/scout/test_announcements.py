"""Targeted announcement index keeps missing publication time and coverage explicit."""

from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from quantlab.scout.models import SHANGHAI
from quantlab.scout.pipeline import DEFAULT_CONFIG
from quantlab.scout.sources import collect_announcements

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
