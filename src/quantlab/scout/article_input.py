"""Compact AI evidence views; full diagnostics remain in immutable run artifacts.

No arithmetic, event netting, new price choice or eligibility upgrade occurs here.
Critical active disclosed events remain separate even if a caller must stop at
the fixed input budget. Truncated notice content creates an explicit review gap.
"""

from __future__ import annotations

import math
import re

VERSION = "article_compact_evidence_v1"
NOTICE_LIMIT = 2
# Below the 2,000-character maximum: leave headroom for 24 fact tables.
NOTICE_BODY_CHARS = 500
HISTORY_EVENT_LIMIT = 2
LOCAL_PATH = re.compile(
    r"(?:[A-Za-z]:[/\\]|\\\\(?:wsl[^\\ ]*|[^\\ ]+)[\\]|/(?:home|mnt|Users|tmp)/)[^\s\"<>]+"
)


def _clean(value):
    if isinstance(value, dict):
        return {
            key: _clean(item)
            for key, item in value.items()
            if not any(word in key.lower() for word in ("path", "artifact_ref", "local_file"))
        }
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return LOCAL_PATH.sub("[local path omitted]", value)
    return value


def _pick(value, fields):
    return {key: _clean(value[key]) for key in fields if key in value}


SOURCE_FIELDS = (
    "source",
    "source_ids",
    "source_id",
    "_source_id",
    "source_status",
    "units_verified",
    "first_seen_at",
    "_first_seen_at",
    "known_at",
    "published_at",
    "_published_at",
    "fetched_at",
)
METRIC_FIELDS = (
    "date",
    "close",
    "open",
    "high",
    "low",
    "ret1",
    "ret3",
    "ret5",
    "ret20",
    "ma5",
    "ma10",
    "ma20",
    "ma10_prior3",
    "ma20_prior5",
    "median_amount20",
    "amount_ratio",
    "box_high",
    "box_low",
    "close_location",
    "upper_shadow",
    "ma20_distance",
    "above_ma20",
    "atr14",
    "one_price_bar",
    "price_basis",
)
SETUP_FIELDS = (
    "route",
    "status",
    "process_state",
    "signal_date",
    "B",
    "F",
    "breakout_distance",
    "p_date",
    "q_date",
    "p",
    "q",
    "p_high",
    "q_low",
    "rise",
    "pullback_low",
    "pullback_depth",
    "pullback_amount_ratio",
    "pullback_amount_mean",
    "rise_end_amount_mean",
    "confirmation_amount_ratio",
    "pullback_dates",
    "rise_end_dates",
    *SOURCE_FIELDS,
)


def _setup(value):
    result = _pick(value, SETUP_FIELDS)
    rules = value.get("rule_results", [])
    result["passed_rules"] = [
        row.get("rule", row.get("code")) for row in rules if row.get("status") == "pass"
    ]
    result["nonpassing_rules"] = [
        _pick(
            row,
            ("rule", "code", "status", "threshold", "reason_code"),
        )
        for row in rules
        if row.get("status") != "pass"
    ]
    result["rule_sources"] = sorted(
        {row["source"] for row in rules if isinstance(row.get("source"), str)}
    )
    result["rule_source_ids"] = sorted(
        {identifier for row in rules for identifier in row.get("source_ids", [])}
    )
    return result


def _moneyflow(value):
    result = _pick(
        value,
        (
            "status",
            "signal_date",
            "negative_days_5",
            "consecutive_outflow_days",
            "consecutive_outflow_observation_window_days",
            "consecutive_outflow_may_extend_before_window",
            "price_returns",
            "state",
            "action",
            "reason_codes",
            "labels_are_supplier_statistics",
            *SOURCE_FIELDS,
        ),
    )
    result["windows"] = {
        str(n): _pick(
            value.get("windows", {}).get(str(n), {}),
            (
                "status",
                "dates",
                "amount_cny",
                "net_cny",
                "large_order_difference_cny",
                "flow_ratio",
                "large_order_ratio",
                *SOURCE_FIELDS,
            ),
        )
        for n in (1, 3, 5)
    }
    result["ratio_definition"] = "window_net_sum/window_amount_sum; not mean of daily ratios"
    missing = value.get("missing_or_invalid_dates", [])
    if missing:
        result["missing_or_invalid_dates"] = _clean(missing)
    return result


