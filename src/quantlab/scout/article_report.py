"""Small frozen-candidate reports; all arithmetic comes from program evidence.

Self-contained mobile HTML and equivalent Markdown share one display contract.
Missing evidence stays visible. These are research observations, never fills.
"""

from __future__ import annotations

import math
import re
from html import escape

MODE_LABELS = {
    "active": "活跃",
    "selective": "分歧",
    "risk_off": "风险期",
    "unknown": "数据不足",
}
MODE_CAPS = {"active": (5, 3), "selective": (3, 1), "risk_off": (3, 0), "unknown": (3, 0)}
ROUTE_LABELS = {"base_breakout": "横盘后温和突破", "pullback_recovery": "缩量回调后修复"}


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _text(value, default="待核查"):
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)):
        return "；".join(_text(item, "") for item in value if item is not None) or default
    if isinstance(value, dict):
        return default
    return str(value)


def _percent(value):
    number = _finite(value)
    return f"{number:+.2%}" if number is not None else "未知"


def _price(value):
    number = _finite(value)
    return f"¥{number:.2f}" if number is not None else "未知"


def _amount(value):
    number = _finite(value)
    if number is None:
        return "未知"
    if abs(number) >= 100_000_000:
        return f"{number / 100_000_000:+,.2f}亿元"
    if abs(number) >= 10_000:
        return f"{number / 10_000:+,.0f}万元"
    return f"{number:+,.0f}元"


def _markdown(value):
    # Provider/model prose is plain text, not Markdown structure or HTML.
    safe = escape(_text(value, ""), quote=False)
    safe = " ".join(safe.splitlines())
    return re.sub(r"([\\`*_[\]{}#|])", r"\\\1", safe)


def _shape(candidate):
    program = candidate.get("program", {})
    setup, metrics = program.get("setup", {}), program.get("metrics", {})
    route = candidate.get("route")
    values = [ROUTE_LABELS.get(route, "形态待核查")]
    if route == "base_breakout":
        if _finite(setup.get("B", setup.get("box_high"))) is not None:
            values.append("箱体上沿 " + _price(setup.get("B", setup.get("box_high"))))
    elif route == "pullback_recovery":
        if setup.get("p_date", setup.get("p")):
            values.append("上涨高点 " + str(setup.get("p_date", setup.get("p"))))
        contraction = _finite(setup.get("pullback_amount_ratio"))
        if contraction is not None:
            values.append(f"回调段成交额/上涨末段 {contraction:.2f}倍")
    ratio = _finite(metrics.get("amount_ratio"))
    if ratio is not None:
        values.append(f"当日成交额/此前20日中位数 {ratio:.2f}倍")
    if _finite(metrics.get("ret5")) is not None:
        values.append("近5日 " + _percent(metrics["ret5"]))
    return "；".join(values)


def _funds(candidate):
    funds = candidate.get("program", {}).get("moneyflow", {})
    windows = funds.get("windows", {})
    values = [f"{n}日 {_percent(windows.get(str(n), {}).get('flow_ratio'))}" for n in (1, 3, 5)]
    text = "供应商口径净额/成交额：" + "、".join(values)
    large = windows.get("3", {}).get("large_order_ratio")
    if _finite(large) is not None:
        text += "；3日大单及特大单买卖差额/成交额 " + _percent(large)
    if funds.get("status") != "complete":
        text += "；必要窗口待核查，不据缺数认定流出或资金支持"
    return text


