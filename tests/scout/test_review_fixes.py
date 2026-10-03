"""Regression cases for the independent fdfb54b review, using only synthetic inputs."""

from datetime import date, datetime

from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.opportunities import event_records

SESSION = date(2026, 9, 30)
CUTOFF = datetime(2026, 10, 3, 12, tzinfo=SHANGHAI)
INDEX_BODY = "巨潮公告索引仅提供标题和PDF链接；正文未由Scout读取。公告日期不等于精确发布时间。"


def index_source(title, url, day="2026-09-30", **updates):
    values = {
        "source": "synthetic_cninfo",
        "title": title,
        "body": INDEX_BODY,
        "url": url,
        "published_at": None,
        "retrieved_at": "2026-10-01T10:00:00+08:00",
        "kind": "official_announcement_index_unverified",
        "instrument_ids": ("600001.SH",),
        "event_dates": (day,),
    }
    return Evidence(**(values | updates))


def separate_notices():
    return [
        index_source("关于与甲客户签订销售合同的公告", "https://example.org/a.PDF", "2026-09-28"),
        index_source("关于与乙客户签订采购合同的公告", "https://example.org/b.PDF"),
    ]


def test_different_index_documents_do_not_merge_placeholder_body():
    rows, _ = event_records(separate_notices(), [], SESSION, CUTOFF)
    assert len(rows) == 2 and len({r["record_id"] for r in rows}) == 2
    assert all(len(r["source_ids"]) == 1 for r in rows)


def test_new_index_document_is_not_repeated_across_snapshots():
    first, second = separate_notices()
    old, _ = event_records([first], [], SESSION, CUTOFF)
    current, _ = event_records([second], old, SESSION, CUTOFF)
    assert current[0]["record_id"] != old[0]["record_id"]
    assert current[0]["novelty"] == "unseen_history_unknown"
    assert current[0]["previous_record_id"] is None


def test_same_document_index_and_pdf_share_one_event():
    index = separate_notices()[0]
    pdf = index_source(
        index.title,
        index.url,
        "2026-09-28",
        kind="official_pdf_text_unverified",
        body="正式销售合同原文（合成）",
    )
    rows, _ = event_records([index, pdf], [], SESSION, CUTOFF)
    assert len(rows) == 1 and len(rows[0]["source_ids"]) == 2
    assert rows[0]["title_only"] is False
    # Obtaining the same document's body later does not create a new market event.
    old, _ = event_records([index], [], SESSION, CUTOFF)
    enriched, _ = event_records([pdf], old, SESSION, CUTOFF)
    assert enriched[0]["novelty"] == "repeated_content"


def test_real_body_syndications_still_merge_but_index_without_url_needs_identity():
    first, second = separate_notices()
    full = [
        index_source(s.title, s.url, kind="news", body="完全相同的正式合同新闻正文")
        for s in (first, second)
    ]
    rows, _ = event_records(full, [], SESSION, CUTOFF)
    assert len(rows) == 1 and len(rows[0]["source_ids"]) == 2
    rows, _ = event_records(
        [
            index_source(first.title, None, "2026-09-28"),
            index_source(second.title, None),
        ],
        [],
        SESSION,
        CUTOFF,
    )
    assert len(rows) == 2
