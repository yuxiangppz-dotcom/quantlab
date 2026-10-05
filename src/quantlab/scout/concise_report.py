"""Selected stocks and their reasons; complete evidence remains in audit artifacts."""

from __future__ import annotations

import re
from html import escape

from quantlab.scout.models import web_url
from quantlab.scout.review import validate_post_run_review


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

    # The comparison retains the original model risk without verbose audit cautions.
    value = row.get("comparison", {}).get(field, row.get(field)) or "待核查"
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
        "",
        "研究候选，效果待前瞻观察。",
        "",
    ]
    if report.get("synthetic") or report["status"] == "demo":
        lines.extend(["**合成演示，不是真实预测。**", ""])
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
                    "选择理由：" + safe(reason_text(row, "thesis")),
                    "",
                    "主要风险：" + safe(reason_text(row, "risk")),
                    "",
                    "失效条件：" + safe(reason_text(row, "invalidation")),
                    "",
                ]
            )
            if row.get("screening_status") == "hold_for_official_notice_review":
                lines.extend(["交易状态：停牌线索待核查。", ""])
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
        f"<p>生成时间 {h(report['finished_at'])}</p><p>研究候选，效果待前瞻观察。</p></header>",
    ]
    if report.get("synthetic") or report["status"] == "demo":
        parts.append("<p><strong>合成演示，不是真实预测。</strong></p>")
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
                    f"<p><strong>选择理由：</strong>{h(reason_text(row, 'thesis'))}</p>",
                    f"<p class='risk'><strong>主要风险：</strong>{h(reason_text(row, 'risk'))}</p>",
                    f"<p><strong>失效条件：</strong>{h(reason_text(row, 'invalidation'))}</p>",
                ]
            )
            if row.get("screening_status") == "hold_for_official_notice_review":
                parts.append("<p class='risk'>交易状态：停牌线索待核查。</p>")
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
