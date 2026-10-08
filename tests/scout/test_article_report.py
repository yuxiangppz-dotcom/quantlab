"""Frozen-report privacy, participation caps and truthful missing-evidence displays."""

from copy import deepcopy
from html.parser import HTMLParser

import pytest

from quantlab.scout.article_report import render_report


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.scripts = []
        self.external_assets = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts.append(dict(attrs))
        if tag in {"script", "img", "link", "iframe"}:
            self.external_assets.extend(value for key, value in attrs if key in {"src", "href"})


def candidate(code="600001.SH", status="priority"):
    return {
        "ts_code": code,
        "name": "合成研究股票",
        "route": "base_breakout",
        "final_status": status,
        "program": {
            "setup": {"B": 10.05},
            "metrics": {"amount_ratio": 1.6, "ret5": 0.02},
            "moneyflow": {
                "status": "complete",
                "windows": {
                    "1": {"flow_ratio": 0.012},
                    "3": {"flow_ratio": -0.002, "large_order_ratio": 0.003},
                    "5": {"flow_ratio": -0.008},
                },
            },
            "lhb": {"status": "not_listed", "action": "pass"},
            "levels": {
                "status": "complete",
                "highest_60": {"price": 11.2},
                "latest_confirmed_swing_high": {"price": 10.9},
                "levels": [
                    {
                        "lower": 9.8,
                        "upper": 9.9,
                        "significant": True,
                        "status": "active",
                        "current_role": "support",
                    }
                ],
            },
            "entry": {
                "reference": 10.05,
                "entry_low": 10.05,
                "entry_high": 10.25,
                "invalidation": 9.82,
                "nearest_resistance_lower": 10.9,
                "status": "valid",
                "entry_check": "pending",
            },
            "unlock": {"action": "pass"},
        },
        "ai": {
            "rationale": "同组相比突破位置较近，但需观察后续修复是否持续。",
            "next_day_hypothesis": "观察价格是否守住参考区间；未守住则放弃。",
            "strongest_counter": "上方历史压力较近，资金五日口径仍为负。",
            "intraday": {"quality": "unknown", "image_ids": [], "per_day_findings": []},
        },
        "chart": {"chart_status": "unavailable", "visible_dates": [], "image_ids": []},
        "reason_codes": [],
    }


def report(mode="active", candidates=None):
    return {
        "run_id": "synthetic-report",
        "strategy_version": "test",
        "config_hash": "not-a-live-hash",
        "signal_date": "2026-09-30",
        "target_date": "2026-10-08",
        "cutoff_at": "2026-10-08T08:45:00+08:00",
        "generated_at": "2026-10-08T08:45:01+08:00",
        "market": {"mode": mode, "metrics": {"adv_ratio": 0.60, "median_ret1": 0.002}},
        "directions": [
            {"group_id": "synthetic-group", "name": "合成方向", "status": "established"}
        ],
        "candidates": [candidate()] if candidates is None else candidates,
        "status": "frozen",
        "cold_start_notes": ["internal-sensitive-funnel"],
        "funnel": {"rejected": ["do-not-publish-stock"]},
        "local_path": "private-local-path",
    }


def parse(html):
    reader = TextReader()
    reader.feed(html)
    return reader, " ".join(reader.text)


def test_mobile_report_and_markdown_preserve_same_selected_facts_without_internal_funnel():
    md, html = render_report(report())
    reader, visible = parse(html)
    for text in (
        "600001.SH",
        "1日 +1.20%",
        "3日 -0.20%",
        "5日 -0.80%",
        "¥10.05",
        "¥10.25",
        "¥9.82",
        "行情截至 2026-09-30",
        "目标日开盘条件待确认",
        "分时未核验",
        "最强反证",
    ):
        assert text in visible
        assert text in md
    for private in (
        "internal-sensitive-funnel",
        "do-not-publish-stock",
        "private-local-path",
        "config_hash",
    ):
        assert private not in html and private not in md
    assert "name='viewport'" in html
    assert not reader.scripts and not reader.external_assets


def test_report_treats_model_and_provider_text_as_plain_untrusted_text():
    source = report()
    dangerous = "<script>alert('secret')</script> [open](javascript:alert(1))\n## forged selection"
    source["candidates"][0]["name"] = dangerous
    source["candidates"][0]["ai"]["rationale"] = dangerous
    md, html = render_report(source)
    reader, visible = parse(html)
    assert not reader.scripts
    assert dangerous in visible
    assert "<script>" not in html and "<script>" not in md
    assert "\n## forged selection" not in md
    assert "\\[open\\]" in md


@pytest.mark.parametrize(
    "mode,total,priority",
    [
        ("active", 5, 3),
        ("selective", 3, 1),
        ("risk_off", 3, 0),
        ("unknown", 3, 0),
    ],
)
def test_market_caps_are_enforced_without_silently_trimming_frozen_candidates(
    mode, total, priority
):
    rows = [
        candidate(f"600{i:03}.SH", "priority" if i < priority else "watch") for i in range(total)
    ]
    render_report(report(mode, rows))
    rows.append(candidate("600999.SH", "watch"))
    with pytest.raises(ValueError, match="participation caps"):
        render_report(report(mode, rows))
    rows = [candidate(f"600{i:03}.SH", "priority") for i in range(priority + 1)]
    with pytest.raises(ValueError, match="participation caps"):
        render_report(report(mode, rows))


def test_invalid_selected_status_and_duplicate_stock_refuse_publication():
    with pytest.raises(ValueError, match="nonselected"):
        render_report(report(candidates=[candidate(status="excluded")]))
    with pytest.raises(ValueError, match="Duplicate"):
        render_report(report(candidates=[candidate(), candidate()]))