EVENT_FIELDS = (
    "event_id",
    "ts_code",
    "report_date",
    "reason_raw",
    "scope_type",
    "scope_start",
    "scope_end",
    "effective_target_date",
    "active_for_target",
    "scope_status",
    "normalization_checks",
    "trusted_semantics",
    "buy_cny",
    "sell_cny",
    "net_cny",
    "denominator_cny",
    "net_ratio",
    "institution_status",
    "institution_rank_buy_cny",
    "institution_rank_sell_cny",
    "institution_rank_net_cny",
    "institution_rank_ratio",
    "action",
    "reason_codes",
    "historical_evidence",
    *SOURCE_FIELDS,
)


def _event(value):
    result = _pick(value, EVENT_FIELDS)
    if "institutional_sides" in value:
        result["institutional_sides"] = [
            _pick(
                seat,
                (
                    "side",
                    "exalter",
                    "buy",
                    "sell",
                    "institution",
                    "classification_source_id",
                    "seat_id",
                    "response_batch_id",
                    "scope_type",
                    "scope_start",
                    "scope_end",
                    "report_date",
                    *SOURCE_FIELDS,
                ),
            )
            for seat in value["institutional_sides"]
        ]
    return result


def _lhb(value):
    result = _pick(
        value,
        (
            "status",
            "action",
            "reason_codes",
            "institution_status",
            "no_cross_event_netting",
            *SOURCE_FIELDS,
        ),
    )
    events = value.get("events", [])
    # Unresolved nonhistorical events may be important too; do not hide them.
    active = [
        row
        for row in events
        if row.get("active_for_target") is True or row.get("historical_evidence") is not True
    ]
    history = sorted(
        [row for row in events if row not in active],
        key=lambda row: (str(row.get("report_date", "")), str(row.get("event_id", ""))),
        reverse=True,
    )
    kept = active + history[:HISTORY_EVENT_LIMIT]
    result["events"] = [_event(row) for row in kept]
    result["event_count_total"] = len(events)
    result["historical_events_omitted"] = max(0, len(history) - HISTORY_EVENT_LIMIT)
    result["omission_policy"] = (
        "all active or unresolved events retained; older historical evidence summarized"
    )
    result["eligibility_action_preserved"] = value.get("action", "review_required")
    return result


LEVEL_FIELDS = (
    "level_id",
    "lower",
    "upper",
    "center",
    "source_types",
    "formed_at",
    "confirmed_at",
    "band_frozen_at",
    "initial_role",
    "current_role",
    "status",
    "significant",
    "significant_origin",
    "independent_touches",
    "last_touch",
    "historical_tests_use_frozen_band",
    *SOURCE_FIELDS,
)


def _level(value):
    result = _pick(value, LEVEL_FIELDS)
    origins = value.get("origins", [])
    # Route sources first; then newest confirmed origins. Nothing is backfilled.
    key_sources = {"box_upper", "box_lower", "recovery_prior_peak", "recovery_pullback_low"}
    origins = sorted(
        origins,
        key=lambda row: (
            str(row.get("confirmed_at", "")),
            str(row.get("formed_at", "")),
            str(row.get("source_type", "")),
        ),
        reverse=True,
    )
    origins.sort(key=lambda row: row.get("source_type") not in key_sources)
    result["origins"] = [
        _pick(
            row,
            ("price", "source_type", "formed_at", "confirmed_at", "significant", *SOURCE_FIELDS),
        )
        for row in origins[:2]
    ]
    result["origins_omitted"] = max(0, len(origins) - 2)
    return result


