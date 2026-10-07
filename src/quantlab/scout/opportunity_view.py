"""Readable opportunity comparisons in the existing report, with escaped text."""

import html
import json
from collections import Counter

from quantlab.scout.opportunities import TYPE_LABELS


def concentration(report: dict) -> dict:
    selected = report["selection"]["selected"]
    candidates = {r["instrument_id"]: r for r in report["candidates"]}
    themes, industries = Counter(), Counter()
    for row in selected:
        code = row["instrument_id"]
        industries[report.get("industry_memberships", {}).get(code, "分类未知")] += 1
        values = candidates[code].get("source_summary", {}).get("themes", [])
        for theme in set(
            json.dumps(v, ensure_ascii=False, sort_keys=True) if isinstance(v, dict) else str(v)
            for v in values
        ):
            themes[theme] += 1
    return {
        "industry_counts": dict(industries),
        "shared_provider_themes": {t: n for t, n in themes.items() if n > 1},
    }


def lines(report: dict) -> list[str]:
    opportunity = report.get("opportunity")
    if opportunity is None:
        return []
    rows = opportunity.get("comparisons", [])
    counts = Counter(r.get("primary_type", "insufficient_evidence") for r in rows)
    nextday = report.get("prediction_objective") == "next_session" or any(
        "next_session_thesis" in r.get("analysis", {}) for r in rows
    )
    output = [
        "机会比较版本：opportunity_v1；主观察期限 " + ("D1。" if nextday else "H5。"),
        "完整比较状态："
        + opportunity["validation"]["status"]
        + "；"
        + "、".join(opportunity["validation"]["errors"]),
        "研究类型：" + "；".join(f"{TYPE_LABELS[k]} {n}只" for k, n in counts.items()),
        "共同分类/题材集中：" + json.dumps(concentration(report), ensure_ascii=False),
        "同主题入选股的新增价值见各股独立依据；题材标签不等于业务受益。尚未验证收益。",
    ]
    for selected in report["selection"]["selected"]:
        row = selected.get("comparison")
        if row is None:
            continue
        a = row["analysis"]
        output.extend(
            [
                f"{row['instrument_id']} · {TYPE_LABELS[row['primary_type']]} · "
                f"比较名次 {row['rank']}",
                f"相对取舍（比较对象 {row['comparator_id'] or '缺少同类未选者'}）："
                f"{row['difference']}",
                f"新增变化：{a['incremental_change']}；经济关系：{a['economic_link']}；"
                f"重要程度：{a['importance']}",
                f"{'次日' if nextday else 'H5'}机制："
                f"{a.get('next_session_thesis') if nextday else a.get('h5_mechanism')}"
                "；下一观察节点："
                f"{a['next_observation_date'] or '日期未知'}，{a['next_node_basis']}",
                f"独立依据：{row['independent_basis']}；关键未知：{row['unknowns']}",
                f"最强反证：{row['risk']}；失效条件：{row['invalidation']}",
                f"证据可靠性：{row['evidence_reliability']}；机会比较："
                f"{row['comparison_strength']}（{row['difference']}）",
                f"参与条件：已知 {row['trade_conditions']['known']}；"
                f"未知 {row['trade_conditions']['unknown']}",
            ]
        )
    return output


def markdown(report: dict) -> list[str]:
    if not report.get("opportunity"):
        return []
    output = [
        "## 机会与相对取舍",
        "",
        *[line + "\n" for line in lines(report)],
        "### 全部深查股比较（内部完整排序）",
        "",
        "| 股票 | 主要类型 | 名次 | 最终状态 | 支持 | 最强反证 | 优先级差异 |",
        "|---|---|---:|---|---|---|---|",
    ]
    for row in report["opportunity"].get("comparisons", []):
        values = [
            row["instrument_id"],
            TYPE_LABELS.get(row.get("primary_type"), "未知"),
            str(row.get("rank") or "证据不足"),
            row.get("final_status", "不完整"),
            row.get("thesis", "未知"),
            row.get("risk", "未知"),
            row.get("difference", "未知"),
        ]
        output.append(
            "| " + " | ".join(v.replace("|", "\\|").replace("\n", " ") for v in values) + " |"
        )
    return output + [""]


def render_html(report: dict) -> str:
    if not report.get("opportunity"):
        return ""
    pieces = ["<section class='panel'><h2>机会与相对取舍</h2>"]
    pieces.extend(f"<p>{html.escape(line)}</p>" for line in lines(report))
    pieces.append(
        "<details><summary>展开全部深查股比较（内部完整排序）</summary><table><thead><tr>"
        "<th>股票</th><th>主要类型 / 名次 / 状态</th><th>支持</th>"
        "<th>最强反证</th><th>具体差异</th></tr></thead><tbody>"
    )
    for row in report["opportunity"].get("comparisons", []):
        values = [
            row.get("instrument_id", "未知"),
            f"{TYPE_LABELS.get(row.get('primary_type'), '未知')} / "
            f"{row.get('rank') or '不可排序'} / {row.get('final_status', '不完整')}",
            row.get("thesis", "未知"),
            row.get("risk", "未知"),
            row.get("difference", "未知"),
        ]
        pieces.append("<tr>" + "".join(f"<td>{html.escape(v)}</td>" for v in values) + "</tr>")
    return "".join(pieces) + "</tbody></table></details></section>"
