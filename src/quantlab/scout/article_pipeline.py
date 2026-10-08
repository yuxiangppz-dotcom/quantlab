"""The independent eight-step article research path; no production switch."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from quantlab.scout.article_ai import ArticleAI
from quantlab.scout.article_charts import VERSION as CHART_VERSION
from quantlab.scout.article_charts import chart_context, load_charts
from quantlab.scout.article_config import DEFAULT_CONFIG, config_dict
from quantlab.scout.article_data import bind_context, load_market
from quantlab.scout.article_engine import (
    allocate_budget,
    analyze_stock,
    assess_groups,
    assess_market,
)
from quantlab.scout.article_input import compact_group, compact_program
from quantlab.scout.article_levels import compute_price_levels, freeze_entry_reference
from quantlab.scout.article_notices import NOTICE_CONFIG, collect_article_notices
from quantlab.scout.article_review import MODE, bind_review
from quantlab.scout.article_risks import DEFAULT_CONFIG as RISK_CONFIG
from quantlab.scout.article_risks import (
    evaluate_moneyflow,
    evaluate_unlock_risk,
    high_divergence_flag,
    normalize_lhb_events,
)
from quantlab.scout.models import SHANGHAI, fingerprint

SOURCE_BUDGET = {"max_requests": 600, "max_seconds": 1800, "max_rows": 500_000}
PIPELINE_VERSION = "article_eight_steps_v1"


def frozen_config():
    skill = Path(__file__).resolve().parents[3] / "skills" / "review-shortline-opportunity"
    return {
        "pipeline_version": PIPELINE_VERSION,
        "strategy": config_dict(),
        "risk": RISK_CONFIG,
        "review": {"mode": MODE, "external_model_calls": 0, "screenshots_required": False},
        "review_skill_sha256": {
            str(path.relative_to(skill)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(skill.rglob("*.md"))
        },
        "sources": SOURCE_BUDGET,
        "notices": NOTICE_CONFIG,
        "chart_version": CHART_VERSION,
        "production_switch": False,
        "code_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob("article_*.py"))
        },
    }


def write_new(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def source_status(pack, api, code=None):
    rows = [row for row in pack.get("coverage", []) if row["api"] == api]
    if code is not None:
        targeted = [
            row
            for row in rows
            if row.get("ts_code") == code or row.get("params", {}).get("ts_code") == code
        ]
        if targeted:
            rows = targeted
    return (
        "available"
        if rows and all(row["status"] == "available" and not row.get("truncated") for row in rows)
        else "unknown"
    )


def merge_packs(*packs):
    records = defaultdict(list)
    coverage = {}
    for pack in packs:
        for api, rows in pack.get("records", {}).items():
            existing = {fingerprint(row) for row in records[api]}
            for row in rows:
                identifier = fingerprint(row)
                if identifier not in existing:
                    records[api].append(row)
                    existing.add(identifier)
        for row in pack.get("coverage", []):
            coverage[row.get("artifact_ref", fingerprint(row))] = row
    return {
        "records": dict(records),
        "coverage": list(coverage.values()),
        "pack_metadata": [
            {key: value for key, value in pack.items() if key not in {"records", "coverage"}}
            for pack in packs
        ],
    }


def prepare(root, pack, cutoff_at, chart_manifest=None, *, budget_check=True):
    """All universe, market, groups and shapes precede allocation and paid research."""
    market = load_market(Path(root))
    if market["freshness"] != "current" or market["calendar_status"] != "available":
        raise ValueError("Current completed market/calendar evidence is not ready")
    signal = str(market["signal_date"])
    sessions = [str(day) for day in market["sessions"]]
    future = [day for day in sessions if day > signal]
    if not future:
        raise ValueError("Next target trading session is unknown")
    context = bind_context(market, pack, market["signal_date"], cutoff_at)
    from quantlab.scout.article_data import extend_context

    context = extend_context(Path(root), context, market["signal_date"], cutoff_at)
    analyses = {
        code: analyze_stock(code, bars, sessions, signal, context["security_by_code"].get(code, {}))
        for code, bars in market["bars_by_code"].items()
    }
    environment = assess_market(
        analyses, sessions, signal, limit_structure=context.get("limit_structure")
    )
    environment["mode"] = environment["state"]
    groups = assess_groups(context["group_snapshots"], analyses, sessions, signal, cutoff_at)
    eligible_groups = [
        group for group in groups if group["group_state"] in {"established", "emerging"}
    ]
    candidates = []
    for code, stock in analyses.items():
        memberships = [group for group in eligible_groups if code in group["members"]]
        setups = [
            route for route in DEFAULT_CONFIG.routes if stock["routes"][route]["status"] == "pass"
        ]
        if not memberships or not setups or stock["eligibility"] == "excluded":
            continue
        # Same-stock overlapping routes remain recorded; deterministic A first.
        setup = stock["routes"][setups[0]]
        raw = pack.get("records", {})
        flow_rows = [row for row in raw.get("moneyflow", []) if row["ts_code"] == code]
        levels = compute_price_levels(
            stock["adjusted_bars"], sessions, signal_date=signal, setup=setup
        )
        entry = freeze_entry_reference(
            stock["adjusted_bars"],
            levels,
            setup=setup,
            market_mode=environment["state"],
            signal_date=signal,
        )
        flow = evaluate_moneyflow(
            flow_rows,
            stock["adjusted_bars"],
            sessions,
            signal_date=signal,
            setup=setup,
            # Current ATR bands must not be retroactively treated as old support.
            # A historical frozen-band proof is not yet available at cold start.
            support_broken=False,
            high_divergence=high_divergence_flag(stock["metrics"]) is True,
            source_status=source_status(pack, "moneyflow"),
            units_verified=bool(flow_rows) and all(row.get("_units_verified") for row in flow_rows),
        )
        flow["source_ids"] = sorted({row["_source_id"] for row in flow_rows})
        flow["structural_break_status"] = "unknown_no_prior_frozen_band"
        lhb_rows = [row for row in raw.get("top_list", []) if row["ts_code"] == code]
        seats = [row for row in raw.get("top_inst", []) if row["ts_code"] == code]
        lhb = normalize_lhb_events(
            lhb_rows,
            seats,
            target_date=future[0],
            cutoff_at=cutoff_at,
            source_status=source_status(pack, "top_list"),
        )
        basics = [
            row
            for row in raw.get("daily_basic", [])
            if row["ts_code"] == code and row.get("trade_date") == signal.replace("-", "")
        ]
        total_shares = (
            basics[-1].get("normalized", {}).get("total_share_shares") if basics else None
        )
        unlocks = [
            dict(row, float_share=row.get("normalized", {}).get("float_share_shares"))
            for row in raw.get("share_float", [])
            if row["ts_code"] == code
        ]
        unlock = evaluate_unlock_risk(
            unlocks,
            sessions,
            signal_date=signal,
            total_shares=total_shares,
            source_status=source_status(pack, "share_float", code),
        )
        notice_rows = [
            row
            for api in ("forecast", "stk_holdertrade", "announcements")
            for row in raw.get(api, [])
            if row.get("ts_code") == code
        ]
        notice_coverage = [
            row
            for row in pack.get("coverage", [])
            if row["api"] == "announcements" and row.get("ts_code") == code
        ]
        evidence_gap = not notice_coverage or any(
            row["status"] != "available" for row in notice_coverage
        )
        risk_events = []
        for row in notice_rows:
            flags = row.get("risk_flags", [])
            if row.get("type") and re.search(r"预亏|首亏|续亏|预减", str(row["type"])):
                flags = [
                    *flags,
                    {
                        "type": "disclosed_loss_or_decline_forecast",
                        "action": "watch_only",
                        "source_id": row.get("_source_id"),
                        "report_period": row.get("end_date"),
                        "body_verification": "pending",
                    },
                ]
            if row.get("in_de") in {"DE", "减持"}:
                flags = [
                    *flags,
                    {
                        "type": "disclosed_holder_decrease",
                        "action": "watch_only",
                        "source_id": row.get("_source_id"),
                        "scope": "reported_change_not_future_execution",
                    },
                ]
            risk_events.extend(flags)
        # A title or third-party theme is not proof of a fully investigated risk.
        actions = [flow["action"], lhb["action"], unlock["action"], entry["action"]]
        actions.extend(flag.get("action", "review_required") for flag in risk_events)
        reasons = [
            reason for item in (flow, lhb, unlock, entry) for reason in item.get("reason_codes", [])
        ]
        ceiling = stock["eligibility"]
        if "exclude" in actions:
            ceiling = "excluded"
        elif (
            ceiling != "priority_candidate"
            or any(action != "pass" for action in actions)
            or not any(group["group_state"] == "established" for group in memberships)
            or environment["priority_cap"] == 0
            or evidence_gap
        ):
            ceiling = "watch_only"
        if evidence_gap:
            reasons.append("official_announcement_review_pending")
        primary = memberships[0]
        candidates.append(
            {
                **stock,
                "group_ids": [group["group_id"] for group in memberships],
                "primary_group_id": primary["group_id"],
                "route": setups[0],
                "eligibility_ceiling": ceiling,
                "reason_codes": sorted(set(reasons)),
                "program": {
                    "setup": setup,
                    "metrics": {**stock["metrics"], "close": stock["adjusted_bars"][-1]["close"]},
                    "moneyflow": flow,
                    "lhb": lhb,
                    "levels": levels,
                    "entry": entry,
                    "unlock": unlock,
                    "notices": notice_rows,
                    "risk_events": risk_events,
                    "risk_body_pending": evidence_gap,
                },
            }
        )
    allocation = allocate_budget(candidates, groups)
    codes = [stock["ts_code"] for stock in allocation["deep"]]
    charts = load_charts(chart_manifest, codes, sessions, signal, cutoff_at)
    facts = []
    seen_facts = set()

    def fact(subject, dimension, value, period=signal):
        row = {"subject": subject, "dimension": dimension, "period": period, "value": value}
        row["fact_id"] = "fact-" + fingerprint(row)[:24]
        if row["fact_id"] not in seen_facts:
            facts.append(row)
            seen_facts.add(row["fact_id"])
        return row["fact_id"]

    market_ref = fact(
        "SHSZ_mainboard",
        "market",
        {
            key: value
            for key, value in environment.items()
            if key not in {"daily_metrics", "rule_results"}
        },
    )
    used_groups = {group for stock in allocation["deep"] for group in stock["group_ids"]}
    group_refs = {
        group["group_id"]: fact(
            group["group_id"],
            "group",
            compact_group(group),
        )
        for group in eligible_groups
        if group["group_id"] in used_groups
    }
    deep = []
    for stock in allocation["deep"]:
        code = stock["ts_code"]
        peers = [
            peer
            for peer in allocation["context"]
            if peer["ts_code"] != code and stock["primary_group_id"] in peer["group_ids"]
        ][:2]
        refs = [market_ref, *[group_refs[group] for group in stock["group_ids"]]]
        compact = compact_program(stock["program"])
        refs.extend(fact(code, key, value) for key, value in compact.items())
        if (
            compact.get("ai_input_ceiling") == "watch_only"
            and stock["eligibility_ceiling"] == "priority_candidate"
        ):
            stock["eligibility_ceiling"] = "watch_only"
            stock["reason_codes"].append("research_notice_context_incomplete")
        refs.append(fact(code, "recent_chart", chart_context(charts[code])))
        for peer in peers:
            refs.append(
                fact(
                    peer["ts_code"],
                    "qualified_peer",
                    {
                        "metrics": peer["metrics"],
                        "route": peer["route"],
                        "ceiling": peer["eligibility_ceiling"],
                        "funds": {
                            "status": peer["program"]["moneyflow"]["status"],
                            "net_ratio_3": peer["program"]["moneyflow"].get("net_ratio_3"),
                        },
                        "counter_codes": peer["reason_codes"],
                    },
                )
            )
        deep.append(
            {
                "ts_code": code,
                "name": stock["security"].get("name", code),
                "route": stock["route"],
                "program_ceiling": stock["eligibility_ceiling"],
                "reason_codes": stock["reason_codes"],
                "fact_ids": refs,
                "peer_codes": [peer["ts_code"] for peer in peers],
            }
        )
    research = {
        "strategy_version": DEFAULT_CONFIG.version,
        "signal_date": signal,
        "target_date": future[0],
        "cutoff_at": str(cutoff_at),
        "market_mode": environment["state"],
        "deep": deep,
        "facts": facts,
        "charts": charts,
    }
    # This runs before announcement/chart supplementation and before any paid API.
    if budget_check:
        ArticleAI(Path(root) / "unused-preflight").preflight(research)
    return {
        "market_data": market,
        "environment": environment,
        "groups": groups,
        "candidates": candidates,
        "allocation": allocation,
        "research": research,
    }


def freeze(prepared, model_result, run_id, generated_at, *, config_snapshot=None):
    research = prepared["research"]
    by_code = {row["ts_code"]: row for row in prepared["allocation"]["deep"]}
    reviews = {row["ts_code"]: row for row in model_result.get("reviews", [])}
    final = []
    priority_count = 0
    cap = prepared["environment"]["priority_cap"]
    for deep in research["deep"]:
        code = deep["ts_code"]
        stock, ai = by_code[code], reviews.get(code)
        if stock["eligibility_ceiling"] in {"excluded", "exclude", "reject"}:
            continue
        actions = [
            stock["program"].get(key, {}).get("action")
            for key in ("moneyflow", "lhb", "unlock", "entry")
        ]
        actions.extend(event.get("action") for event in stock["program"].get("risk_events", []))
        if "exclude" in actions:
            continue
        effects = {
            dimension.get("effect") for dimension in (ai or {}).get("dimensions", {}).values()
        }
        critical_unknown = any(
            (ai or {}).get("dimensions", {}).get(key, {}).get("effect") == "unknown"
            for key in ("group_event", "setup", "lhb", "funds", "levels")
        )
        if ai and (ai["decision"] == "reject" or "reject" in effects):
            continue
        status = "watch"
        if (
            ai
            and ai["decision"] == "priority"
            and stock["eligibility_ceiling"] == "priority_candidate"
            and all(action == "pass" for action in actions)
            and "downgrade" not in effects
            and not critical_unknown
            and ai.get("daily_review", {}).get("status") == "complete"
            and ai.get("daily_review", {}).get("quality") in {"good", "mixed"}
            and priority_count < cap
        ):
            status = "priority"
            priority_count += 1
        final.append(
            {
                "ts_code": code,
                "name": deep["name"],
                "route": stock["route"],
                "final_status": status,
                "program_ceiling": stock["eligibility_ceiling"],
                "program": stock["program"],
                "price": stock["program"]["entry"],
                "reason_codes": stock["reason_codes"] + ([] if ai else ["ai_review_pending"]),
                "ai": ai
                or {
                    "rationale": "研究结论待核查；仅保留程序形态观察。",
                    "next_day_hypothesis": "等待完整研究证据，暂无优先参与判断。",
                    "strongest_counter": "模型结果未形成可验证的完整研究判断。",
                },
                "chart": chart_context(research["charts"][code]),
                "chart_version": CHART_VERSION,
                "chart_status": (ai or {}).get("daily_review", {}).get("status", "unavailable"),
                "review_mode": MODE,
                "data_version": fingerprint(
                    {"signal": research["signal_date"], "history": stock["adjusted_bars"]}
                ),
            }
        )
    final.sort(
        key=lambda row: (row["final_status"] != "priority", list(by_code).index(row["ts_code"]))
    )
    final = final[: prepared["environment"]["total_cap"]]
    report = {
        "run_id": run_id,
        "strategy_version": DEFAULT_CONFIG.version,
        "config_hash": fingerprint(config_snapshot or frozen_config()),
        "signal_date": research["signal_date"],
        "target_date": research["target_date"],
        "cutoff_at": research["cutoff_at"],
        "generated_at": str(generated_at),
        "status": "research_pending_observation",
        "market": prepared["environment"],
        "market_explanation": model_result.get("market_explanation", ""),
        "directions": [
            group
            for group in prepared["groups"]
            if group["group_state"] in {"established", "emerging"}
        ][:3],
        "candidates": final,
        "deep_candidates": research["deep"],
        "review_mode": MODE,
        "model_receipt": {key: model_result.get(key) for key in ("calls", "tokens", "errors")},
        "external_model_calls": 0,
        "cold_start_notes": ["历史成员/可得性缺口不回填；新策略参数未经收益验证。"],
        "performance_status": "待观察",
        "production_switched": False,
    }
    report["freeze_hash"] = fingerprint(report)
    return report


def collect_article_sources(root, *, client=None):
    """A bounded isolated refresh, full directions then all qualifying shapes' checks."""
    from datetime import timedelta

    from quantlab.scout.article_sources import ArticleSources
    from quantlab.scout.cloud_data import Fetcher, refresh_calendar, refresh_market

    root = Path(root)
    now = datetime.now(SHANGHAI)
    if (root / ".scout-cloud").read_text().strip() != "quantlab-scout-cloud-v1":
        raise ValueError("Missing explicit isolated data-refresh marker")
    fetcher = Fetcher(root, now, client=client)
    refresh_calendar(root, fetcher, now)
    refresh_market(root, fetcher, now, history_sessions=120)
    market = load_market(root)
    signal = market["signal_date"]
    future = [day for day in market["sessions"] if day > signal]
    if not future:
        raise ValueError("Next target session missing after calendar refresh")
    source = ArticleSources(
        root,
        cutoff=now + timedelta(seconds=SOURCE_BUDGET["max_seconds"]),
        online=True,
        client=client,
        **SOURCE_BUDGET,
    )
    packs = [source.collect_basic_context(signal, future[0])]
    records = {}
    records["stock_basic"] = source.fetch(
        "stock_basic", {"list_status": "L"}, scope="current_listed_master", freshness_required=False
    ).rows
    records["daily_basic"] = source.fetch(
        "daily_basic",
        {"trade_date": signal.strftime("%Y%m%d")},
        expected_date=signal,
        scope="whole_signal_day",
    ).rows
    packs.append(source.report(records, coverage_mode="fresh_master_and_total_shares"))
    packs.append(
        source.collect_direction_sources(
            signal,
            market["sessions"],
            member_request_budget=max(0, SOURCE_BUDGET["max_requests"] - source.calls - 120),
            reserve_requests=120,
        )
    )
    preliminary = merge_packs(*packs)
    write_new(root / "article_source_packs" / (uuid4().hex + ".prechecks.json"), preliminary)
    calculated = prepare(root, preliminary, datetime.now(SHANGHAI).isoformat(), budget_check=False)
    shape_codes = [row["ts_code"] for row in calculated["candidates"]]
    packs.append(
        source.collect_candidate_checks(
            signal, market["sessions"], shape_codes, target_sessions=future[:5]
        )
    )
    result = merge_packs(*packs)
    result["costs"] = {"market_requests": fetcher.calls, "enhanced_requests": source.calls}
    result["collected_at"] = datetime.now(SHANGHAI).isoformat()
    return result