def test_mock_conflict_cannot_publish_ai_upgrade_over_program_severe_sell():
    row = candidate()
    row["program"]["lhb"] = {"status": "complete", "action": "exclude", "events": []}
    row["ai"]["rationale"] = "消息积极，图示暂时稳定，模型仍希望保留。"
    with pytest.raises(ValueError, match="program-excluded"):
        render_report(report(candidates=[row]))
    row["final_status"] = "watch"
    with pytest.raises(ValueError, match="program-excluded"):
        render_report(report(candidates=[row]))


def test_mock_missing_funds_cannot_publish_priority_but_missing_chart_alone_can():
    row = candidate()
    # Day-line priority may still be shown with truthful missing visual coverage.
    md, _ = render_report(report(candidates=[row]))
    assert "优先研究" in md and "分时未核验" in md
    row["program"]["moneyflow"]["action"] = "review_required"
    with pytest.raises(ValueError, match="above program ceiling"):
        render_report(report(candidates=[row]))
    row["final_status"] = "watch"
    render_report(report(candidates=[row]))


def test_data_failure_and_deliberate_risk_pause_have_distinct_empty_messages():
    unknown, _ = render_report(report("unknown", []))
    risk, _ = render_report(report("risk_off", []))
    active, _ = render_report(report("active", []))
    assert "数据不足，未形成完整判断" in unknown
    assert "暂停新增优先候选" in risk
    assert "数据不足" not in risk
    assert "当前没有符合本轮条件的候选" in active
    assert "数据不足" not in active


def test_partial_moneyflow_and_failed_lhb_never_become_zero_or_unlisted():
    row = candidate(status="watch")
    row["program"]["moneyflow"]["status"] = "unknown"
    row["program"]["moneyflow"]["windows"]["3"] = {"status": "unknown", "flow_ratio": None}
    row["program"]["moneyflow"]["windows"]["5"] = {"status": "unknown", "flow_ratio": float("nan")}
    row["program"]["lhb"] = {"status": "unknown", "events": [], "action": "review_required"}
    md, _ = render_report(report(candidates=[row]))
    assert "1日 +1.20%" in md
    assert "3日 未知" in md and "5日 未知" in md
    assert "3日 +0.00%" not in md
    assert "龙虎榜来源或统计范围待核查" in md
    assert "完整查询后未上榜" not in md


def test_unverified_lhb_amounts_never_display_as_confirmed_cny():
    row = candidate(status="watch")
    row["program"]["lhb"] = {
        "status": "unknown",
        "events": [
            {
                "report_date": "2026-09-30",
                "event_id": "unknown",
                "trusted_semantics": False,
                "net_cny": -40_000_000,
                "net_ratio": -0.08,
            }
        ],
    }
    md, _ = render_report(report(candidates=[row]))
    assert "单位、统计范围或时点待核查" in md
    assert "4,000万元" not in md


def test_independent_lhb_events_are_not_net_merged_and_institution_unknown_is_not_zero():
    row = candidate(status="watch")
    row["program"]["lhb"] = {
        "status": "complete",
        "events": [
            {
                "report_date": "2026-09-30",
                "event_id": "daily",
                "trusted_semantics": True,
                "scope_type": "single_day",
                "net_cny": -40_000_000,
                "net_ratio": -0.08,
                "institution_status": "unknown",
                "historical_evidence": True,
            },
            {
                "report_date": "2026-09-30",
                "event_id": "multi",
                "trusted_semantics": True,
                "scope_type": "multi_day",
                "scope_start": "2026-09-28",
                "scope_end": "2026-09-30",
                "net_cny": 60_000_000,
                "net_ratio": 0.06,
                "institution_status": "none_disclosed",
            },
        ],
    }
    md, _ = render_report(report(candidates=[row]))
    assert "-4,000万元" in md and "+6,000万元" in md
    assert "+2,000万元" not in md
    assert "机构明细待核查" in md
    assert "榜内未披露机构席位" in md
    assert "历史证据，本次不滚动延长限制" in md


def test_text_page_or_image_id_without_actual_dates_cannot_count_as_reviewed_chart():
    row = candidate()
    row["ai"]["intraday"]["quality"] = "good"
    row["chart"] = {"chart_status": "complete", "visible_dates": [], "image_ids": ["placeholder"]}
    md, _ = render_report(report(candidates=[row]))
    assert "分时未核验" in md
    assert "图示较好" not in md
    row["chart"] = {
        "chart_status": "partial",
        "visible_dates": ["2026-09-30"],
        "image_ids": ["synthetic-image"],
    }
    md, _ = render_report(report(candidates=[row]))
    assert "2026-09-30；图示较好" in md
    assert "仅部分核验，不概括最近几日整体承接" in md
    assert "历史图像不代表目标日盘中条件已触发" in md


def test_recovery_dates_invalid_interval_and_open_condition_do_not_claim_fill():
    row = candidate(status="watch")
    row["route"] = "pullback_recovery"
    row["program"]["setup"] = {"p_date": "2026-09-25", "pullback_amount_ratio": 0.6}
    row["program"]["entry"].update(status="invalid", entry_high=9.9, entry_check="fail")
    md, _ = render_report(report(candidates=[row]))
    assert "上涨高点 2026-09-25" in md
    assert "回调段成交额/上涨末段 0.60倍" in md
    assert "未形成有效冻结参考区间" in md
    assert "开盘价格条件未通过" in md
    row["program"]["entry"].update(status="valid", entry_high=10.25, entry_check="pass")
    md, _ = render_report(report(candidates=[row]))
    assert "开盘价格条件通过，未推断成交" in md


def test_render_does_not_mutate_frozen_report():
    source = report()
    before = deepcopy(source)
    render_report(source)
    assert source == before