def _lhb(candidate):
    lhb = candidate.get("program", {}).get("lhb", {})
    if lhb.get("status") == "not_listed":
        return "行情日龙虎榜完整查询后未上榜（中性，未上榜不等于资金流出）"
    events = lhb.get("events", [])
    if not events:
        return "龙虎榜来源或统计范围待核查，不能认定未上榜"
    descriptions = []
    for event in sorted(
        events,
        key=lambda item: (str(item.get("report_date", "")), str(item.get("event_id", ""))),
        reverse=True,
    )[:2]:
        if event.get("trusted_semantics") is not True:
            descriptions.append("披露事件的单位、统计范围或时点待核查")
            continue
        scope = (
            "单日"
            if event.get("scope_type") == "single_day"
            else (f"{event.get('scope_start', '未知')}至{event.get('scope_end', '未知')}累计区间")
        )
        text = f"{event.get('report_date', '日期未知')} {scope}：披露净额 "
        text += _amount(event.get("net_cny"))
        text += "，占同范围成交额 " + _percent(event.get("net_ratio"))
        institution = event.get("institution_status")
        if institution == "disclosed":
            text += "；榜内机构排名侧买卖差额 " + _amount(event.get("institution_rank_net_cny"))
        elif institution == "none_disclosed":
            text += "；榜内未披露机构席位"
        else:
            text += "；机构明细待核查"
        if event.get("historical_evidence"):
            text += "（历史证据，本次不滚动延长限制）"
        descriptions.append(text)
    return "；".join(descriptions)


def _levels(candidate):
    program = candidate.get("program", {})
    levels, entry = program.get("levels", {}), program.get("entry", {})
    values = []
    reference = _finite(entry.get("reference"))
    supports = [
        level
        for level in levels.get("levels", [])
        if level.get("status") == "active"
        and level.get("significant")
        and level.get("current_role") in {"support", "potential_support"}
        and _finite(level.get("upper")) is not None
        and reference is not None
        and level["upper"] <= reference
    ]
    if supports:
        support = max(supports, key=lambda level: level["upper"])
        label = "潜在支撑" if support.get("current_role") == "potential_support" else "支撑带"
        values.append(f"{label} {_price(support.get('lower'))}–{_price(support.get('upper'))}")
    high60 = levels.get("highest_60") or {}
    if _finite(high60.get("price")) is not None:
        values.append("近60日最高点 " + _price(high60["price"]))
    swing = levels.get("latest_confirmed_swing_high") or {}
    if _finite(swing.get("price")) is not None:
        values.append("最近确认摆动高点 " + _price(swing["price"]))
    resistance = _finite(entry.get("nearest_resistance_lower"))
    values.append(
        "最近显著压力带下沿 "
        + (
            _price(resistance)
            if resistance is not None
            else "未识别；空间比未知"
            if levels.get("status") == "complete"
            else "待核查"
        )
    )
    return "；".join(values)


def _entry(candidate):
    entry = candidate.get("program", {}).get("entry", {})
    low, high = _finite(entry.get("entry_low")), _finite(entry.get("entry_high"))
    if low is not None and high is not None and low <= high and entry.get("status") == "valid":
        text = f"冻结参考区间 {_price(low)}–{_price(high)}"
    else:
        text = "未形成有效冻结参考区间，保留观察"
    if _finite(entry.get("invalidation")) is not None:
        text += "；结构失效位 " + _price(entry["invalidation"])
    state = {
        "pending": "目标日开盘条件待确认",
        "unobservable": "开盘条件无法核验",
        "pass": "开盘价格条件通过，未推断成交",
        "fail": "开盘价格条件未通过",
    }
    return text + "；" + state.get(entry.get("entry_check"), "目标日开盘条件待确认")


def _chart(candidate):
    daily = candidate.get("ai", {}).get("daily_review")
    if isinstance(daily, dict):
        label = {"complete": "已浏览核查", "partial": "部分核查", "unavailable": "未核验"}
        result = "雪球日线：" + label.get(daily.get("status"), "未核验")
        findings = daily.get("findings")
        if findings:
            result += "；" + _text(findings)
        if daily.get("unknowns"):
            result += "；待核查：" + _text(daily["unknowns"])
        return result + "；日线观察不等于分时承接已确认"
    chart, judgment = candidate.get("chart", {}), candidate.get("ai", {}).get("intraday", {})
    dates = sorted(set(str(day) for day in chart.get("visible_dates", ())))
    image_ids = chart.get("image_ids", ())
    status = chart.get("chart_status", "unavailable")
    if not dates or not image_ids or status not in {"complete", "partial"}:
        return "分时未核验；未取得可核验图像，不推断承接情况"
    quality = {
        "good": "图示较好",
        "mixed": "图示存在分歧",
        "weak": "图示较弱",
        "unknown": "图示判断未知",
    }
    result = f"实际图像日期：{'、'.join(dates)}；" + quality.get(
        judgment.get("intraday_quality", judgment.get("quality")), "图示判断未知"
    )
    if status == "partial" or len(dates) < 3:
        result += "；仅部分核验，不概括最近几日整体承接"
    result += "；历史图像不代表目标日盘中条件已触发"
    return result


