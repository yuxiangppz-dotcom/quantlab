"""Immutable local JSON/Markdown research reports with traceable inputs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from quantlab.scout.models import fingerprint


def text(value: object) -> str:
    # Treat sourced/generated HTML and Markdown as plain text in report fields.
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ")


def render_report(report: dict) -> str:
    status = report["status"]
    title = "【合成演示，不是真实荐股】" if status == "demo" else ""
    lines = [
        f"# {title}短线研究观察报告",
        "",
        f"状态：**{status}** · 行情日期：{report['market']['session']}",
        f"生成时间：{report['finished_at']}",
        "",
        "本报告不代表已验证策略或可执行买卖建议。",
        "",
        text(report["selection"]["market_view"]),
        "",
        "## AI候选",
        "",
    ]
    for row in report["selection"]["selected"]:
        code = row["instrument_id"]
        candidate = next(x for x in report["candidates"] if x["instrument_id"] == code)
        lines.extend(
            [
                f"### {text(candidate['name'])} {code} · {row['status']}",
                "",
                f"依据：{text(row['thesis'])}",
                "",
                f"风险：{text(row['risk'])}",
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
    run_dir = root / report["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    for name, value in (("report.json", report), ("ai_responses.json", raw)):
        (run_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
    (run_dir / "report.md").write_text(render_report(report), encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "report_sha256": fingerprint(report),
                "ai_responses_sha256": fingerprint(raw),
            },
            indent=2,
        )
    )
    return run_dir
