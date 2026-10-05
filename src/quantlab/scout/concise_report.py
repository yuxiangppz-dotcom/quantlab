"""Selected stocks and their reasons; complete evidence remains in audit artifacts."""

from __future__ import annotations

import re
from html import escape

from quantlab.scout.models import web_url
from quantlab.scout.review import validate_post_run_review

VIEW_VERSION = "research_cards_v2"


def source_gap_note(report):
    gaps = sorted(
        {
            str(c.get("source", "未知来源"))
            for c in report.get("coverage", [])
            if c.get("status")
            in {"failed", "unavailable", "not_configured", "unsupported", "error"}
            and any(
                k in str(c.get("source", "")).lower()
                for k in ("news", "hot", "comment", "新闻", "热榜", "评论")
            )
        }
    )
    return "主要来源缺口：" + (
        "、".join(gaps) if gaps else "未记录新闻/热榜/评论缺口；不代表全量覆盖"
    )


def card_content(report, row):
    """One six-item content contract for HTML, Markdown and the WeChat body."""
    from quantlab.scout.decision_contract import rule_text
    from quantlab.scout.opportunities import TYPE_LABELS

    comparison = row.get("comparison", row)
    candidate = next(c for c in report["candidates"] if c["instrument_id"] == row["instrument_id"])
    analysis = comparison.get("analysis", {})
    risks = list(filter(None, row.get("program_risks", [])))
    m = candidate.get("metrics", {})
    if m.get("return_5d") is not None and m["return_5d"] > 0.2:
        if not any("涨幅较大" in r for r in risks):
            risks.append("近5日累计涨幅超过20%（程序事实），关注拥挤和回撤。")
    if (
        m.get("up_limit") is not None
        and m.get("close") is not None
        and abs(m["up_limit"] - m["close"]) < 1e-8
    ):
        if not any("收盘等于涨停价" in r for r in risks):
            risks.append("行情日收盘等于涨停价（程序事实），目标日价格及实际可买性未知。")
    if m.get("one_price_session"):
        risks.append("行情日为一价（程序事实），不代表已知委托队列。")
    if row.get("screening_status") == "hold_for_official_notice_review":
        risks.append("交易状态：停牌线索待核查。")
    conditions = comparison.get("trade_conditions", {})
    mechanism = analysis.get("h5_mechanism") or "原版未定义，后续机制待观察"
    peer = comparison.get("comparator_id")
    difference = reason_text(row, "difference")
    if not peer:
        difference = "无可核查同类比较对象；" + difference
    note = row.get("invalidation_observation") or comparison.get("invalidation_observation")
    if not note:
        note = rule_text(comparison.get("invalidation_rule"))
    return [
        (
            "研究类型与当前状态",
            TYPE_LABELS.get(comparison.get("primary_type"), "研究类型未归档")
            + " / 等待条件确认；"
            + ("重点仅表示研究优先级" if row["status"] == "focus" else "一般观察"),
        ),
        (
            "关注依据",
            "模型判断：" + reason_text(row, "thesis") + "；后续假设（未验证）：" + mechanism,
        ),
        ("为什么优先于同类", ("比较对象 " + peer + "；" if peer else "") + difference),
        (
            "为什么此时可能不适合参与",
            "模型最强反证："
            + reason_text({**row, "program_risks": []}, "risk")
            + "；程序风险："
            + " ".join(dict.fromkeys(risks)),
        ),
        (
            "需要确认什么",
            "已知："
            + (conditions.get("known") or "未归档")
            + "；尚缺："
            + (conditions.get("unknown") or "可核查的参与观测条件未定义")
            + "；关键未知："
            + (comparison.get("unknowns") or "未知项未归档"),
        ),
        ("假设何时失效", note + "；模型描述（非交易退出）：" + reason_text(row, "invalidation")),
    ]


def selected_groups(report):
    rows = report["selection"]["selected"]
    return [
        (
            label,
            sorted(
                [r for r in rows if r["status"] == status],
                key=lambda r: (r.get("rank") or 9999, r["instrument_id"]),
            ),
        )
        for status, label in (("focus", "重点核查"), ("watch", "一般观察"))
    ]


def reason_text(row, field):
    from quantlab.scout.daily_contract import format_fact

    # Program risks are a separate field; the original comparison cannot hide them.
    value = row.get("comparison", {}).get(field, row.get(field)) or "待核查"
    if field == "risk":
        value = " ".join(dict.fromkeys([value, *filter(None, row.get("program_risks", []))]))
    labels = {
        "return_1d": "近1日涨幅",
        "return_5d": "近5日涨幅",
        "return_20d": "近20日涨幅",
        "amount_ratio_5d": "成交额放大倍数",
        "amount_cny": "成交额",
        "breakout_20d": "突破前20日高点幅度",
        "net_1d_wan_cny": "近1日资金净额",
        "net_3d_wan_cny": "近3日资金净额",
        "net_5d_wan_cny": "近5日资金净额",
    }
    for fact in row.get("fact_cards", []):
        if fact["subject_id"] == row["instrument_id"] and fact["metric"] in labels:
            original = format_fact(fact)
            number = original.split("：", 1)[1].replace(" times", "倍")
            value = value.replace(original, labels[fact["metric"]] + " " + number)
    return value


