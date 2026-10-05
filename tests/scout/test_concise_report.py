from copy import deepcopy

from quantlab.scout.html_report import render_html_report
from quantlab.scout.report import render_report


def report():
    return {
        "status": "live_research_unvalidated",
        "finished_at": "2026-10-05T18:01:49+08:00",
        "market": {"session": "2026-09-30"},
        "timing": {"target_session": "2026-10-08"},
        "candidates": [
            {"instrument_id": "a", "name": "观察股"},
            {"instrument_id": "b", "name": "重点股<script>"},
            {"instrument_id": "c", "name": "未选股"},
        ],
        "selection": {
            "selected": [
                {
                    "instrument_id": "a",
                    "status": "watch",
                    "rank": 3,
                    "thesis": "观察理由",
                    "risk": "风险反证",
                    "invalidation": "失效条件",
                },
                {
                    "instrument_id": "b",
                    "status": "focus",
                    "rank": 1,
                    "thesis": "重点理由<script>bad()</script>",
                    "risk": "长审计补充",
                    "invalidation": "资金改变",
                    "comparison": {"risk": "原模型反证"},
                    "screening_status": "hold_for_official_notice_review",
                },
            ]
        },
        "coverage": [{"source": "不展示来源", "status": "failed"}],
        "candidate_diagnostics": {"c": "不展示阶段原因"},
    }


def test_default_views_show_only_selected_reason_risk_and_halt_without_changing_report():
    saved = report()
    before = deepcopy(saved)
    md, html = render_report(saved), render_html_report(saved)
    for view in (md, html):
        assert "2026-10-08" in view and "2026-09-30" in view
        assert "重点理由" in view and "观察理由" in view and "原模型反证" in view
        assert "停牌线索待核查" in view and "资金改变" in view
        assert view.index("重点理由") < view.index("观察理由")
        for omitted in (
            "未选股",
            "长审计补充",
            "机会与相对取舍",
            "信息源覆盖",
            "候选阶段诊断",
            "不展示来源",
            "不展示阶段原因",
        ):
            assert omitted not in view
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "script-src 'none'" in html
    assert saved == before


def test_failed_empty_view_does_not_suggest_model_found_no_opportunity():
    saved = report()
    saved["status"] = "incomplete"
    saved["selection"]["selected"] = []
    for view in (render_report(saved), render_html_report(saved)):
        assert "未通过校验" in view and "重点股" not in view