def _article_root(root):
    root = Path(root).resolve()
    if not root.name.startswith("scout_article_"):
        raise ValueError("Article research requires its own explicitly named isolated volume")
    if (root / ".scout-article").read_text().strip() != "quantlab-scout-article-v1":
        raise ValueError("Article volume marker is missing")
    return root


def run_article(root, *, online=False, source_pack=None, chart_manifest=None, source_client=None):
    """Prepare a fixed evidence packet for Codex; never call an external model."""
    from datetime import timedelta

    from quantlab.scout.daily_runtime import exclusive

    if chart_manifest is not None:
        raise ValueError("Current Codex daily review does not require screenshot manifests")
    root = _article_root(root)
    now = datetime.now(SHANGHAI)
    run_id = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    folder = root / "article_runs" / run_id
    with exclusive(root / "article-run.lock"):
        folder.mkdir(parents=True)
        config_snapshot = json.loads(json.dumps(frozen_config()))
        write_new(folder / "config.json", config_snapshot)
        try:
            pack = (
                source_pack
                if source_pack is not None
                else collect_article_sources(root, client=source_client)
                if online
                else {"records": {}, "coverage": []}
            )
            write_new(folder / "sources.json", pack)
            prepared = prepare(root, pack, datetime.now(SHANGHAI).isoformat())
            if online:
                codes = [row["ts_code"] for row in prepared["allocation"]["deep"]]
                notices = collect_article_notices(
                    root,
                    codes,
                    (datetime.now(SHANGHAI) + timedelta(minutes=10)).isoformat(),
                    online=True,
                )
                pack = merge_packs(pack, notices)
                write_new(folder / "sources-with-notices.json", pack)
                prepared = prepare(root, pack, datetime.now(SHANGHAI).isoformat())
            if fingerprint(frozen_config()) != fingerprint(config_snapshot):
                raise ValueError("Research code/config changed during input collection")
            write_new(folder / "research.json", prepared["research"])
            write_new(
                folder / "diagnostics.json",
                {
                    "market": prepared["environment"],
                    "groups": prepared["groups"],
                    "shape_candidates": prepared["candidates"],
                    "allocation": prepared["allocation"],
                },
            )
            write_new(
                folder / "state.json",
                {
                    "stage": "awaiting_codex_browser_daily_review",
                    "review_mode": MODE,
                    "external_model_calls": 0,
                    "target_date": prepared["research"]["target_date"],
                    "research_hash": fingerprint(prepared["research"]),
                    "production_switched": False,
                },
            )
            return folder
        except Exception as exc:
            write_new(
                folder / "failure.json",
                {
                    "status": "failed_preserved",
                    "error_type": type(exc).__name__,
                    "started_at": now.isoformat(),
                    "details": str(exc) if isinstance(exc, ValueError) else "See local diagnostics",
                },
            )
            raise