def markdown(report):
    def safe(value):
        return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ")

    timing = report.get("timing") or {}
    candidates = {c["instrument_id"]: c for c in report["candidates"]}
    lines = [
        "# Scout 选股报告",
        "",
        f"目标交易日：{safe(timing.get('target_session') or '未知')} · "
        f"行情截至：{safe(report['market']['session'])}",
        f"生成时间：{safe(report['finished_at'])}",
        f"信息截点：{safe(timing.get('information_cutoff') or timing.get('information_cutoff_at') or report.get('selection_input_packet', {}).get('timing', {}).get('information_cutoff') or '未归档')}",
        safe(source_gap_note(report)),
        "",
        "研究候选，效果待前瞻观察。",
        "",
    ]
    if report.get("synthetic") or report["status"] == "demo":
        lines.extend(["**合成演示，不是真实预测。**", ""])
    if report.get("display_replay"):
        lines.extend(
            [
                "**保存证据离线展示副本，不是新预测；原报告 "
                + safe(report["display_replay"]["source_run_id"])
                + " · 展示 "
                + VIEW_VERSION
                + "。**",
                "",
            ]
        )
    for label, rows in selected_groups(report):
        if not rows:
            continue
        lines.extend(["## " + label, ""])
        for row in rows:
            code = row["instrument_id"]
            lines.extend(
                [
                    f"### {safe(candidates[code]['name'])}（{safe(code)}）",
                    "",
                ]
            )
            for label, value in card_content(report, row):
                lines.extend([label + "：" + safe(value), ""])
    if not report["selection"]["selected"]:
        lines.extend(
            [
                "本次未通过校验，没有可用候选。"
                if report["status"] == "incomplete"
                else "本次未选出候选。",
                "",
            ]
        )
    return "\n".join(lines)


def html(report, review=None):
    if review is not None:
        validate_post_run_review(report, review)

    def h(value):
        return escape(str(value), quote=True)

    timing = report.get("timing") or {}
    candidates = {c["instrument_id"]: c for c in report["candidates"]}
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; script-src 'none'; base-uri 'none'; form-action 'none'\">",
        "<title>Scout · 选股报告</title><style>",
        "*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#17212b;"
        "font:16px/1.7 system-ui,sans-serif}main{max-width:900px;margin:auto;"
        "padding:24px 16px 48px}header{background:#172b3b;color:white;padding:24px;"
        "border-radius:16px}h1{margin:0}header p{margin:7px 0}h2{margin:28px 0 12px}"
        ".card{background:white;border:1px solid #dce3e8;border-radius:14px;"
        "padding:20px 24px;margin:14px 0}.card h3{margin:0 0 12px;font-size:21px}"
        ".code{color:#607180;font-size:14px;margin-left:10px}.card p{margin:10px 0;"
        "overflow-wrap:anywhere}.risk{color:#79552f}a{color:#155b85}"
        "@media(max-width:600px){main{padding:12px}header,.card{padding:18px}}"
        "@media print{.card{break-inside:avoid}}",
        "</style></head><body><main><header><h1>Scout 选股报告</h1>",
        f"<p>目标交易日 {h(timing.get('target_session') or '未知')} · "
        f"行情截至 {h(report['market']['session'])}</p>",
        f"<p>生成时间 {h(report['finished_at'])}</p>"
        f"<p>信息截点 {h(timing.get('information_cutoff') or timing.get('information_cutoff_at') or '未归档')}</p>"
        f"<p>{h(source_gap_note(report))}</p><p>研究候选，效果待前瞻观察。</p></header>",
    ]
    if report.get("synthetic") or report["status"] == "demo":
        parts.append("<p><strong>合成演示，不是真实预测。</strong></p>")
    if report.get("display_replay"):
        parts.append(
            "<p>保存证据离线展示副本，不是新预测；原报告 "
            + h(report["display_replay"]["source_run_id"])
            + " · 展示 "
            + VIEW_VERSION
            + "。</p>"
        )
    for label, rows in selected_groups(report):
        if not rows:
            continue
        parts.append(f"<section><h2>{label}</h2>")
        for row in rows:
            code = row["instrument_id"]
            parts.extend(
                [
                    f"<article class='card' id='stock-{h(code)}'>"
                    f"<h3>{h(candidates[code]['name'])}"
                    f"<span class='code'>{h(code)}</span></h3>",
                ]
            )
            for label, value in card_content(report, row):
                parts.append(f"<p><strong>{h(label)}：</strong>{h(value)}</p>")
            if row.get("fact_cards"):
                from quantlab.scout.daily_contract import format_fact

                parts.append(
                    "<details><summary>程序可核实事实（不证明未来收益）</summary><ul>"
                    + "".join("<li>" + h(format_fact(f)) + "</li>" for f in row["fact_cards"])
                    + "</ul></details>"
                )
            for finding in (review or {}).get("findings", []):
                if finding["instrument_id"] == code:
                    parts.append(
                        "<p class='risk'>报告后核查备注（未参与模型分级）："
                        f"{h(finding['summary'])}</p>"
                    )
                    url = web_url(finding["source_url"])
                    if url:
                        parts.append(
                            f"<a href='{h(url)}' target='_blank' "
                            "rel='noopener noreferrer'>查看核查原文</a>"
                        )
            parts.append("</article>")
        parts.append("</section>")
    if not report["selection"]["selected"]:
        parts.append(
            "<p>本次未通过校验，没有可用候选。</p>"
            if report["status"] == "incomplete"
            else "<p>本次未选出候选。</p>"
        )
    parts.append("</main></body></html>")
    return "".join(parts)
