"""Immutable local JSON/Markdown research reports with traceable inputs."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from quantlab.scout.models import fingerprint


def text(value: object) -> str:
    # Treat sourced/generated HTML and Markdown as plain text in report fields.
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ")


def announcement_timing_note(report: dict) -> str | None:
    """Describe index timestamps relative to the market snapshot without backdating them."""
    notices = [
        item
        for item in report.get("evidence", [])
        if item.get("kind") == "official_announcement_index_unverified"
    ]
    if not notices:
        return None
    session = report["market"]["session"]
    later = sum(any(day > session for day in item.get("event_dates", [])) for item in notices)
    unknown_time = sum(not item.get("published_at") for item in notices)
    body_urls = {
        item.get("url")
        for item in report.get("evidence", [])
        if item.get("kind") == "official_pdf_text_unverified"
    }
    read_count = sum(item.get("url") in body_urls for item in notices)
    return (
        f"时点边界：{len(notices)}条正式公告索引中，{later}条公告日期晚于行情日{session}，"
        f"{unknown_time}条精确发布时间未知。这些线索不能证明在{session}收盘时已知；"
        "模型分级不得用于该收盘时点的回测评价。"
        f"其中{read_count}条PDF正文在模型分级前机器提取；其余索引仅含标题与链接。"
        "机器提取未经过人工核实，且实际进入模型的证据须以输入审计为准；使用候选前须核对原文风险。"
    )


def execution_observation(report: dict) -> tuple[int, str] | None:
    """Summarize observed limit prices without inferring a fill or next-session access."""
    selected = report["selection"]["selected"]
    if not selected:
        return None
    candidates = {item["instrument_id"]: item for item in report["candidates"]}
    metrics = [candidates[item["instrument_id"]]["metrics"] for item in selected]
    at_limit = sum(
        item.get("up_limit") is not None and abs(item["close"] - item["up_limit"]) < 1e-8
        for item in metrics
    )
    one_price = sum(item["one_price_session"] for item in metrics)
    unknown = sum(item.get("up_limit") is None for item in metrics)
    note = (
        f"可交易性观察：{len(selected)}只模型观察股票中，{at_limit}只当日收盘价等于涨停价；"
        f"{one_price}只出现一价行情（两项可重叠）。"
    )
    if unknown:
        note += f"另有{unknown}只涨停价未知。"
    note += "这仅是当日行情描述；次日开盘价和盘口未知，不能推断可按该收盘价成交。"
    return at_limit, note


def present_selection(model_selection: dict, candidates: list[dict]) -> dict:
    """Keep model priority/citations, but never publish its unverified prose as fact."""
    by_code = {row["instrument_id"]: row for row in candidates}
    selected = []
    for row in model_selection["selected"]:
        candidate = by_code.get(row["instrument_id"])
        if candidate is None:
            # The ordinary scope validator will reject the unknown code.
            candidate = {}
        has_disclosure = any(
            key in candidate.get("context", {}) for key in ("top_list", "top_inst")
        )
        selected.append(
            {
                "instrument_id": row["instrument_id"],
                "status": row["status"],
                "thesis": (
                    "模型列为优先核查；下方量价和披露事实由程序根据本轮输入列示。"
                    if row["status"] == "focus"
                    else "模型列为一般观察；下方量价和披露事实由程序根据本轮输入列示。"
                ),
                "risk": (
                    "交易披露仅覆盖上榜样本，统计窗口可能重叠；发布时间可能未知。"
                    if has_disclosure
                    else "本轮没有该股交易披露样本；这不表示不存在反向信息。"
                )
                + "未采集订单簿或次日可执行价格，不能推断实际成交。",
                "invalidation": "需用后续已完成会话重新观察量价和披露；尚未验证收益或可交易阈值。",
                "evidence_ids": row["evidence_ids"],
            }
        )
    return {
        "market_view": (
            "模型仅给出候选研究优先级。以下事实由程序从已采集行情和披露生成；"
            "原始模型论述单独封存，未通过人工事实审查前不作为报告依据。"
        ),
        "selected": selected,
    }


def observed_facts(candidate: dict, disclosure_context: dict) -> list[str]:
    """Build a compact fact card without inferring fills or investor intent."""
    m = candidate["metrics"]
    price = (
        f"一日涨跌幅 {m['return_1d']:.2%}、五日 {m['return_5d']:.2%}、"
        f"二十日 {m['return_20d']:.2%}；成交额 {m['amount_cny'] / 1e8:.2f} 亿元，"
        f"相对前五日均值 {m['amount_ratio_5d']:.2f} 倍；"
        f"相对前二十日最高价的突破指标 {m['breakout_20d']:.2%}。"
    )
    if m["one_price_session"]:
        shape = "当日开高低收为同一价格；这不显示委托队列或整日封单。"
    else:
        location = m["close_location"]
        display = f"{location:.2f}" if location is not None else "未知"
        shape = f"当日不是一价行情，收盘位置指标 {display}。"
    if m.get("up_limit") is not None:
        shape += (
            "收盘价等于当日涨停价。"
            if abs(m["close"] - m["up_limit"]) < 1e-8
            else "收盘价不等于当日涨停价。"
        )
    facts = [price, shape]
    records = disclosure_context.get("top_list", {}).get("records", [])
    if records:
        for record in records[:4]:
            net = record.get("net_amount")
            direction = (
                "正"
                if net is not None and net > 0
                else "负"
                if net is not None and net < 0
                else "未知"
            )
            facts.append(
                f"龙虎榜统计：{record.get('window_label', '统计窗口未知')}；"
                f"披露净额方向为{direction}，仅代表该上榜记录。"
            )
    else:
        facts.append("本轮没有该股龙虎榜统计记录；不能据此推断没有反向信息。")
    return facts


def announcement_index_lines(evidence: list[dict], code: str) -> list[str]:
    notices = [
        item
        for item in evidence
        if item.get("kind") == "official_announcement_index_unverified"
        and code in item.get("instrument_ids", [])
    ]
    notices.sort(key=lambda item: (item.get("event_dates") or [""])[0], reverse=True)
    body_urls = {
        item.get("url")
        for item in evidence
        if item.get("kind") == "official_pdf_text_unverified"
        and code in item.get("instrument_ids", [])
    }
    lines = []
    for item in notices[:5]:
        event = (item.get("event_dates") or ["未知"])[0]
        url = item.get("url")
        link = f"[原文]({url.replace(')', '%29')})" if url else "原文链接未知"
        after_selection = (
            "；模型分级后补查" if item.get("source", "").endswith("post_selection") else ""
        )
        body_status = "机器提取正文、未人工核实" if url in body_urls else "正文未读取"
        lines.append(
            f"- {text(item['title'])}（公告日期 {text(event)}；精确发布时间"
            f"{text(item.get('published_at') or '未知')}；{body_status}{after_selection}） {link}"
        )
    if len(notices) > 5:
        lines.append(f"- 另有 {len(notices) - 5} 条索引记录未在候选卡展开。")
    if not lines:
        lines.append("本轮未检出该股正式公告索引；这不表示没有公告。")
    if any("停牌" in item["title"] for item in notices):
        lines.insert(
            0,
            "**停牌风险线索：公告标题包含“停牌”。须核对原文和生效日期，"
            "不能据此报告假定次日可交易。**",
        )
    return lines


def theme_board_items(evidence: list[dict], code: str) -> list[dict]:
    """Return only provider-labeled, unverified themes for this stock."""
    return [
        item
        for item in evidence
        if item.get("kind") == "theme_board_unverified" and code in item.get("instrument_ids", [])
    ]


def screen_notice_risks(selection: dict, evidence: list[dict]) -> dict:
    """Keep research priority and flag title-level trading-status uncertainty."""
    selected = []
    for row in selection["selected"]:
        code = row["instrument_id"]
        suspension_titles = [
            item["title"]
            for item in evidence
            if item.get("kind") == "official_announcement_index_unverified"
            and code in item.get("instrument_ids", [])
            and "停牌" in item.get("title", "")
        ]
        if suspension_titles:
            row = {
                **row,
                "screening_status": "hold_for_official_notice_review",
                "screening_reason": "官方公告索引标题含停牌；研究分级保留，交易状态待核实",
            }
        selected.append(row)
    return {**selection, "selected": selected}


def hold_candidate_pool(pool: list[dict], selection: dict) -> list[dict]:
    held_codes = {
        row["instrument_id"]
        for row in selection["selected"]
        if row.get("screening_status") == "hold_for_official_notice_review"
    }
    return [
        {
            **candidate,
            "screening_status": "hold_for_official_notice_review",
            "cautions": candidate["cautions"]
            + ["官方公告索引标题含停牌；研究分级保留，交易状态待核实"],
        }
        if candidate["instrument_id"] in held_codes
        else candidate
        for candidate in pool
    ]


def render_report(report: dict) -> str:
    status = report["status"]
    title = "【合成演示，不是真实荐股】" if status == "demo" else ""
    lines = [
        f"# {title}短线研究观察报告",
        "",
        f"状态：**{status}** · 行情日期：{report['market']['session']}",
        f"生成时间：{report['finished_at']}",
        (
            f"AI：{text(report['ai_provider'])} / {text(report['ai_model'])}"
            if report.get("ai_provider")
            else "AI：未调用"
        ),
        "",
        "本报告不代表已验证策略或可执行买卖建议。",
        "",
        text(report["selection"]["market_view"]),
        "",
    ]
    timing_note = announcement_timing_note(report)
    if timing_note:
        lines.extend([f"**{text(timing_note)}**", ""])
    execution = execution_observation(report)
    if execution:
        lines.extend([f"**{text(execution[1])}**", ""])
    lines.extend(["## AI候选（模型分级，事实由程序列示）", ""])
    held = [
        row
        for row in report["selection"]["selected"]
        if row.get("screening_status") == "hold_for_official_notice_review"
    ]
    if held:
        lines.extend(
            [
                f"**交易状态提醒：{len(held)}只模型候选的官方公告索引标题含“停牌”，"
                "研究分级照常显示；交易状态及生效日期待核实，不能据此视作可成交名单。**",
                "",
            ]
        )
    for row in report["selection"]["selected"]:
        code = row["instrument_id"]
        candidate = next(x for x in report["candidates"] if x["instrument_id"] == code)
        notice_lines = announcement_index_lines(report.get("evidence", []), code)
        themes = theme_board_items(report.get("evidence", []), code)
        model_label = "优先核查" if row["status"] == "focus" else "一般观察"
        display_label = model_label
        if row.get("screening_status") == "hold_for_official_notice_review":
            display_label += " · 停牌线索（交易状态待核查）"
        lines.extend(
            [
                f"### {text(candidate['name'])} {code} · {display_label}",
                "",
                f"分级说明：{text(row['thesis'])}",
                "",
                "可核查事实："
                + " ".join(
                    text(x)
                    for x in observed_facts(
                        candidate, report.get("disclosure_context", {}).get(code, {})
                    )
                ),
                "",
                *(
                    [
                        "第三方涨停题材标签（未核实，非公司公告）：",
                        *(f"- {text(item['title'])}；{text(item['body'])}" for item in themes),
                        "",
                    ]
                    if themes
                    else []
                ),
                "正式公告索引（仅标题和链接，未核实正文）：",
                *notice_lines,
                "",
                f"证据边界：{text(row['risk'])}",
                "",
                f"失效观察：{text(row['invalidation'])}",
                "",
                f"证据：{', '.join(row['evidence_ids'])}",
                "",
            ]
        )
    if not report["selection"]["selected"]:
        lines.extend(["没有AI重点候选。以下是待调查的规则候选，不能当作AI推荐。", ""])
    lines.extend(
        [
            "## 规则候选与多路候选池",
            "",
            "规则分数只用于可复现排序，不是涨停概率。",
            "",
            "| 股票 | 入口 | 1日涨幅 | 5日涨幅 | 成交额(亿元) | 额比(5日) | 注意事项 |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for candidate in report["candidates"]:
        m = candidate["metrics"]
        lines.append(
            f"| {text(candidate['name'])} {candidate['instrument_id']} "
            f"| {text('、'.join(candidate['routes']))} | {m['return_1d']:.2%} "
            f"| {m['return_5d']:.2%} | {m['amount_cny'] / 1e8:.2f} "
            f"| {m['amount_ratio_5d']:.2f} | {text('；'.join(candidate['cautions']))} |"
        )
    lines.extend(
        [
            "",
            "## 交易披露与评论线索",
            "",
            "龙虎榜保留各上榜原因和统计窗口，不合计为全部资金；大宗折溢价不自动判为利好利空。",
            "评论是导入样本，不能代表平台总体热度或真实持仓。",
            "",
        ]
    )
    for candidate in report["candidates"]:
        context = candidate.get("context", {})
        if not context:
            continue
        code = candidate["instrument_id"]
        lines.extend([f"### {text(candidate['name'])} {code}", ""])
        for dataset in ("top_list", "top_inst", "block_trade"):
            if dataset in context:
                item = context[dataset]
                label = {
                    "top_list": "龙虎榜统计",
                    "top_inst": "龙虎榜席位明细",
                    "block_trade": "大宗交易",
                }[dataset]
                lines.append(
                    f"- {label}：{item['record_count']}条披露记录；交易日期 "
                    f"{', '.join(item['observed_trade_dates'])}；证据 {item['evidence_id']}。"
                )
                if dataset == "block_trade":
                    records = (
                        report.get("disclosure_context", {})
                        .get(code, {})
                        .get(dataset, {})
                        .get("records", [])
                    )
                    for row in records[:3]:
                        premium = row["premium_to_close_pct"]
                        display = f"{premium:+.2f}%" if premium is not None else "未知"
                        lines.append(
                            f"  样本：{row['trade_date']} 成交价 {row['price']}，"
                            f"成交量 {row['volume_shares']:g}股，相对当日收盘价 {display}。"
                        )
        if "discussion" in context:
            item = context["discussion"]
            lines.append(
                f"- 评论样本：{item['sample_count']}条，{item['unique_text_count']}种文本，"
                f"重复文本比例 {item['repeated_text_fraction']:.1%}；"
                f"采样方式：{text(item['sampling_method'])}。"
            )
            lines.append(
                f"  样本窗口：{item['window_start']} 至 {item['window_end']}；热度增速未知。"
            )
            lines.append(f"  证据：{', '.join(item['evidence_ids'])}。")
        lines.append("")
    comparison = report.get("source_comparison", {})
    if comparison:
        lines.extend(
            [
                "### 候选发现对照",
                "",
                "新增来源加入前的候选池："
                + text("、".join(comparison["pool_without_supplemental_routes"])),
                "",
                "新增来源带来的候选：" + text("、".join(comparison["new_candidate_codes"]) or "无"),
                "",
                "此处只对照候选发现；不是AI消融实验，也不能据此归因选股收益。",
                "",
            ]
        )
    lines.extend(["", "## 信息源覆盖", "", "| 来源 | 状态 | 条数 | 说明 |", "|---|---|---:|---|"])
    for source in report["coverage"]:
        lines.append(
            f"| {text(source['source'])} | {source['status']} "
            f"| {source['count']} | {text(source['detail'])} |"
        )
    lines.extend(["", "## 来源索引", ""])
    for item in report["evidence"]:
        # Autolinks escape bracket injection; Evidence validates the URL scheme.
        url = (item["url"] or "无可核实直链").replace("<", "%3C").replace(">", "%3E")
        lines.extend(
            [
                f"- **{item['evidence_id']}** {text(item['title'])}",
                f"  来源：{text(item['source'])}；发布时间："
                f"{item['published_at'] or '未知，不能证明事件新鲜度'}；"
                f"获取：{item['retrieved_at']}",
                "  事件/交易日期：" + text("、".join(item.get("event_dates", [])) or "未单列"),
                f"  <{url}>" if item["url"] else "  无可核实直链；需人工核对。",
            ]
        )
    lines.extend(["", "## 限制", ""] + [f"- {x}" for x in report["limitations"]])
    lines.extend(["", "完整指标、基线名单、模型用量和原始响应保存在同目录JSON中。", ""])
    return "\n".join(lines)


def write_report(root: Path, report: dict, raw: list[dict]) -> Path:
    from quantlab.scout.html_report import render_html_report

    markdown = render_report(report)
    html = render_html_report(report)
    run_dir = root / report["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    for name, value in (("report.json", report), ("ai_responses.json", raw)):
        (run_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    html_path = run_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "report_sha256": fingerprint(report),
                "ai_responses_sha256": fingerprint(raw),
                "html_sha256": sha256(html_path.read_bytes()).hexdigest(),
            },
            indent=2,
        )
    )
    return run_dir