def finalize_article(root, run_folder, review_file):
    """Freeze the actual main agent review against the unchanged prepared packet."""
    from quantlab.scout.article_report import render_report
    from quantlab.scout.daily_runtime import exclusive

    root = _article_root(root)
    folder = Path(run_folder).resolve()
    if folder.parent != (root / "article_runs").resolve() or folder.is_symlink():
        raise ValueError("Prepared article run must belong to the isolated volume")
    with exclusive(root / "article-run.lock"):
        if (folder / "completed.json").exists():
            return folder
        config_snapshot = json.loads((folder / "config.json").read_text())
        if fingerprint(frozen_config()) != fingerprint(config_snapshot):
            raise ValueError("Prepared code/config changed; prepare a new fixed packet")
        research = json.loads((folder / "research.json").read_text())
        diagnostic = json.loads((folder / "diagnostics.json").read_text())
        document = json.loads(Path(review_file).read_text(encoding="utf-8"))
        saved_review = folder / "codex-review.json"
        if saved_review.exists():
            result = json.loads(saved_review.read_text())
            if fingerprint(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"calls", "tokens", "errors", "external_model_calls"}
                }
            ) != fingerprint(document):
                raise ValueError("A different Codex review is already frozen for this packet")
        else:
            result = bind_review(document, research)
            write_new(saved_review, result)
        prepared = {
            "research": research,
            "environment": diagnostic["market"],
            "groups": diagnostic["groups"],
            "allocation": diagnostic["allocation"],
        }
        report = freeze(
            prepared,
            result,
            folder.name,
            datetime.now(SHANGHAI).isoformat(),
            config_snapshot=config_snapshot,
        )
        report["research_cutoff_at"] = research["cutoff_at"]
        report["reviewed_at"] = max(
            [
                research["cutoff_at"],
                *[row["daily_review"]["observed_at"] for row in result["reviews"]],
            ]
        )
        report["cutoff_at"] = report["reviewed_at"]
        report.pop("freeze_hash")
        report["freeze_hash"] = fingerprint(report)
        frozen_path = folder / "frozen-report.json"
        if frozen_path.exists():
            report = json.loads(frozen_path.read_text())
        else:
            write_new(frozen_path, report)
        markdown, html = render_report(report)
        for path, text in ((folder / "report.md", markdown), (folder / "report.html", html)):
            if path.exists():
                if path.read_text(encoding="utf-8") != text:
                    raise ValueError("Frozen report display bytes conflict")
            else:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(text)
        track_article(root, report)
        if not (folder / "report.json").exists():
            write_new(folder / "report.json", report)
        write_new(
            folder / "completed.json",
            {
                "stage": "frozen_pending_observation",
                "freeze_hash": report["freeze_hash"],
                "external_model_calls": 0,
                "production_switched": False,
            },
        )
        return folder


