"""Versioned D1 official-reference labels and auxiliary price observations.

This module never fetches data, changes predictions, or assumes executable fills.
Legacy H5 records are intentionally not converted into this protocol.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from statistics import mean

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, fingerprint, finite, timestamp
from quantlab.scout.tracking import _write_snapshot

VERSION = "nextday_strong_close_official5pct_v1"
STRONG_CLOSE_THRESHOLD = 0.05
COMPLETION_HOUR = 18
AUXILIARY_HORIZONS = (2, 3, 5)


def freeze_protocol(rows, packet, universe, memberships, funnel=None):
    """Called only while publishing a new prediction, never by a legacy reader."""
    candidates = {c["instrument_id"]: c for c in packet["candidates"]}
    gaps = sorted(
        c["source"]
        for c in packet.get("coverage", [])
        if c.get("status") not in {"ok", "complete", "available", "empty"}
    )
    frozen = []
    for row in rows:
        code = row["instrument_id"]
        candidate = candidates[code]
        original = universe[code]
        summary = candidate.get("source_summary") or {}
        frozen.append(
            {
                "instrument_id": code,
                "rank": row.get("rank"),
                "research_grade": row["final_status"],
                "primary_type": row["primary_type"],
                "discovery_score": original.score if finite(original.score) else None,
                "industry": memberships.get(code),
                "themes": summary.get("themes", []),
                "source_state": "degraded" if summary.get("gaps") or gaps else "no_recorded_gap",
                "d0_limit_state": "upper_limit"
                if finite(original.metrics.get("close"))
                and finite(original.metrics.get("up_limit"))
                and abs(original.metrics["close"] - original.metrics["up_limit"]) < 0.005
                else "not_upper_limit"
                if finite(original.metrics.get("close"))
                and finite(original.metrics.get("up_limit"))
                else "unknown",
                "participation_state": row.get("participation_status", "unknown"),
                "invalidation_rule": row.get("invalidation_rule"),
                "participation_cancel_rule": row.get("participation_cancel_rule"),
                "next_session_condition": row.get("next_session_condition"),
            }
        )
    score_ready = all(r["discovery_score"] is not None for r in frozen)
    order = (
        sorted(frozen, key=lambda r: (-r["discovery_score"], r["instrument_id"]))
        if score_ready
        else []
    )
    k = sum(r["research_grade"] == "focus" for r in frozen)
    timing = packet["timing"]
    value = {
        "version": VERSION,
        "classification": "prospective",
        "strong_close_threshold": STRONG_CLOSE_THRESHOLD,
        "threshold_status": "predefined_product_parameter_not_validated_optimum",
        "d1_definition": "raw_D1_close/provider_D1_pre_close_minus1",
        "completion_hour_shanghai": COMPLETION_HOUR,
        "price_proxy": "adjusted_D1_open_to_D2_close_not_fill_or_net_return",
        "auxiliary_horizons": [3, 5],
        "timing": timing,
        "input_sha256": fingerprint(packet),
        "rows": sorted(frozen, key=lambda r: r["instrument_id"]),
        "eligible_ids": sorted(universe),
        "memberships": {c: memberships.get(c) for c in universe},
        "funnel": funnel or {},
        "focus_k": k,
        "score_control_status": "frozen" if score_ready else "score_incomplete",
        "score_top_k": [r["instrument_id"] for r in order[:k]],
        "source_gaps": gaps,
        "research_rules": packet.get("research_rules", {}),
        "missing_policy": "unknown_retained_in_original_denominator_no_replacement",
    }
    return {**value, "sha256": fingerprint(value)}


def verify_freeze(report):
    freeze = report.get("nextday_freeze")
    if not freeze or freeze.get("version") != VERSION:
        raise ValueError("Missing or unsupported next-day freeze; legacy reports stay legacy")
    if freeze.get("sha256") != fingerprint({k: v for k, v in freeze.items() if k != "sha256"}):
        raise ValueError("Next-day freeze hash mismatch")
    if freeze["input_sha256"] != fingerprint(report["selection_input_packet"]):
        raise ValueError("Next-day freeze does not match original selection input")
    if freeze["strong_close_threshold"] != STRONG_CLOSE_THRESHOLD:
        raise ValueError("Unknown strong-close parameter")
    ids = [r["instrument_id"] for r in freeze["rows"]]
    if len(ids) != len(set(ids)) or not set(ids) <= set(freeze["eligible_ids"]):
        raise ValueError("Next-day freeze contains duplicate or ineligible securities")
    if set(ids) != {r["instrument_id"] for r in report["selection_input_packet"]["candidates"]}:
        raise ValueError("Next-day freeze must cover every actual deep candidate")
    return freeze


def _eligibility(report, freeze, days, publication):
    timing = report["timing"]
    target = date.fromisoformat(timing["target_session"])
    if target not in days:
        return target, "calendar_unavailable"
    if report.get("synthetic") or report.get("status") == "demo":
        return target, "synthetic"
    if freeze.get("classification") != "prospective":
        return target, "posthoc_or_shadow"
    if not timing.get("primary_eligible") or timing.get("report_kind") in {
        "late_research",
        "manual_research",
        "shadow",
        "posthoc",
    }:
        return target, "not_formal_prediction"
    if report.get("status") not in {"complete", "live_research_unvalidated"}:
        return target, "report_not_complete"
    deadline = timing.get("publication_deadline") or timing.get("publish_deadline")
    if not deadline:
        return target, "publication_deadline_not_frozen"
    if publication.get("status") not in {"published", "local_saved"}:
        return target, "publication_unknown"
    published = publication.get("published_at")
    if not published:
        return target, "publication_unknown"
    if timestamp(published) >= timestamp(deadline):
        return target, "late_research"
    if timestamp(published) >= datetime.combine(target, time(9, 30), SHANGHAI):
        return target, "published_after_market_open"
    return target, "eligible"


def publication_receipt(run_dir, report, digest):
    """Cloud actual publication overrides local saving, without rewriting the prediction."""
    for name in ("cloud-publication.json", "publication.json"):
        if (
            name == "publication.json"
            and report.get("release_identity", {}).get("delivery_channel") == "cloud"
        ):
            continue
        path = run_dir / name
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if (
                receipt.get("run_id") != report["run_id"]
                or receipt.get("source_report_sha256") != digest
            ):
                raise ValueError("Publication receipt does not match immutable prediction")
            return {**receipt, "receipt_type": name}
    published = report.get("published_at") or report["timing"].get("published_at")
    return {
        "status": "published" if published else "unknown",
        "published_at": published,
        "receipt_type": "frozen_report_field" if published else "missing",
    }


def observe_nextday(
    run_dir, canonical_dir, output_root, *, observed_at=None, allow_synthetic=False
):
    run_dir, canonical_dir, output_root = map(Path, (run_dir, canonical_dir, output_root))
    if output_root.resolve().is_relative_to(canonical_dir.resolve()):
        raise ValueError("Observation output cannot be inside canonical data")
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    digest = fingerprint(report)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["report_sha256"] != digest:
        raise ValueError("Original report integrity check failed")
    synthetic = report.get("synthetic") or report.get("status") == "demo"
    if synthetic and not allow_synthetic:
        raise ValueError("Synthetic report cannot enter real observation")
    if observed_at is not None and not (synthetic and allow_synthetic):
        raise ValueError("Clock override only allowed for synthetic observation replay")
    now = observed_at or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("Observation clock must have timezone")
    freeze = verify_freeze(report)
    storage = ParquetStorage(canonical_dir)
    days = sorted(
        {r.trade_date for r in storage.load_trading_calendar() if r.exchange == "SSE" and r.is_open}
    )
    publication = publication_receipt(run_dir, report, digest)
    target, eligibility = _eligibility(report, freeze, days, publication)
    result = {
        "run_id": report["run_id"],
        "report_sha256": digest,
        "observed_at": now.isoformat(),
        "definition_version": VERSION,
        "target_session": target.isoformat(),
        "primary_eligibility": eligibility,
        "publication": publication,
        "synthetic": bool(synthetic),
        "rows": [],
        "eligible_rows": [],
        "freeze_sha256": freeze["sha256"],
        "strong_close_threshold": STRONG_CLOSE_THRESHOLD,
        "version_identity": {
            "provider": report.get("ai_provider", report.get("provider")),
            "model": report.get("ai_model", report.get("model")),
            "prompt_version": report.get("prompt_version"),
            "schema_version": report.get("schema_version"),
            "decision_schema_version": report.get("decision_schema_version"),
            "runtime_config_fingerprint": report.get("config_sha256"),
            "installed_config_sha256": report.get("release_identity", {}).get("config_sha256"),
            "engine_commit": report.get("release_identity", {}).get("engine_commit"),
            "app_commit": report.get("release_identity", {}).get("app_commit"),
            "feature_version": report.get("feature_version"),
        },
        "release_identity": report.get("release_identity", {}),
        "limitations": (
            "Official-reference labels and adjusted price proxies; no fills/costs/net returns."
        ),
    }
    if target not in days:
        return _write_snapshot(result, output_root)
    index = days.index(target)
    relevant = days[index : index + 5]
    bars = {d: {b.instrument_id: b for b in storage.load_daily_bars_by_date(d)} for d in relevant}
    factors = {
        d: {f.instrument_id: f.adj_factor for f in storage.load_adj_factors_by_date(d)}
        for d in relevant
    }
    limits = {r.instrument_id: r for r in storage.load_daily_price_limits_by_date(target)}

    def raw(code, day):
        if day is None:
            return None, "calendar_unavailable"
        if now.astimezone(SHANGHAI) < datetime.combine(day, time(COMPLETION_HOUR), SHANGHAI):
            return None, "not_yet_due"
        if not storage.daily_bars_path(day).exists():
            return None, "partition_missing"
        bar = bars[day].get(code)
        if not bar:
            return None, "bar_missing_halt_unknown"
        if not all(
            finite(getattr(bar, f)) and getattr(bar, f) > 0
            for f in ("open", "high", "low", "close")
        ) or not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
            return None, "ohlc_invalid"
        if not finite(bar.volume) or bar.volume <= 0 or not finite(bar.amount) or bar.amount <= 0:
            return None, "no_valid_trading_activity"
        return bar, "observed"

    def auxiliary(code, h):
        end = days[index + h - 1] if index + h - 1 < len(days) else None
        first, opening_state = raw(code, target)
        last, end_state = raw(code, end)
        path = days[index : index + h] if end else []
        values = []
        for day in path:
            bar, state = raw(code, day)
            factor = factors.get(day, {}).get(code)
            if state == "observed" and finite(factor) and factor > 0:
                values.append(bar.low * factor)
        f1, f2 = factors[target].get(code), factors.get(end, {}).get(code)
        ready = opening_state == end_state == "observed"
        factor_ready = all(finite(f) and f > 0 for f in (f1, f2))
        opening = first.open * f1 if ready and factor_ready else None
        closing = last.close * f2 if ready and factor_ready else None
        return {
            "horizon_sessions": h,
            "end_session": end.isoformat() if end else None,
            "status": "observed"
            if ready and factor_ready
            else "factor_missing_or_invalid"
            if ready
            else f"{opening_state}:{end_state}",
            "adjusted_price_return": closing / opening - 1 if opening and closing else None,
            "adverse_daily_low_change": min(0, min(values) / opening - 1)
            if opening and len(values) == h
            else None,
            "path_valid_sessions": len(values),
            "path_expected_sessions": h,
            "mode": "price_observation",
            "actual_execution": "unknown",
        }

    eligible = {}
    for code in freeze["eligible_ids"]:
        bar, state = raw(code, target)
        reference = bar.pre_close if bar else None
        if state == "observed" and not (finite(reference) and reference > 0):
            state = "official_reference_missing_or_invalid"
        value = bar.close / reference - 1 if state == "observed" else None
        limit = limits.get(code)
        limit_known = bool(limit and finite(limit.up_limit) and limit.up_limit > 0)
        eligible[code] = {
            "instrument_id": code,
            "d1_status": state,
            "d1_official_return": value,
            "strong_close": value >= STRONG_CLOSE_THRESHOLD - 1e-12 if value is not None else None,
            "official_reference_price": reference if finite(reference) else None,
            "reference_definition": "provider_daily_pre_close_not_previous_raw_close",
            "d1_open_gap": bar.open / reference - 1 if value is not None else None,
            "d1_open_to_close": bar.close / bar.open - 1 if bar else None,
            "d1_high_return": bar.high / reference - 1 if value is not None else None,
            "d1_touched_upper_limit": bar.high >= limit.up_limit - 0.005
            if bar and limit_known
            else None,
            "d1_closed_upper_limit": abs(bar.close - limit.up_limit) < 0.005
            if bar and limit_known
            else None,
            "d1_open_at_upper_limit": abs(bar.open - limit.up_limit) < 0.005
            if bar and limit_known
            else None,
            "d1_one_price": abs(bar.high - bar.low) < 0.005 if bar else None,
            "limit_status": "provider_day_limit" if limit_known else "unknown",
        }
    observed = sorted(
        (r for r in eligible.values() if r["d1_official_return"] is not None),
        key=lambda r: -r["d1_official_return"],
    )
    for row in observed:
        positions = [
            i + 1
            for i, r in enumerate(observed)
            if r["d1_official_return"] == row["d1_official_return"]
        ]
        row["d1_cross_section_rank"] = mean(positions)
        row["d1_rank_denominator"] = len(observed)
    for code, row in eligible.items():
        industry = freeze["memberships"].get(code)
        peers = [
            r for c, r in eligible.items() if industry and freeze["memberships"].get(c) == industry
        ]
        values = [p["d1_official_return"] for p in peers if p["d1_status"] == "observed"]
        benchmark = mean(values) if peers and len(values) == len(peers) else None
        row.update(
            {
                "frozen_industry": industry,
                "industry_members_original": len(peers),
                "industry_members_observed": len(values),
                "industry_mean": benchmark,
                "d1_relative_industry_return": row["d1_official_return"] - benchmark
                if benchmark is not None and row["d1_official_return"] is not None
                else None,
                "industry_reference": "frozen_all_eligible_industry_members_complete",
            }
        )
    result["eligible_rows"] = list(eligible.values())
    ma_days = [d for d in days if d <= target][-20:]
    # Load each existing history partition once for all bound MA20 rules.
    ma_prices = {}
    if any(
        (r.get("invalidation_rule") or {}).get("rule_id") == "ma20_break_v1" for r in freeze["rows"]
    ):
        for day in ma_days:
            fs = {f.instrument_id: f.adj_factor for f in storage.load_adj_factors_by_date(day)}
            ma_prices[day] = {
                b.instrument_id: b.close * fs[b.instrument_id]
                for b in storage.load_daily_bars_by_date(day)
                if finite(b.close)
                and b.close > 0
                and finite(fs.get(b.instrument_id))
                and fs[b.instrument_id] > 0
            }
    for frozen in freeze["rows"]:
        code = frozen["instrument_id"]
        rule = frozen.get("invalidation_rule") or {}
        value = None
        if eligible[code]["d1_status"] == "observed":
            if rule.get("rule_id") == "relative_d1_nonpositive_v1":
                value = eligible[code]["d1_relative_industry_return"]
            elif rule.get("rule_id") == "ma20_break_v1":
                prices = [ma_prices.get(d, {}).get(code) for d in ma_days]
                if len(prices) == 20 and all(v is not None for v in prices):
                    value = prices[-1] / mean(prices) - 1
        cancellation = frozen.get("participation_cancel_rule") or {}
        opening = bars[target].get(code)
        limit = limits.get(code)
        cancel = None
        if (
            cancellation.get("rule_id") == "target_open_limit_or_halt_v1"
            and eligible[code]["d1_status"] == "observed"
            and opening
            and limit
            and all(finite(v) and v > 0 for v in (limit.up_limit, limit.down_limit))
        ):
            cancel = any(abs(opening.open - v) < 0.005 for v in (limit.up_limit, limit.down_limit))
        result["rows"].append(
            {
                **eligible[code],
                **frozen,
                "auxiliary": [auxiliary(code, h) for h in AUXILIARY_HORIZONS],
                "research_rule_observation": {
                    "rule_id": rule.get("rule_id"),
                    "value": value,
                    "invalidated": value <= 0 if value is not None else None,
                    "status": "observed"
                    if value is not None
                    else "official_event_review_required"
                    if rule.get("rule_id") == "event_cancelled_d1_v1"
                    else "unknown",
                },
                "participation_cancel_observation": {
                    "rule_id": cancellation.get("rule_id"),
                    "cancelled": cancel,
                    "status": "observed_after_open_no_assumed_fill"
                    if cancel is not None
                    else "unknown_no_confirmed_halt_or_limits",
                    "actual_execution": "unknown",
                },
            }
        )
    result["controls"] = control_observation(freeze, result["rows"])
    result["attribution"] = attribute_errors(freeze, eligible)
    result["maturity"] = (
        "complete"
        if all(
            r["d1_status"] == "observed" and all(a["status"] == "observed" for a in r["auxiliary"])
            for r in result["rows"]
        )
        and result["rows"]
        else "pending_or_incomplete"
    )
    return _write_snapshot(result, output_root)


def _group(rows):
    valid = [r for r in rows if r["d1_status"] == "observed"]
    hits = sum(r["strong_close"] is True for r in valid)
    values = [r["d1_official_return"] for r in valid]
    auxiliary = {}
    for horizon in AUXILIARY_HORIZONS:
        marks = [
            a for r in rows for a in r.get("auxiliary", []) if a["horizon_sessions"] == horizon
        ]
        available = [a for a in marks if a["status"] == "observed"]
        lows = [
            a["adverse_daily_low_change"]
            for a in available
            if a["adverse_daily_low_change"] is not None
        ]
        auxiliary[str(horizon)] = {
            "original_count": len(rows),
            "observable_count": len(available),
            "unknown_count": len(rows) - len(available),
            "mean_adjusted_price_return": mean(a["adjusted_price_return"] for a in available)
            if available
            else None,
            "adverse_valid_count": len(lows),
            "mean_adverse_daily_low_change": mean(lows) if lows else None,
            "worst_daily_low_change": min(lows) if lows else None,
            "mode": "price_observation_not_execution_or_net_return",
        }
    return {
        "stock_ids": [r["instrument_id"] for r in rows],
        "original_count": len(rows),
        "observable_count": len(valid),
        "unknown_count": len(rows) - len(valid),
        "hit_count": hits,
        "strong_close_hit_rate": hits / len(valid) if valid else None,
        "unknown_fraction": (len(rows) - len(valid)) / len(rows) if rows else None,
        "mean_official_return": mean(values) if values else None,
        "negative_count": sum(v < 0 for v in values),
        "minimum_official_return": min(values) if values else None,
        "mean_open_gap": mean(r["d1_open_gap"] for r in valid) if valid else None,
        "mean_open_to_close": mean(r["d1_open_to_close"] for r in valid) if valid else None,
        "auxiliary": auxiliary,
        "theme_counts": dict(Counter(t for r in rows for t in r.get("themes", []))),
    }


def control_observation(freeze, rows):
    by_code = {r["instrument_id"]: r for r in rows}
    focus = [r for r in rows if r["research_grade"] == "focus"]
    control = [by_code[c] for c in freeze["score_top_k"]]
    groups = {
        "focus": _group(focus),
        "score_top_k": _group(control),
        "all_deep": _group(rows),
        "top3_ranked": _group(
            sorted((r for r in rows if r["rank"] is not None), key=lambda r: r["rank"])[:3]
        ),
    }
    for field in ("research_grade", "primary_type", "d0_limit_state", "source_state"):
        for label in sorted({r[field] for r in rows}):
            groups[f"{field}:{label}"] = _group([r for r in rows if r[field] == label])
    paired = (
        freeze["focus_k"] > 0
        and freeze["score_control_status"] == "frozen"
        and all(r["d1_status"] == "observed" for r in [*focus, *control])
    )
    return {
        "groups": groups,
        "k": freeze["focus_k"],
        "abstention": not focus,
        "paired_complete": paired,
        "ai_minus_score_hit_rate": groups["focus"]["strong_close_hit_rate"]
        - groups["score_top_k"]["strong_close_hit_rate"]
        if paired
        else None,
        "denominator_policy": "focus_only_same_pool_original_score_same_k_unknowns_visible",
    }


def attribute_errors(freeze, eligible):
    funnel = freeze.get("funnel") or {}
    deep = {r["instrument_id"]: r for r in freeze["rows"]}
    recalled = set(funnel.get("recalled_candidates", funnel.get("cheap_candidates", [])))
    shortlist = set(funnel.get("shortlist_candidates", funnel.get("cheap_candidates", [])))
    result = []
    for code, row in eligible.items():
        strong = row["strong_close"] is True
        own = deep.get(code)
        if row["d1_status"] != "observed":
            reason = "data_or_time_unknown"
        elif strong and code not in recalled and funnel:
            reason = "not_recalled"
        elif strong and code not in shortlist and funnel:
            reason = "shortlist_rejected"
        elif strong and not own:
            reason = "budget_not_deep" if funnel else "discovery_stage_unknown"
        elif strong and own["research_grade"] != "focus":
            reason = "deep_rank_or_grade_not_selected"
        elif own and own["research_grade"] == "focus" and row["d1_open_to_close"] < 0:
            reason = (
                "high_open_consumed_strength"
                if row["d1_open_gap"] > 0
                else "selected_then_weakened"
            )
        elif own and own["research_grade"] == "focus" and row["d1_official_return"] < 0:
            reason = "selected_then_weakened"
        else:
            reason = "no_observed_error_under_this_label"
        result.append(
            {
                "instrument_id": code,
                "strong_close": row["strong_close"],
                "reason": reason,
                "trade_constraint": "daily_limit_or_one_price"
                if row["d1_open_at_upper_limit"] or row["d1_one_price"]
                else "unknown",
                "actual_execution": "unknown",
            }
        )
    return {
        "scope": "frozen_eligible_only_not_full_market_recall",
        "eligible_original": len(eligible),
        "observable_count": sum(r["d1_status"] == "observed" for r in eligible.values()),
        "reason_counts": dict(Counter(r["reason"] for r in result)),
        "rows": result,
    }


def summarize_nextday(observations, *, eligible_target_sessions=None):
    """Version/date denominators; duplicate dates cannot be picked by outcome."""
    accepted, excluded, seen = [], [], set()
    for item in sorted(observations, key=lambda x: (x.get("target_session", ""), x["run_id"])):
        key = item.get("target_session")
        reason = "synthetic" if item.get("synthetic") else item.get("primary_eligibility")
        if item.get("definition_version") != VERSION:
            reason = "different_protocol"
        if reason != "eligible":
            excluded.append({"run_id": item["run_id"], "reason": reason})
        elif key in seen:
            raise ValueError(
                "Multiple formal predictions for one target day; choose at publication"
            )
        else:
            seen.add(key)
            accepted.append(item)
    groups = [i["controls"]["groups"]["focus"] for i in accepted]
    denominator = sum(g["observable_count"] for g in groups)
    hits = sum(g["hit_count"] for g in groups)
    date_rates = [
        g["strong_close_hit_rate"] for g in groups if g["strong_close_hit_rate"] is not None
    ]
    paired = [
        i["controls"]["ai_minus_score_hit_rate"]
        for i in accepted
        if i["controls"]["paired_complete"]
    ]
    coverage_days = list(eligible_target_sessions) if eligible_target_sessions is not None else None
    coverage_denominator = len(coverage_days) if coverage_days is not None else len(accepted)
    recommendation_days = sum(g["original_count"] > 0 for g in groups)
    per_version = {}
    for item in accepted:
        key = fingerprint(item.get("version_identity", {}))
        per_version.setdefault(key, []).append(item)
    version_groups = {}
    for key, items in per_version.items():
        focus_groups = [i["controls"]["groups"]["focus"] for i in items]
        valid = sum(g["observable_count"] for g in focus_groups)
        version_groups[key] = {
            "identity": items[0].get("version_identity", {}),
            "target_sessions": [i["target_session"] for i in items],
            "formal_days": len(items),
            "observable_stocks": valid,
            "stock_weighted_hit_rate": sum(g["hit_count"] for g in focus_groups) / valid
            if valid
            else None,
        }
    return {
        "version": VERSION,
        "formal_target_days": len(accepted),
        "excluded": excluded,
        "recommendation_days": recommendation_days,
        "recommendation_coverage": recommendation_days / coverage_denominator
        if coverage_denominator
        else None,
        "coverage_day_denominator": coverage_denominator,
        "coverage_scope": "provided_eligible_calendar_sessions"
        if coverage_days is not None
        else "frozen_formal_reports_only_calendar_coverage_unknown",
        "missing_formal_target_sessions": sorted(set(coverage_days) - seen)
        if coverage_days is not None
        else None,
        "abstention_days": sum(g["original_count"] == 0 for g in groups),
        "selected_original": sum(g["original_count"] for g in groups),
        "observable_stocks": denominator,
        "unknown_stocks": sum(g["unknown_count"] for g in groups),
        "strong_close_hits": hits,
        "stock_weighted_hit_rate": hits / denominator if denominator else None,
        "date_weighted_hit_rate": mean(date_rates) if date_rates else None,
        "paired_dates": len(paired),
        "date_mean_ai_minus_score_hit_rate": mean(paired) if paired else None,
        "version_groups": version_groups,
        "first_descriptive_review_target_days": 20,
        "uncertainty": "descriptive_only_shared_themes_and_overlapping_H3_H5_not_independent",
        "profitability": "not_measured_price_observation_no_execution_or_costs",
    }