def _levels(value, metrics, entry):
    result = _pick(
        value,
        (
            "status",
            "signal_date",
            "observed_days",
            "missing_dates",
            "atr14",
            "dynamic_references",
            "highest_60",
            "latest_confirmed_swing_high",
            "price_basis",
            "historical_support_test_mode",
            *SOURCE_FIELDS,
        ),
    )
    levels = value.get("levels", [])
    by_id = {row.get("level_id"): row for row in levels}
    close = metrics.get("close", value.get("reference_close"))
    actual_close = (
        isinstance(close, (int, float)) and not isinstance(close, bool) and math.isfinite(close)
    )
    supports = [
        row
        for row in levels
        if row.get("status") == "active"
        and row.get("significant")
        and row.get("current_role") in {"support", "potential_support"}
        and isinstance(row.get("upper"), (int, float))
        and not isinstance(row["upper"], bool)
        and math.isfinite(row["upper"])
        and actual_close
        and row["upper"] <= close
    ]
    support = (
        max(supports, key=lambda row: (row["upper"], str(row.get("level_id", ""))))
        if supports
        else None
    )
    pressure_id = entry.get("nearest_resistance_id")
    pressure = by_id.get(pressure_id) if pressure_id is not None else None
    result["closest_support"] = _level(support) if support else None
    result["closest_support_status"] = (
        "identified" if support else "not_identified" if actual_close else "unknown_close"
    )
    result["nearest_resistance_id"] = pressure_id
    result["nearest_significant_pressure"] = _level(pressure) if pressure else None
    result["nearest_pressure_status"] = (
        "identified"
        if pressure
        else "unknown_missing_selected_band"
        if pressure_id
        else "not_identified"
    )
    keys = [
        row
        for row in levels
        if set(row.get("source_types", []))
        & {"box_upper", "box_lower", "recovery_prior_peak", "recovery_pullback_low"}
    ]
    result["route_key_levels"] = [
        {"level_id": row["level_id"], "see": "closest_support"}
        if row is support
        else {"level_id": row["level_id"], "see": "nearest_significant_pressure"}
        if row is pressure
        else _level(row)
        for row in keys
    ]
    result["invalidated_support_count"] = sum(row.get("status") == "invalidated" for row in levels)
    result["full_level_count"] = len(levels)
    result["other_levels_omitted"] = len(
        [row for row in levels if row not in keys and row is not support and row is not pressure]
    )
    return result


def _notices(rows):
    if isinstance(rows, dict):
        source = _pick(rows, SOURCE_FIELDS)
        original_status = rows.get("status")
        rows = rows.get("items", [])
    else:
        source = {}
        original_status = None
    items, truncated, missing_bodies, body_budget = [], 0, 0, NOTICE_BODY_CHARS
    for row in rows[:NOTICE_LIMIT]:
        summary = _pick(
            row,
            (
                "ts_code",
                "title",
                "ann_date",
                "end_date",
                "published_at",
                "type",
                "event_type",
                "p_change_min",
                "p_change_max",
                "net_profit_min",
                "net_profit_max",
                "normalized",
                "_units_verified",
                "coverage_status",
                "url",
                "source_url",
                "document_id",
                "announcement_dates",
                "date_uncertainty",
                "body_first_seen_at",
                "novelty_status",
                "index_only",
                "body_extracted",
                "machine_text_not_independently_verified",
                "risk_flags",
                *SOURCE_FIELDS,
            ),
        )
        body = next(
            (
                row.get(key)
                for key in ("body", "text", "content", "extracted_text", "summary", "change_reason")
                if isinstance(row.get(key), str) and row.get(key)
            ),
            None,
        )
        if body is not None:
            summary["body"] = _clean(body[:body_budget])
            summary["body_chars_total"] = len(body)
            summary["body_truncated"] = len(body) > body_budget
            body_budget -= min(len(body), body_budget)
            truncated += summary["body_truncated"]
        else:
            summary["body_status"] = "unknown_or_structured_record_only"
            missing_bodies += 1
        items.append(summary)
    omitted = max(0, len(rows) - NOTICE_LIMIT)
    incomplete = bool(omitted or truncated or missing_bodies)
    status = "partial" if incomplete else original_status or "as_supplied"
    return {
        **source,
        "items": items,
        "total_count": len(rows),
        "omitted_count": omitted,
        "truncated_body_count": truncated,
        "missing_body_count": missing_bodies,
        "status": status,
        "original_status": original_status,
        "omitted_notice_counter_evidence": [
            _pick(
                row,
                (
                    "ts_code",
                    "title",
                    "risk_flags",
                    "index_only",
                    "body_extracted",
                    "body_first_seen_at",
                    "announcement_dates",
                    *SOURCE_FIELDS,
                ),
            )
            for row in rows[NOTICE_LIMIT:]
            if any(flag.get("action") != "pass" for flag in row.get("risk_flags", []))
        ],
        "unknown_reason": "notice_content_incomplete_requires_review" if incomplete else None,
    }


