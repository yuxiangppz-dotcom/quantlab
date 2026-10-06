"""Holiday and source cap boundaries, without contacting providers."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.pipeline import DEFAULT_CONFIG
from quantlab.scout.sources import (
    collect_cninfo_market_index,
    collect_cninfo_pdf_bodies,
    collect_sources,
    pdf_relevant_text,
    source_cache,
    source_window,
)

NOW = datetime(2026, 10, 8, 8, tzinfo=SHANGHAI)
START = datetime(2026, 9, 30, 15, tzinfo=SHANGHAI)
CONFIG = DEFAULT_CONFIG | {"_source_window_start": START.isoformat()}


def test_holiday_window_is_d0_close_not_three_days_or_72_hours():
    assert source_window(CONFIG, NOW) == (START, NOW)
    forms = []

    def read(_, form):
        forms.append(form)
        return {"announcements": [], "hasMore": False}

    with patch("quantlab.scout.sources.read_cninfo_json", side_effect=read):
        rows, coverage = collect_cninfo_market_index(
            CONFIG | {"cninfo_market_index": True}, NOW, True, set()
        )
    assert rows == []
    assert len(forms) == 2
    assert all(form["seDate"] == "2026-09-30~2026-10-08" for form in forms)
    assert START.isoformat() in coverage.detail
    assert "not exhaustive" in coverage.detail


def test_truncated_index_uses_bounded_date_supplements_and_explicit_gaps():
    forms = []

    def read(_, form):
        forms.append(form)
        return {"announcements": [], "hasMore": True}

    with patch("quantlab.scout.sources.read_cninfo_json", side_effect=read):
        rows, coverage = collect_cninfo_market_index(
            CONFIG | {"cninfo_market_index": True}, NOW, True, set()
        )
    assert rows == [] and len(forms) == 12
    assert coverage.status == "possibly_truncated"
    assert any(form["seDate"] == "2026-09-30~2026-09-30" for form in forms)
    assert "not_queried_budget" in coverage.detail


def test_later_page_restriction_survives_short_packet_with_location():
    text, coverage = pdf_relevant_text(
        [
            "重大合同中标金额人民币，预计有助收入增长。" * 50,
            "背景材料" * 300,
            "重大风险：合同为多年框架协议，尚需审批，不能保证履行。",
        ]
    )
    assert "第3页" in text[:700]
    assert "不能保证履行" in text[:700]
    assert "第1页" in text[:1100]
    assert coverage["truncated"]
    assert coverage["omitted_fragments"]


def test_contract_without_old_financial_terms_can_receive_pdf_body():
    notice = Evidence(
        "cninfo:market_index",
        "重大合同中标公告",
        "标题索引",
        "https://static.cninfo.com.cn/finalpage/2026-10-01/123.PDF",
        None,
        NOW.isoformat(),
        "official_announcement_index_unverified",
        ("600001.SH",),
        event_dates=("2026-10-01",),
    )
    reader = Mock(
        is_encrypted=False,
        pages=[
            Mock(extract_text=Mock(return_value="重大合同条款及金额说明。" * 12)),
            Mock(extract_text=Mock(return_value="合同风险尚需审批，条件未满足可终止。" * 8)),
        ],
    )
    with (
        patch("quantlab.scout.sources.read_cninfo_pdf", return_value=b"%PDF-test") as fetch,
        patch("pypdf.PdfReader", return_value=reader),
    ):
        rows, coverage = collect_cninfo_pdf_bodies(
            CONFIG | {"cninfo_announcements": True, "cninfo_pdf_bodies": True}, NOW, True, [notice]
        )
    fetch.assert_called_once()
    assert coverage.status == "targeted_only"
    assert "第2页" in rows[0].body[:700] and "可终止" in rows[0].body[:700]


def test_historical_source_cache_has_collection_watermark_and_current_day_refreshes(tmp_path):
    config = CONFIG | {"_source_cache_root": str(tmp_path)}
    fetch = Mock(return_value={"announcements": []})
    first, watermark = source_cache(config, "index", "past-day-query", NOW, fetch, historical=True)
    again, saved_watermark = source_cache(
        config, "index", "past-day-query", NOW, fetch, historical=True
    )
    assert first == again and watermark == saved_watermark
    assert fetch.call_count == 1
    source_cache(config, "index", "today-query", NOW, fetch, historical=False)
    source_cache(config, "index", "today-query", NOW, fetch, historical=False)
    assert fetch.call_count == 3
    assert len(list(tmp_path.rglob("*.json"))) == 1


def test_news_covers_early_holiday_and_reuses_completed_slices(tmp_path):
    queries = []

    def news(**query):
        queries.append(query)
        published = datetime.fromisoformat(query["start_date"]) + timedelta(minutes=1)
        return Mock(
            to_dict=Mock(
                return_value=[
                    {
                        "datetime": published.isoformat(),
                        "title": "新闻合成样例",
                        "content": "事实待核实",
                    }
                ]
            )
        )

    api = Mock(news=news)
    config = CONFIG | {"_source_cache_root": str(tmp_path)}
    with (
        patch.dict("os.environ", {"TUSHARE_TOKEN": "synthetic-test-token"}),
        patch("tushare.pro_api", return_value=api),
    ):
        rows, coverage = collect_sources(config, NOW, True)
        first_calls = len(queries)
        again, _ = collect_sources(config, NOW, True)
    assert first_calls == 8
    assert len(queries) == 9  # Only the current-day final slice is refreshed.
    assert any(row.published_at.startswith("2026-10-01") for row in rows)
    assert {row.evidence_id for row in rows} == {row.evidence_id for row in again}
    assert "8/8 bounded requests" in coverage[-1].detail
    assert START.isoformat() in coverage[-1].detail


def test_news_failure_type_does_not_persist_provider_secret():
    api = Mock(news=Mock(side_effect=RuntimeError("synthetic-secret-in-provider-error")))
    with (
        patch.dict("os.environ", {"TUSHARE_TOKEN": "synthetic-test-token"}),
        patch("tushare.pro_api", return_value=api),
    ):
        rows, coverage = collect_sources(CONFIG, NOW, True)
    assert rows == [] and coverage[-1].status == "failed"
    assert "RuntimeError" in coverage[-1].detail
    assert "synthetic-secret" not in coverage[-1].detail
