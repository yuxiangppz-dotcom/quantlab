"""Immutable local JSON/Markdown research reports with traceable inputs."""

from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from quantlab.scout.models import SHANGHAI, fingerprint


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
    """Preserve validated model reasoning and append program-derived cautions."""
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
        routes = candidate.get("routes", [])
        route_summary = (
            "、".join(
                label
                for label, applies in (
                    ("公告与事件", any("announcement" in route for route in routes)),
                    ("板块", any(route.startswith("板块领先:") for route in routes)),
                    ("回撤或温和放量", any("回撤" in route or "温和" in route for route in routes)),
                    ("量价或趋势", any(route in {"量价异动", "趋势突破"} for route in routes)),
                    ("热榜", "热度观察" in routes),
                )
                if applies
            )
            or "行情完整性核查"
        )
        hot = candidate.get("context", {}).get("hot_rank")
        attention = "含已保存的热榜观察" if hot else "热度变化未由本轮证实"
        m = candidate.get("metrics", {})
        at_limit = (
            m.get("up_limit") is not None
            and m.get("close") is not None
            and abs(m["close"] - m["up_limit"]) < 1e-8
        )
        price_risk = (
            "本轮收盘等于涨停价，后续价格和实际可买性仍未知。"
            if at_limit
            else "后续价格和实际可买性仍未知。"
        )
        if (m.get("return_5d") or 0) > 0.2:
            price_risk = "近期涨幅较大，关注拥挤和回撤。" + price_risk
        relation_risk = (
            "公告索引或题材关联仍需核对原文与发布时间。"
            if any(route.startswith("信息关联:") for route in routes)
            else "本轮未确认新的公司级催化事实。"
        )
        selected.append(
            {
                **row,
                "instrument_id": row["instrument_id"],
                "status": row["status"],
                "thesis": row["thesis"],
                "risk": row["risk"]
                + " 程序补充："
                + (
                    "交易披露仅覆盖上榜样本，统计窗口可能重叠；发布时间可能未知。"
                    if has_disclosure
                    else "本轮没有该股交易披露样本；这不表示不存在反向信息。"
                )
                + price_risk
                + relation_risk,
                "invalidation": row["invalidation"],
                "program_risks": list(
                    dict.fromkeys(
                        [
                            *row.get("program_risks", []),
                            price_risk,
                            "当日为一价行情，委托队列及目标日成交未知。"
                            if m.get("one_price_session")
                            else "",
                            "交易状态存在停牌线索，须独立确认。"
                            if row.get("screening_status") == "hold_for_official_notice_review"
                            else "",
                        ]
                    )
                ),
                "evidence_ids": row["evidence_ids"],
                "opportunity_type": candidate.get("routes", []),
                "discovery_summary": route_summary + "；" + attention,
                "reason_provenance": (
                    "typed_core_numbers_checked_other_model_inference_unverified"
                    if "quant_claims" in row
                    else "legacy_core_prose_checked_without_structured_model_claims"
                ),
                "evidence_quality": (
                    "external_lead_requires_verification"
                    if any(ref.startswith("ev-") for ref in row["evidence_ids"])
                    else "market_observation_only"
                ),
                "price_attention_status": (
                    "recently_extended"
                    if candidate.get("metrics", {}).get("return_5d", 0) > 0.2
                    else "not_derived_as_probable_upside"
                ),
                "observation_horizon_sessions": 5,
            }
        )
    return {
        "market_view": "模型研判，未经人工独立事实核查：" + model_selection["market_view"],
        "selected": selected,
        "no_recommendation_reason": (
            "模型本轮未列重点关注；候选只保留一般观察或证据仍待核实。"
            if not any(row["status"] == "focus" for row in selected)
            else None
        ),
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
    upgrade = candidate.get("source_summary") or candidate.get("context", {}).get(
        "tushare_upgrade", {}
    )
    history = upgrade.get("limit_history") or []
    if history:
        last = history[-1]
        limit_times = last.get("limit_times")
        open_times = last.get("open_times")
        limit_count = limit_times if limit_times is not None else "未知"
        facts.append(
            f"TuShare历史板单截至{last.get('trade_date') or '未知'}："
            f"类型{last.get('limit') or '未知'}，连板数{limit_count}，"
            f"炸/开板次数{open_times if open_times is not None else '未知'}；"
            "这不说明下一交易日委托队列或可成交性。"
        )
    flows = upgrade.get("moneyflow") or {}
    if flows:
        parts = []
        for days in (1, 3, 5):
            value = flows.get(f"net_{days}d_wan_cny")
            covered = flows.get(f"coverage_{days}d", 0)
            parts.append(
                f"{days}日{value:.2f}万元"
                if isinstance(value, (int, float))
                else f"{days}日未知（覆盖{covered}/{days}）"
            )
        facts.append("TuShare供应商资金流净额：" + "、".join(parts) + "；不等于机构账户流向。")
    for item in (upgrade.get("future_unlocks") or [])[:2]:
        ratio = item.get("float_ratio")
        facts.append(
            f"已披露未来解禁：{item.get('float_date') or '日期未知'}；"
            f"占总股本比例{ratio if ratio is not None else '未知'}%；"
            "不等于股东实际卖出。"
        )
    if upgrade.get("trading_status"):
        facts.append("TuShare停复牌状态样本：" + str(upgrade["trading_status"]))
    for item in (upgrade.get("fina_indicator") or [])[:1]:
        facts.append(
            f"财务指标：报告期{item.get('end_date') or '未知'}，公告日"
            f"{item.get('ann_date') or '未知'}；扣非利润"
            f"{item.get('profit_dedt') if item.get('profit_dedt') is not None else '未知'}"
            "（供应商原值，单位待核）；其余字段和口径见原始证据。"
        )
    if upgrade.get("fina_mainbz"):
        facts.append(
            "主营构成已取得，报告期与原币种见原始证据；公开时间未知，"
            "不按不同维度加总，也不据此推断主题盈利。"
        )
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
    from quantlab.scout.concise_report import markdown

    return markdown(report)


def render_audit_report(report: dict) -> str:
    from quantlab.scout.opportunity_view import markdown as opportunity_markdown

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
    timing = report.get("timing")
    if timing:
        lines.extend(
            [
                f"目标观察交易日：{timing['target_session'] or '未知'} · "
                f"报告类型：{timing['report_kind']} · 时区：{timing['timezone']}",
                f"信息截点：{timing['information_cutoff']} · "
                f"行情截至：{timing['asof_session']} · "
                f"可进入盘前主版本跟踪：{'是' if timing['primary_eligible'] else '否'}",
                "夜间准备报告保留真实生成时间；若在目标开盘后完成，不算该日盘前预测。",
                "",
            ]
        )
    timing_note = announcement_timing_note(report)
    if timing_note:
        lines.extend([f"**{text(timing_note)}**", ""])
    execution = execution_observation(report)
    if execution:
        lines.extend([f"**{text(execution[1])}**", ""])
    lines.extend(opportunity_markdown(report))
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
                (
                    f"事实边界：{len(row.get('quant_claims', []))}条核心量化断言与"
                    "模型实际输入的程序事实逐项核对；其余机会判断仍为模型推断。"
                    if row.get("reason_provenance")
                    == "typed_core_numbers_checked_other_model_inference_unverified"
                    else "事实边界：旧模型输出缺结构化量价声明；本条仅作兼容性核查，"
                    "不能追认模型当时曾提交新版事实结构。"
                ),
                f"机会入口：{text('、'.join(row.get('opportunity_type', candidate['routes'])))}；"
                f"证据等级：{text(row.get('evidence_quality', '未知'))}；"
                f"观察期限：{row.get('observation_horizon_sessions', 5)}个交易日。",
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
                "正式公告索引与正文提取状态（机器提取未人工核实）：",
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
    if not any(row["status"] == "focus" for row in report["selection"]["selected"]):
        lines.extend(
            [
                "今天没有足够依据推荐新的重点关注标的。",
                text(
                    report["selection"].get("no_recommendation_reason")
                    or "模型未给出重点；具体原因尚未结构化核实。"
                ),
                "",
            ]
        )
    lines.extend(
        [
            "## 多入口深查候选池",
            "",
            "发现阶段的排序分数不是上涨概率，也不是 AI 与规则的收益对照。",
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
    stages = report.get("candidate_stages", {})
    if stages:
        lines.extend(
            [
                "",
                f"廉价召回 {len(stages['cheap_candidates'])} 只；"
                f"深查 {len(stages['deep_candidates'])} 只；"
                f"因深查预算未进入 {len(stages['not_deep_reason'])} 只。",
                "逐只阶段与原因见 report.json 的 candidate_stages。",
            ]
        )
    diagnostics = report.get("candidate_diagnostics", {})
    if diagnostics:
        lines.extend(
            [
                "",
                "## 候选阶段诊断",
                "",
                "前日收盘涨停按该证券已知涨停价核对；未知值不计作未涨停。",
                "公司事实覆盖只计算已取得的事件记录或公告正文，不等于人工核实通过。",
                "",
                "| 阶段 | 股票数 | 已知收盘涨停 | 涨停价未知 | 近5日涨幅>20% | 公司事件/正文覆盖 |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for stage in ("eligible", "cheap", "deep", "focus", "watch"):
            values = diagnostics.get(stage, {})
            lines.append(
                f"| {stage} | {values.get('count', 0)} | "
                f"{values.get('prior_close_at_known_up_limit', 0)} | "
                f"{values.get('up_limit_unknown', 0)} | "
                f"{values.get('return_5d_bins', {}).get('above_20pct', 0)} | "
                f"{values.get('company_fact_or_pdf_coverage', 0)} |"
            )
        lines.extend(
            [
                "",
                "各路径原始/独有/重叠/分配/截断数、行业集中和完整涨幅分布见 "
                "report.json 的 candidate_stages 与 candidate_diagnostics。",
                "",
            ]
        )
    portfolio = report.get("portfolio_review", {})
    lines.extend(["", "## 持仓与自选独立观察", ""])
    if portfolio.get("status") != "provided":
        lines.extend(["本轮未提供持仓或自选清单。", ""])
    else:
        lines.extend(
            [
                f"清单观察时间：{text(portfolio['observed_at'])}；"
                f"输入超过一周：{'是' if portfolio['stale_input'] else '否'}。",
                "此处只显示已知行情与风险，不占新候选名额，未单独调用 AI 深查。",
                "",
                "| 类别 | 股票 | 行情日收盘 | 新候选合格 | 已知缺口 |",
                "|---|---|---:|---|---|",
            ]
        )
        for item in portfolio["rows"]:
            close = f"{item['close']:.2f}" if item["close"] is not None else "未知"
            lines.append(
                f"| {text(item['group'])} | {text(item['name'] or '身份未知')} "
                f"{item['instrument_id']} | {close} | "
                f"{'是' if item['new_candidate_eligible'] else '否'} | "
                f"{text('、'.join(item['concerns']) or '暂无已知警示')} |"
            )
        lines.append("")
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
    lines.extend(["", "完整指标、模型用量和原始响应保存在同目录JSON中。", ""])
    return "\n".join(lines)


def write_report(root: Path, report: dict, raw: list[dict]) -> Path:
    from quantlab.scout.html_report import render_audit_html_report, render_html_report

    markdown = render_report(report)
    html = render_html_report(report)
    run_dir = root / report["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    for name, value in (("report.json", report), ("ai_responses.json", raw)):
        (run_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "audit_report.md").write_text(render_audit_report(report), encoding="utf-8")
    audit_html = render_audit_html_report(report)
    (run_dir / "audit_report.html").write_text(audit_html, encoding="utf-8")
    html_path = run_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "report_sha256": fingerprint(report),
                "ai_responses_sha256": fingerprint(raw),
                "html_sha256": sha256(html_path.read_bytes()).hexdigest(),
                "audit_html_sha256": sha256(audit_html.encode()).hexdigest(),
            },
            indent=2,
        )
    )
    if report.get("nextday_freeze"):
        (run_dir / "publication.json").write_text(
            json.dumps(
                {
                    "run_id": report["run_id"],
                    "source_report_sha256": fingerprint(report),
                    "status": "local_saved",
                    "published_at": datetime.now(SHANGHAI).isoformat(),
                    "medium": "local_saved_report_only",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return run_dir
