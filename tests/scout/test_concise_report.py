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


def test_six_card_items_preserve_audit_coverage_without_global_source_dump():
    from quantlab.scout.concise_report import card_content

    saved = report()
    saved["timing"]["information_cutoff"] = "2026-10-05T18:00:00+08:00"
    saved["coverage"] = [
        {"source": "news:cls", "status": "failed"},
        {"source": "akshare:hot", "status": "failed"},
        {"source": "local:comments", "status": "not_configured"},
    ]
    row = saved["selection"]["selected"][0]
    row.update(
        primary_type="trend_continuation",
        comparator_id="c",
        difference="仅有已归档日线差异，经营证据仍不足",
        analysis={"h5_mechanism": "若行业内相对表现保持才可能延续，仍未验证"},
        trade_conditions={"known": "日线已知", "unknown": "目标日价格及执行条件待确认"},
    )
    assert len(card_content(saved, row)) == 6
    before = deepcopy(saved)
    for view in (render_report(saved), render_html_report(saved)):
        for required in (
            "2026-10-05T18:00",
            "后续假设",
            "比较对象 c",
            "执行条件待确认",
            "尚无可核查的失效规则",
        ):
            assert required in view
        assert "候选阶段诊断" not in view
        for source in ("news:cls", "akshare:hot", "local:comments"):
            assert source not in view
    assert saved == before