def _candidate_view(candidate):
    ai = candidate.get("ai", {})
    sections = [
        ("形态", _shape(candidate)),
        ("研究理由（AI判断）", _text(ai.get("rationale"), "判断尚待复核")),
        ("龙虎榜", _lhb(candidate)),
        ("资金", _funds(candidate)),
        ("位置", _levels(candidate)),
        ("参考与失效", _entry(candidate)),
        (
            "日线" if candidate.get("review_mode") == "codex_browser_daily_v1" else "分时",
            _chart(candidate),
        ),
        ("下一步观察", _text(ai.get("next_day_hypothesis"), "具体确认条件尚待复核")),
        ("最强反证", _text(ai.get("strongest_counter"), "反证尚待复核")),
    ]
    unlock = candidate.get("program", {}).get("unlock", {})
    if unlock.get("action") in {"watch_only", "review_required"}:
        text = (
            "近期解禁压力，最多观察" if unlock.get("action") == "watch_only" else "解禁风险待核查"
        )
        sections.append(("风险", text))
    return {
        "title": f"{_text(candidate.get('name'), candidate['ts_code'])} · {candidate['ts_code']}",
        "status": "优先研究" if candidate["final_status"] == "priority" else "观察",
        "sections": sections,
    }


def _view(report):
    market = report.get("market", {})
    mode = market.get("mode", market.get("state", "unknown"))
    if mode not in MODE_CAPS:
        raise ValueError("Unknown article market mode")
    candidates = report.get("candidates", [])
    total_cap, priority_cap = MODE_CAPS[mode]
    if any(candidate.get("final_status") not in {"priority", "watch"} for candidate in candidates):
        raise ValueError("Public report contains nonselected or unknown status")
    for candidate in candidates:
        # These are the already computed actions, not another copy of formulas.
        program = candidate.get("program", {})
        actions = [
            program.get(key, {}).get("action")
            for key in ("moneyflow", "lhb", "levels", "entry", "unlock")
        ]
        if any(action in {"exclude", "excluded", "reject"} for action in actions):
            raise ValueError("Public report contains program-excluded stock")
        if candidate["final_status"] == "priority" and any(
            action in {"watch_only", "watch", "review_required"} for action in actions
        ):
            raise ValueError("Public report raises stock above program ceiling")
    if (
        len(candidates) > total_cap
        or sum(candidate["final_status"] == "priority" for candidate in candidates) > priority_cap
    ):
        raise ValueError("Public report exceeds market participation caps")
    codes = [candidate["ts_code"] for candidate in candidates]
    if len(set(codes)) != len(codes):
        raise ValueError("Duplicate stock in frozen report")
    directions = []
    for item in report.get("directions", [])[:3]:
        if isinstance(item, dict):
            label = _text(item.get("name", item.get("display_name", item.get("group_id"))))
            state = item.get("status", item.get("group_state"))
            label += (
                "（持续方向）"
                if state == "established"
                else "（新观察方向）"
                if state == "emerging"
                else ""
            )
            directions.append(label)
        else:
            directions.append(_text(item))
    stats = market.get("metrics", market)
    environment = "沪深主板样本环境：" + MODE_LABELS[mode]
    advance = _finite(stats.get("adv_ratio"))
    if advance is not None:
        environment += f"；上涨占比 {advance:.1%}"
    if _finite(stats.get("median_ret1")) is not None:
        environment += "；收益中位数 " + _percent(stats["median_ret1"])
    if mode == "risk_off":
        environment += "。本策略今日暂停新增优先候选"
    elif mode == "unknown":
        environment += "。数据不足，未形成完整判断"
    empty = "当前没有符合本轮条件的候选，保留空名单" if not candidates else ""
    if not candidates and mode == "risk_off":
        empty = "本策略今日暂停新增优先候选，本轮没有观察名单"
    elif not candidates and mode == "unknown":
        empty = "数据不足，未形成完整判断，未发布完整候选名单"
    return {
        "title": f"Scout · {report.get('target_date', '目标日待确认')} 短线研究",
        "timing": f"行情截至 {report.get('signal_date', '未知')} · "
        + f"信息截至 {report.get('cutoff_at', '未知')}",
        "environment": environment,
        "directions": directions,
        "empty": empty,
        "cards": [_candidate_view(candidate) for candidate in candidates],
        "footer": "研究观察，不代表成交或已验证收益。参考区间不是委托指令，失效价不能保证成交。",
    }