def track_article(root, report):
    """Observe every frozen selected row; future endpoints remain pending."""
    from datetime import time

    from quantlab.scout.article_data import _context_root, _read_context
    from quantlab.scout.article_risks import day_key, finite
    from quantlab.scout.article_tracking import observe, save_observation

    root = _context_root(root)
    run_id = str(report.get("run_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", run_id):
        raise ValueError("Unsafe article observation run identity")
    now = datetime.now(SHANGHAI)
    market = load_market(root, now=now)
    raw = {
        "asof": now.isoformat(),
        "sessions": [str(day) for day in market["sessions"]],
        "daily": market["bars_by_code"],
        "limits": {},
        "statuses": {},
    }
    for code, bars in market["bars_by_code"].items():
        # Read the currently joined actual target prices on every recovery. An
        # earlier missing price-limit partition never permanently poisons entry.
        raw["limits"][code] = [
            {
                "date": row["date"],
                "up_limit": row["up_limit"],
                "down_limit": row["down_limit"],
                "source_status": "available",
                "source_ids": row.get("source_ids", []),
                "availability": "retrospective_actual_local_price_limit_partition",
                **({"tick": row["tick"]} if "tick" in row else {}),
            }
            for row in bars
            if finite(row.get("up_limit")) is not None
            and finite(row.get("down_limit")) is not None
            and row.get("join_status") != "conflict_unknown"
        ]
    target = day_key(report.get("target_date"))
    target_contexts = []
    for path in sorted((root / "article_context" / "snapshots").glob("*.json")):
        context, digest = _read_context(path)
        known = datetime.fromisoformat(context["known_at"].replace("Z", "+00:00")).astimezone(
            SHANGHAI
        )
        if day_key(context["signal_date"]) != target or known > now:
            continue
        # The same historical availability rule as extend_context: a later
        # current master or late reconstructed freeze cannot clear old status.
        target_end = datetime.combine(
            datetime.fromisoformat(target).date(), time(23, 59, 59), SHANGHAI
        )
        target_start = datetime.combine(datetime.fromisoformat(target).date(), time(), SHANGHAI)
        if target_start <= known <= target_end:
            target_contexts.append((known, digest, context))
    if target_contexts:
        latest = max(item[0] for item in target_contexts)
        latest_payloads = {
            digest: context for known, digest, context in target_contexts if known == latest
        }
        if len(latest_payloads) == 1:
            context = next(iter(latest_payloads.values()))
            for code, state in context["security_states"].items():
                state = state if isinstance(state, dict) else {}
                flags = state.get("status", {})
                flags = flags if isinstance(flags, dict) else {}
                st, delisted, suspended = (
                    flags.get("is_st"),
                    flags.get("is_delisting"),
                    state.get("suspended"),
                )
                status = (
                    "delisting"
                    if delisted is True
                    else "st"
                    if st is True
                    else "suspended"
                    if suspended is True
                    else "tradable"
                    if all(value is False for value in (st, delisted, suspended))
                    else "unknown"
                )
                raw["statuses"][code] = {
                    "date": target,
                    "status": status,
                    "observed_at": latest.isoformat(),
                    "source_status": "available" if status != "unknown" else "unknown",
                    "context_sha256": next(iter(latest_payloads)),
                    "qualification_flags": {
                        "is_st": st if isinstance(st, bool) else None,
                        "is_delisting": delisted if isinstance(delisted, bool) else None,
                        "is_suspended": suspended if isinstance(suspended, bool) else None,
                    },
                }
        # Equal-time disagreement is unknown; no favorable tie breaking.
    # Daily rows and today's master never manufacture target qualification.
    output = root / "article_observations"
    if not output.resolve().is_relative_to(root):
        raise ValueError("Article observation output resolves outside isolated root")
    directory = output / run_id / "observations"
    if not directory.resolve().is_relative_to(output.resolve()):
        raise ValueError("Article observation run resolves outside selected root")
    saved = []
    for path in sorted(directory.glob("*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(output.resolve()):
            raise ValueError("Article observations must remain regular isolated files")
        record = json.loads(path.read_text(encoding="utf-8"))
        observed = datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("Previous article observation must have an aware observed_at")
        saved.append((observed, record))
    previous = None
    if saved:
        latest = max(item[0] for item in saved)
        latest_records = [record for observed, record in saved if observed == latest]
        referenced = {record.get("previous_sha256") for record in latest_records}
        terminal = [record for record in latest_records if record.get("sha256") not in referenced]
        if len(terminal) != 1:
            raise ValueError("Same-time article observations have conflicting history")
        previous = terminal[0]
    return save_observation(output, observe(report, raw, previous))
