"""The standalone view must expose evidence boundaries without executing source text."""

from datetime import datetime

import pytest

from quantlab.scout.html_report import render_html_report
from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.report import execution_observation


def test_execution_summary_counts_observed_and_unknown_separately():
    report = {
        "selection": {
            "selected": [{"instrument_id": "a"}, {"instrument_id": "b"}],
        },
        "candidates": [
            {
                "instrument_id": "a",
                "metrics": {"close": 11, "up_limit": 11, "one_price_session": True},
            },
            {
                "instrument_id": "b",
                "metrics": {"close": 12, "up_limit": None, "one_price_session": False},
            },
        ],
    }
    count, note = execution_observation(report)
    assert count == 1
    assert "1只当日收盘价等于涨停价" in note
    assert "1只出现一价行情（两项可重叠）" in note
    assert "另有1只涨停价未知" in note
    assert "不能推断可按该收盘价成交" in note


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
        "run_id": "demo-review-run",
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
    theme = Evidence(
        source="tushare:kpl_list",
        title="第三方题材<script>",
        body="供应商归类，非公司事实",
        url=None,
        published_at=None,
        retrieved_at=now,
        kind="theme_board_unverified",
        instrument_ids=("600001.SH",),
        event_dates=("2026-01-09",),
    )
    report["evidence"].append(theme.to_dict())
    html = render_html_report(report)
    assert "<!doctype html>" in html
    assert "script-src 'none'" in html
    assert "href='#candidates'" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "优先核查" in html
    assert "停牌线索 · 交易状态待核查" in html
    assert "第三方涨停题材标签（未核实，非公司公告）" in html
    assert "发布时间 未知" in html
    assert "1条公告日期晚于行情日2026-01-09" in html
    assert "模型分级不得用于该收盘时点的回测评价" in html
    assert "其中0条PDF正文在模型分级前机器提取" in html
    assert "涨停价未知" in html
    assert "次日开盘价和盘口未知" in html
    assert f"href='#evidence-{notice.evidence_id}'" in html
    assert "https://static.cninfo.com.cn/finalpage/2026-01-10/1.PDF" in html
    assert "没有净收益证据" in html

    extracted = Evidence(
        source="cninfo:official_pdf_text",
        title="机器提取正文",
        body="正文风险 <script>不能执行</script>",
        url=notice.url,
        published_at=None,
        retrieved_at=now,
        kind="official_pdf_text_unverified",
        instrument_ids=("600001.SH",),
        event_dates=("2026-01-10",),
    )
    report["evidence"].append(extracted.to_dict())
    with_body = render_html_report(report)
    assert "展开机器提取正文（未人工核实）" in with_body
    assert "正文风险 &lt;script&gt;不能执行&lt;/script&gt;" in with_body
    assert "其中1条PDF正文在模型分级前机器提取" in with_body
    assert "<script>不能执行</script>" not in with_body

    review = {
        "run_id": report["run_id"],
        "reviewed_at": "2026-01-11T12:00:00+08:00",
        "findings": [
            {
                "instrument_id": "600001.SH",
                "category": "risk",
                "source_url": notice.url,
                "source_sha256": "0" * 64,
                "summary": "正文风险 <script>attack()</script>",
            }
        ],
    }
    reviewed_html = render_html_report(report, review)
    assert "报告生成后的人工公告正文复核" in reviewed_html
    assert "报告后人工复核备注（备注未参与模型分级）" in reviewed_html
    assert "本轮部分PDF机器正文已在模型前采集" in reviewed_html
    assert "正文风险 &lt;script&gt;attack()&lt;/script&gt;" in reviewed_html
    assert "<script>" not in reviewed_html
    assert "后置复核来自独立文件" in reviewed_html
    with pytest.raises(ValueError, match="archived official URL"):
        render_html_report(
            report,
            {**review, "findings": [{**review["findings"][0], "source_url": "https://other.org"}]},
        )
    with pytest.raises(ValueError, match="later than report generation"):
        render_html_report(report, {**review, "reviewed_at": now})
    with pytest.raises(ValueError, match="run_id must match"):
        render_html_report(report, {**review, "run_id": "different-run"})
    with pytest.raises(ValueError, match="selected stock"):
        render_html_report(
            report,
            {**review, "findings": [{**review["findings"][0], "instrument_id": "600002.SH"}]},
        )
    with pytest.raises(ValueError, match="selected stock"):
        render_html_report(
            report,
            {**review, "findings": [{**review["findings"][0], "instrument_id": ["bad"]}]},
        )
