"""Self-contained, read-only Scout report view without scripts or external assets."""

from __future__ import annotations

from html import escape

from quantlab.scout.models import web_url
from quantlab.scout.report import (
    announcement_timing_note,
    execution_observation,
    observed_facts,
    theme_board_items,
)
from quantlab.scout.review import validate_post_run_review


def h(value: object) -> str:
    return escape(str(value), quote=True)


def official_notices(report: dict, code: str) -> list[dict]:
    notices = [
        item
        for item in report.get("evidence", [])
        if item.get("kind") == "official_announcement_index_unverified"
        and code in item.get("instrument_ids", [])
    ]
    return sorted(notices, key=lambda item: (item.get("event_dates") or [""])[0], reverse=True)


def render_html_report(report: dict, review: dict | None = None) -> str:
    """Render only archived, already checked facts; never execute model/source text."""
    if review is not None:
        validate_post_run_review(report, review)
    review_rows = review["findings"] if review else []
    selected = report["selection"]["selected"]
    candidates = {item["instrument_id"]: item for item in report["candidates"]}
    evidence = {item["evidence_id"]: item for item in report.get("evidence", [])}
    held = sum(row.get("screening_status") == "hold_for_official_notice_review" for row in selected)
    official_count = sum(
        item.get("kind") == "official_announcement_index_unverified"
        for item in report.get("evidence", [])
    )
    execution = execution_observation(report)
    timing = report.get("timing") or {}
    parts = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; script-src 'none'; img-src 'none'; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'\">",
        "<title>Scout · 短线研究观察</title>",
        "<style>",
        "*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#17212b;"
        "font:15px/1.65 system-ui,-apple-system,'Segoe UI',sans-serif}"
        "a{color:#155b85;overflow-wrap:anywhere}"
        "a:focus-visible,summary:focus-visible{outline:3px solid #d08716}"
        ".shell{max-width:1100px;margin:auto;padding:28px 20px 64px}"
        "header{background:#172b3b;color:#f8fafc;border-radius:18px;padding:28px 30px}"
        "header h1{font-size:clamp(27px,4vw,38px);margin:0 0 8px;line-height:1.2}"
        "header p{margin:7px 0;color:#dce7ee}header a{color:#fff}.meta{font-size:13px}"
        ".stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));"
        "gap:12px;margin:18px 0 22px}"
        ".stat,.panel,.card{background:#fff;border:1px solid #dce3e8;border-radius:14px}"
        ".stat{padding:15px 18px}.stat b{display:block;font-size:23px;line-height:1.3}"
        ".stat span{color:#526474;font-size:13px}"
        ".notice{border-left:5px solid #b75a29;background:#fff6ef;padding:15px 18px;"
        "border-radius:10px;margin:18px 0}.notice strong{color:#78351b}"
        "h2{font-size:23px;margin:34px 0 14px}h3{font-size:20px;margin:0}"
        ".cards{display:grid;gap:17px}.card{padding:22px 24px}"
        ".card.held{border-color:#d6895f;background:#fffaf6}"
        ".card-head{display:flex;justify-content:space-between;align-items:start;gap:16px;flex-wrap:wrap}"
        ".code{font-size:14px;color:#607180;font-weight:500;margin-left:7px}"
        ".badge{display:inline-block;border-radius:100px;padding:3px 10px;font-size:12px;"
        "font-weight:700;background:#e7edf2;color:#30475a}"
        ".badge.focus{background:#dcecf4;color:#17506c}.badge.held{background:#fbe1d2;color:#8b3c1e}"
        ".metrics{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0 16px}"
        ".metric{min-width:110px;background:#f3f6f8;border-radius:9px;padding:7px 10px}"
        ".metric b{display:block;font-size:16px}.metric small{color:#596a78}"
        ".subhead{font-size:14px;font-weight:700;margin:18px 0 6px;color:#334f61}"
        ".facts{margin:7px 0}.facts li{margin:4px 0}"
        ".sources{padding-left:21px}.sources li{margin:7px 0}"
        ".pdf-text{white-space:pre-wrap;overflow:auto;max-height:360px;padding:12px;"
        "background:#f3f6f8;border-radius:8px;font:12px/1.6 ui-monospace,monospace}"
        ".unknown{color:#695744;font-size:13px}.boundary{color:#526474;font-size:13px}"
        ".panel{padding:20px 24px;margin-top:12px;overflow:auto}"
        "table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px 9px;"
        "border-bottom:1px solid #e6ebef;text-align:left;vertical-align:top}"
        "th{background:#f4f7f9;color:#3a5262}tr:last-child td{border-bottom:0}"
        "details{border-top:1px solid #e5eaee;padding:10px 0}"
        "summary{cursor:pointer;font-weight:650}"
        ".index{max-height:520px;overflow:auto;padding-left:20px;font-size:13px}"
        ".index li{margin:11px 0}.index small{display:block;color:#5f6f7c}"
        "footer{margin-top:30px;color:#607180;font-size:13px}"
        "@media(max-width:660px){.shell{padding:12px 11px 40px}header{padding:22px 18px}"
        ".stats{grid-template-columns:1fr 1fr}.card{padding:18px 16px}.panel{padding:15px 13px}}"
        "@media print{body{background:white}.shell{max-width:none}"
        ".card,.panel{break-inside:avoid}}",
        "</style></head><body><main class='shell'>",
        "<header><h1>Scout · 短线研究观察</h1>",
        f"<p>行情截至 {h(report['market']['session'])} 收盘 · "
        f"报告生成 {h(report['finished_at'])}</p>",
        (
            f"<p>目标观察交易日 {h(timing.get('target_session') or '未知')} · "
            f"类型 {h(timing.get('report_kind') or '旧版未标注')} · "
            f"信息截点 {h(timing.get('information_cutoff') or '未知')}</p>"
        ),
        f"<p class='meta'>状态：{h(report['status'])} · "
        f"AI：{h(report.get('ai_provider') or '未调用')} / "
        f"{h(report.get('ai_model') or '无')}</p>",
        "<p>模型只给研究优先级；本页不代表已验证策略、次日可交易名单或买卖建议。</p>"
        "<nav aria-label='报告导航'><a href='#candidates'>查看候选研究卡</a> · "
        "<a href='#coverage'>查看信息源覆盖</a></nav></header>",
        "<section class='stats' aria-label='本轮摘要'>",
        f"<div class='stat'><b>{len(selected)}</b><span>模型观察股票</span></div>",
        f"<div class='stat'><b>{held}</b><span>停牌线索待核查</span></div>",
        f"<div class='stat'><b>{official_count}</b><span>定向公告索引记录</span></div>",
        f"<div class='stat'><b>{execution[0] if execution else 0}</b>"
        "<span>已知收盘等于涨停价</span></div>",
        "</section>",
    ]
    if execution:
        parts.append(f"<p class='boundary'>{h(execution[1])}</p>")
    stages = report.get("candidate_stages")
    if stages:
        parts.append(
            "<p class='boundary'>"
            f"廉价召回 {len(stages['cheap_candidates'])} 只；"
            f"AI 深查池 {len(stages['deep_candidates'])} 只；"
            f"深查预算外 {len(stages['not_deep_reason'])} 只。"
            "逐只原因见 report.json。</p>"
        )
    timing_note = announcement_timing_note(report)
    if timing_note:
        parts.append(f"<aside class='notice'><strong>{h(timing_note)}</strong></aside>")
    if held:
        parts.append(
            "<aside class='notice'><strong>公告风险拦截。</strong> "
            "有候选的官方公告索引标题含“停牌”；"
            "研究分级照常显示。须核对原文与生效日期，不能据此视作可成交名单。</aside>"
        )
    if review:
        machine_text_note = (
            "本轮部分PDF机器正文已在模型前采集；是否实际输入模型须查看输入审计。"
            if any(
                item.get("kind") == "official_pdf_text_unverified"
                for item in report.get("evidence", [])
            )
            else "本轮公告PDF正文未在模型前机器采集。"
        )
        parts.append(
            "<aside class='notice'><strong>报告生成后的人工公告正文复核。</strong> "
            f"复核时间 {h(review['reviewed_at'])}；{len(review_rows)} 条备注仅作后置风险核查，"
            f"未进入原模型分级，也不能倒填到行情日收盘。{machine_text_note}</aside>"
        )
    parts.extend(["<h2 id='candidates'>候选研究卡</h2>", "<section class='cards'>"])
    for row in selected:
        code = row["instrument_id"]
        candidate = candidates[code]
        metrics = candidate["metrics"]
        is_held = row.get("screening_status") == "hold_for_official_notice_review"
        badge = "优先核查" if row["status"] == "focus" else "一般观察"
        badge_class = "focus" if row["status"] == "focus" else ""
        parts.extend(
            [
                f"<article class='card{' held' if is_held else ''}' id='stock-{h(code)}'>",
                "<div class='card-head'><h3>"
                f"{h(candidate['name'])}<span class='code'>{h(code)}</span></h3>"
                f"<span class='badge {badge_class}'>{badge}</span></div>",
                "<div class='metrics'>",
            ]
        )
        for label, value in (
            ("1日涨跌幅", f"{metrics['return_1d']:.2%}"),
            ("5日涨跌幅", f"{metrics['return_5d']:.2%}"),
            ("20日涨跌幅", f"{metrics['return_20d']:.2%}"),
            ("成交额", f"{metrics['amount_cny'] / 1e8:.2f}亿元"),
            ("成交额/前5日均值", f"{metrics['amount_ratio_5d']:.2f}倍"),
        ):
            parts.append(f"<div class='metric'><b>{h(value)}</b><small>{h(label)}</small></div>")
        parts.append(
            "</div><div class='subhead'>模型分级与发现理由</div>"
            f"<p>{h(row.get('thesis') or '模型理由未通过核查')}</p>"
            "<div class='subhead'>可核查事实</div><ul class='facts'>"
        )
        facts = observed_facts(candidate, report.get("disclosure_context", {}).get(code, {}))
        parts.extend(f"<li>{h(fact)}</li>" for fact in facts)
        parts.append("</ul>")
        parts.append(
            "<p class='boundary'>"
            f"发现入口：{h('、'.join(row.get('opportunity_type', candidate.get('routes', []))))}；"
            f"证据等级：{h(row.get('evidence_quality', '未知'))}；"
            f"预设观察期限：{h(row.get('observation_horizon_sessions', 5))}个交易日。"
            "</p>"
        )
        hot = candidate.get("context", {}).get("hot_rank")
        if hot:
            previous = hot.get("previous_rank")
            parts.append(
                "<p class='boundary'>东方财富人气榜抓取时排名 "
                f"{h(hot['rank'])}；前次可比排名 "
                f"{h(previous if previous is not None else '未知')}。"
                "关注度只作线索，榜单精确发布时间未知。</p>"
            )
        themes = theme_board_items(report.get("evidence", []), code)
        if themes:
            parts.append("<div class='subhead'>第三方涨停题材标签（未核实，非公司公告）</div>")
            parts.append("<ul class='sources'>")
            parts.extend(f"<li>{h(item['title'])}；{h(item['body'])}</li>" for item in themes)
            parts.append("</ul>")
        parts.append("<div class='subhead'>正式公告索引</div>")
        notices = official_notices(report, code)
        body_by_url = {
            item.get("url"): item
            for item in report.get("evidence", [])
            if item.get("kind") == "official_pdf_text_unverified"
            and code in item.get("instrument_ids", [])
        }
        if notices:
            parts.append("<ul class='sources'>")
            for item in notices[:5]:
                event = (item.get("event_dates") or ["未知"])[0]
                url = item.get("url")
                link = (
                    f"<a href='{h(url)}' rel='noopener noreferrer' target='_blank'>查看原文 PDF</a>"
                    if url and web_url(url)
                    else "原文链接未知"
                )
                post = (
                    " · 模型分级后补查" if item.get("source", "").endswith("post_selection") else ""
                )
                published = item.get("published_at") or "未知"
                body_status = "机器提取正文，未人工核实" if url in body_by_url else "PDF正文未读取"
                parts.append(
                    f"<li>{h(item['title'])} <span class='unknown'>公告日 {h(event)} · "
                    f"发布时间 {h(published)} · {body_status}{post}</span> {link}"
                )
                if url in body_by_url:
                    body = body_by_url[url]["body"]
                    excerpt = body[:4000] + (
                        "\n[页面截取；完整机器文本见 report.json]" if len(body) > 4000 else ""
                    )
                    parts.append(
                        "<details><summary>展开机器提取正文（未人工核实）</summary>"
                        f"<pre class='pdf-text'>{h(excerpt)}</pre></details>"
                    )
                parts.append("</li>")
            if len(notices) > 5:
                parts.append(
                    f"<li class='unknown'>另有 {len(notices) - 5} 条索引未在卡片展开。</li>"
                )
            parts.append("</ul>")
        else:
            parts.append("<p class='unknown'>本轮未检出该股公告索引；这不表示没有公告。</p>")
        if is_held:
            parts.append(
                "<p class='notice'><strong>停牌线索 · 交易状态待核查：</strong>"
                "模型研究分级保留；须核对原文和生效日期。</p>"
            )
        stock_review = [item for item in review_rows if item["instrument_id"] == code]
        if stock_review:
            parts.append("<div class='subhead'>报告后人工复核备注（备注未参与模型分级）</div>")
            parts.append("<ul class='sources'>")
            for item in stock_review:
                label = "风险" if item["category"] == "risk" else "背景"
                parts.append(
                    f"<li><strong>{label}：</strong>{h(item['summary'])} "
                    f"<a href='{h(item['source_url'])}' rel='noopener noreferrer' "
                    "target='_blank'>查看官方 PDF</a></li>"
                )
            parts.append("</ul>")
        parts.append(f"<p class='boundary'>证据边界：{h(row['risk'])}</p>")
        parts.append(f"<p class='boundary'>失效观察：{h(row.get('invalidation') or '未知')}</p>")
        parts.append("<p class='boundary'>证据编号：")
        for index, ref in enumerate(row["evidence_ids"]):
            if index:
                parts.append(" · ")
            if ref in evidence:
                parts.append(f"<a href='#evidence-{h(ref)}'>{h(ref)}</a>")
            else:
                parts.append(h(ref))
        parts.append("</p></article>")
    if not selected:
        parts.append(
            "<div class='panel'>今天没有足够依据推荐新的关注标的；请查看状态和覆盖信息。</div>"
        )
    elif not any(row.get("status") == "focus" for row in selected):
        parts.append(
            "<div class='panel'>本轮无重点关注："
            f"{h(report['selection'].get('no_recommendation_reason') or '具体原因待核实')}"
            "</div>"
        )
    portfolio = report.get("portfolio_review", {})
    if portfolio.get("status") == "provided":
        parts.append("<h2>持仓与自选独立观察</h2><section class='panel'>")
        parts.append(
            "<p>清单不占新候选名额；这里只显示已知行情与风险，未单独调用 AI 深查。"
            f"输入时间 {h(portfolio['observed_at'])}；"
            f"超过一周：{'是' if portfolio['stale_input'] else '否'}。</p>"
        )
        parts.append(
            "<table><thead><tr><th>类别</th><th>股票</th><th>收盘</th>"
            "<th>新候选合格</th><th>已知缺口</th></tr></thead><tbody>"
        )
        for item in portfolio["rows"]:
            close = f"{item['close']:.2f}" if item["close"] is not None else "未知"
            parts.append(
                "<tr>"
                f"<td>{h(item['group'])}</td>"
                f"<td>{h(item['name'] or '身份未知')} {h(item['instrument_id'])}</td>"
                f"<td>{h(close)}</td>"
                f"<td>{'是' if item['new_candidate_eligible'] else '否'}</td>"
                f"<td>{h('、'.join(item['concerns']) or '暂无已知警示')}</td>"
                "</tr>"
            )
        parts.append("</tbody></table></section>")
    parts.extend(
        ["</section><h2 id='coverage'>信息源覆盖</h2><section class='panel'>", "<table><thead><tr>"]
    )
    parts.extend(f"<th scope='col'>{label}</th>" for label in ("来源", "状态", "条数", "说明"))
    parts.append("</tr></thead><tbody>")
    for source in report["coverage"]:
        parts.append(
            "<tr>"
            + "".join(
                f"<td>{h(value)}</td>"
                for value in (
                    source["source"],
                    source["status"],
                    source["count"],
                    source["detail"],
                )
            )
            + "</tr>"
        )
    parts.append("</tbody></table></section>")
    parts.append(
        f"<h2 id='evidence'>来源索引</h2><section class='panel'><details><summary>"
        f"展开全部 {len(evidence)} 条保存的证据记录</summary><ol class='index'>"
    )
    for item in report.get("evidence", []):
        ref = item["evidence_id"]
        url = item.get("url")
        link = (
            f" <a href='{h(url)}' rel='noopener noreferrer' target='_blank'>原文</a>"
            if url and web_url(url)
            else ""
        )
        parts.append(
            f"<li id='evidence-{h(ref)}'><b>{h(ref)}</b> · {h(item['title'])}{link}"
            f"<small>{h(item['source'])} · 发布时间："
            f"{h(item.get('published_at') or '未知')} · 获取："
            f"{h(item['retrieved_at'])}</small></li>"
        )
    parts.extend(["</ol></details></section><h2 id='limits'>限制</h2><section class='panel'><ul>"])
    parts.extend(f"<li>{h(limit)}</li>" for limit in report["limitations"])
    parts.extend(
        [
            "</ul></section><footer>Scout 本地只读报告 · 完整指标与模型用量见同轮 report.json。"
            + ("后置复核来自独立文件；" if review else "")
            + "研究结果未经前瞻收益与可交易性验证。"
            "</footer></main></body></html>"
        ]
    )
    return "".join(parts)
