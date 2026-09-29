"""A later targeted lookup must not mutate or overstate an archived run."""

from datetime import datetime

from quantlab.scout.models import SHANGHAI, Coverage, Evidence
from quantlab.scout.recheck import compare_announcement_index


def notice(code: str, title: str, url: str) -> Evidence:
    return Evidence(
        source="cninfo:official_index_post_selection",
        title=title,
        body="索引，正文未读",
        url=url,
        published_at=None,
        retrieved_at="2026-09-30T08:30:00+08:00",
        kind="official_announcement_index_unverified",
        instrument_ids=(code,),
        event_dates=("2026-09-30",),
    )


def test_recheck_only_lists_new_selected_official_links():
    old = notice("600001.SH", "旧公告", "https://static.cninfo.com.cn/old.PDF")
    new = notice("600001.SH", "关于停牌的公告", "https://static.cninfo.com.cn/new.PDF")
    other = notice("600002.SH", "其他股票公告", "https://static.cninfo.com.cn/other.PDF")
    report = {
        "run_id": "saved-run",
        "market": {"session": "2026-09-29"},
        "selection": {"selected": [{"instrument_id": "600001.SH"}]},
        "evidence": [old.to_dict()],
    }
    result = compare_announcement_index(
        report,
        [old, new, new, other],
        Coverage("cninfo", "partial", 4, "one failed query"),
        datetime(2026, 9, 30, 8, 31, tzinfo=SHANGHAI),
    )
    assert result["source_run_id"] == "saved-run"
    assert len(result["source_report_sha256"]) == 64
    assert result["archived_selected_index_count"] == 1
    assert result["fresh_index_record_count"] == 4
    assert result["coverage"]["status"] == "partial"
    assert len(result["not_in_archived_index"]) == 1
    assert result["not_in_archived_index"][0]["url"] == new.url
    assert result["not_in_archived_index"][0]["halt_title"] is True
    assert result["not_in_archived_index"][0]["published_at"] is None
    assert len(report["evidence"]) == 1
