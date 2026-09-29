"""The standalone view must expose evidence boundaries without executing source text."""

from datetime import datetime

from quantlab.scout.html_report import render_html_report
from quantlab.scout.models import SHANGHAI, Evidence


def test_html_view_escapes_sources_and_shows_halt_across_candidate():
    now = datetime(2026, 1, 10, 12, tzinfo=SHANGHAI).isoformat()
    notice = Evidence(
        source="cninfo:official_index",
        title="关于停牌的公告 <script>alert(1)</script>",
        body="索引标题，正文未读",
        url="https://static.cninfo.com.cn/finalpage/2026-01-10/1.PDF",
        published_at=None,
        retrieved_at=now,
        kind="official_announcement_index_unverified",
        instrument_ids=("600001.SH",),
        event_dates=("2026-01-10",),
    )
    report = {
        "status": "live_research_unvalidated",
        "market": {"session": "2026-01-09"},
        "finished_at": now,
        "ai_provider": "deepseek",
        "ai_model": "deepseek-flash",
        "selection": {
            "selected": [
                {
                    "instrument_id": "600001.SH",
                    "status": "focus",
                    "screening_status": "hold_for_official_notice_review",
                    "risk": "未知 </section><script>attack()</script>",
                    "evidence_ids": ["market:600001.SH", notice.evidence_id],
                }
            ]
        },
        "candidates": [
            {
                "instrument_id": "600001.SH",
                "name": "测试<script>",
                "metrics": {
                    "return_1d": 0.01,
                    "return_5d": 0.02,
                    "return_20d": 0.03,
                    "amount_cny": 100_000_000,
                    "amount_ratio_5d": 2.0,
                    "breakout_20d": 0.01,
                    "one_price_session": False,
                    "close_location": 0.5,
                    "up_limit": None,
                },
            }
        ],
        "disclosure_context": {},
        "evidence": [notice.to_dict()],
        "coverage": [
            {
                "source": "cninfo_official_index_targeted",
                "status": "targeted_only",
                "count": 1,
                "detail": "PDF未读",
            }
        ],
        "limitations": ["没有净收益证据"],
    }
    html = render_html_report(report)
    assert "<!doctype html>" in html
    assert "script-src 'none'" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "暂停候选资格" in html
    assert "发布时间 未知" in html
    assert f"href='#evidence-{notice.evidence_id}'" in html
    assert "https://static.cninfo.com.cn/finalpage/2026-01-10/1.PDF" in html
    assert "没有净收益证据" in html