CSS = """
:root{color-scheme:light;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
color:#182a37;background:#f0f4f6;font-size:16px;line-height:1.65}
*{box-sizing:border-box}body{margin:0}main{max-width:900px;margin:auto;padding:28px 18px 48px}
h1{font-size:clamp(1.45rem,5vw,2rem);line-height:1.3;margin:0 0 10px}h2{font-size:1.1rem}
.meta{color:#637785;font-size:.85rem;overflow-wrap:anywhere}header{padding:12px 0 20px}
.environment,.directions{background:#e0eef2;padding:14px 18px;border-radius:14px;margin:12px 0}
.directions p{margin:5px 0}.card{background:#fff;border:1px solid #d6e2e7;border-radius:18px;
padding:20px;margin:18px 0;box-shadow:0 5px 18px #193c4c08}.card-head{display:flex;
justify-content:space-between;align-items:flex-start;gap:12px}.card h2{margin:0}
.badge{white-space:nowrap;font-size:.8rem;padding:3px 10px;background:#e0eef2;border-radius:20px;
color:#225d71}.card p{margin:11px 0;overflow-wrap:anywhere}.label{font-weight:650;color:#31586b}
.counter{background:#fff7ee;border-left:3px solid #ce9356;padding:8px 12px;border-radius:3px}
footer{font-size:.85rem;color:#637785;padding-top:12px}.empty{padding:20px;background:#fff;border-radius:14px}
@media(max-width:520px){main{padding:20px 12px 35px}.card{padding:16px}.card-head{display:block}
.badge{display:inline-block;margin-top:8px}}
@media print{body{background:#fff}.card{box-shadow:none;break-inside:avoid}main{max-width:none}}
"""


def render_report(report: dict) -> tuple[str, str]:
    """Render only the frozen selections; internal coverage/funnel stays in JSON."""
    view = _view(report)
    markdown = [
        f"# {_markdown(view['title'])}",
        "",
        _markdown(view["timing"]),
        "",
        _markdown(view["environment"]),
        "",
    ]
    html = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{escape(view['title'])}</title><style>{CSS}</style></head><body><main>",
        f"<header><h1>{escape(view['title'])}</h1>"
        + f"<div class='meta'>{escape(view['timing'])}</div></header>",
        f"<section class='environment'>{escape(view['environment'])}</section>",
    ]
    if view["directions"]:
        markdown.extend(
            ["## 研究方向", "", *[f"- {_markdown(item)}" for item in view["directions"]], ""]
        )
        html.append(
            "<section class='directions'><h2>研究方向</h2>"
            + "".join(f"<p>{escape(item)}</p>" for item in view["directions"])
            + "</section>"
        )
    if view["empty"]:
        markdown.extend([_markdown(view["empty"]), ""])
        html.append(f"<p class='empty'>{escape(view['empty'])}</p>")
    for card in view["cards"]:
        markdown.extend([f"## {_markdown(card['title'])} · {card['status']}", ""])
        html.append(
            f"<article class='card'><div class='card-head'><h2>{escape(card['title'])}</h2>"
            f"<span class='badge'>{card['status']}</span></div>"
        )
        for label, text in card["sections"]:
            markdown.extend([f"**{label}：** {_markdown(text)}", ""])
            css = " class='counter'" if label == "最强反证" else ""
            html.append(f"<p{css}><span class='label'>{escape(label)}：</span> {escape(text)}</p>")
        html.append("</article>")
    markdown.append(view["footer"])
    html.append(f"<footer>{escape(view['footer'])}</footer></main></body></html>")
    return "\n".join(markdown) + "\n", "".join(html)