def compact_program(program: dict) -> dict:
    """Return a fact-only AI view, without mutating the full program artifact."""
    metrics = _pick(program.get("metrics", {}), METRIC_FIELDS)
    entry = _pick(
        program.get("entry", {}),
        (
            "action",
            "status",
            "reference",
            "invalidation",
            "invalidation_raw",
            "entry_low",
            "entry_high",
            "entry_high_before_resistance",
            "entry_high_after_resistance",
            "nearest_resistance_id",
            "nearest_resistance_lower",
            "overhead_room",
            "risk_distance",
            "geometric_rr",
            "entry_mode",
            "entry_check",
            "price_basis",
            "signal_date",
            "reason_codes",
            "r_max",
            "not_an_execution_or_profit_guarantee",
            *SOURCE_FIELDS,
        ),
    )
    notices = _notices(program.get("notices", []))
    pending = bool(program.get("risk_body_pending")) or notices["status"] not in {
        "as_supplied",
        "available",
        "complete",
        "known_empty",
        "ok",
    }
    result = {
        "setup": _setup(program.get("setup", {})),
        "metrics": metrics,
        "moneyflow": _moneyflow(program.get("moneyflow", {})),
        "lhb": _lhb(program.get("lhb", {})),
        "levels": _levels(program.get("levels", {}), metrics, entry),
        "entry": entry,
        "unlock": _pick(
            program.get("unlock", {}),
            (
                "action",
                "status",
                "ratio_total_shares",
                "total_shares",
                "unlock_shares",
                "dates",
                "reason_codes",
                *SOURCE_FIELDS,
            ),
        ),
        "notices": notices,
        "risk_events": _clean(program.get("risk_events", [])),
        "risk_body_pending": pending,
        "compaction": {
            "version": VERSION,
            "full_diagnostics_retained_separately": True,
            "not_a_new_eligibility_evaluation": True,
        },
    }
    if pending:
        result["ai_input_ceiling"] = "watch_only"
    return result


def compact_group(group: dict) -> dict:
    """No members, full daily history, snapshots or filesystem locations in prompts."""
    result = _pick(
        group,
        (
            "group_id",
            "group_code",
            "name",
            "group_type",
            "group_state",
            "status",
            "coverage_status",
            "date",
            "strength",
            "strength_percentile",
            "persistence",
            "share_expansion",
            "priority_support",
            "membership_source",
            "snapshot_at",
            "reason_code",
            *SOURCE_FIELDS,
        ),
    )
    result["metrics"] = _pick(
        group.get("metrics", {}),
        (
            "expected_count",
            "valid_count",
            "coverage",
            "breadth",
            "median_ret1",
            "median_ret3",
            "excess3",
            "limit_count",
            "limit_density",
            "amount_share",
            "market_limit_density",
        ),
    )
    result["member_identity_details_retained_separately"] = True
    return result
