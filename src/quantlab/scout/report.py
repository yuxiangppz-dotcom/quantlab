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
