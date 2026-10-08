"""Pure, conservative funds and disclosed-seat rules for article strategy research.

Inputs are one security's immutable records, not a provider client. Explicit
semantic receipts are required before a disclosed event can become a hard gate.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

DEFAULT_CONFIG = {
    "version": "article_risk_levels_v1",
    "high_divergence_ret20": 0.25,
    "high_divergence_amount_ratio": 2.0,
    "high_divergence_upper_shadow": 0.4,
    "high_divergence_close_location": 0.35,
    "large_unlock_ratio": 0.05,
    "large_unlock_days": 5,
    "lhb_net_sell_cny": -30_000_000.0,
    "lhb_net_sell_ratio": -0.05,
    "lhb_institution_sell_cny": -20_000_000.0,
    "lhb_institution_sell_ratio": -0.03,
    "lhb_amount_abs_tolerance_cny": 1.0,
    "lhb_amount_relative_tolerance": 0.0001,
    "lhb_rate_tolerance_percentage_points": 0.1,
    "flow_supportive_1": 0.01,
    "flow_supportive_3": 0.0,
    "large_supportive_3": 0.0,
    "flow_severe_1": -0.08,
    "flow_severe_3": -0.05,
    "large_severe_3": -0.03,
    "flow_severe_negative_days_5": 4,
    "breakout_opposing_ratio": -0.005,
    "flow_divergence_ret5": 0.05,
    "flow_divergence_ratio_5": -0.03,
    "flow_divergence_negative_days_5": 3,
    "price_history_days": 120,
    "pivot_right_left_days": 2,
    "touch_separation_days": 3,
    "level_width_cap_ratio": 0.01,
    "level_width_floor_ratio": 0.003,
    "level_atr_width": 0.25,
    "support_break_amount_ratio": 1.2,
    "role_conversion_closes": 2,
    "significant_touch_count": 2,
    "price_tick": 0.01,
    "invalidation_atr_multiple": 0.5,
    "active_risk_max": 0.08,
    "selective_risk_max": 0.06,
    "active_close_entry_cap": 1.02,
    "selective_close_entry_cap": 1.01,
    "breakout_reference_cap": 1.03,
    "recovery_peak_entry_cap": 1.02,
    "geometric_rr_min": 1.5,
}


def risk_config(config: dict | None = None) -> dict:
    """Copy the frozen defaults; reject accidental or nonfinite threshold changes."""
    result = dict(DEFAULT_CONFIG)
    if config:
        unknown = set(config) - set(result)
        if unknown:
            raise ValueError("Unknown article risk threshold: " + ",".join(sorted(unknown)))
        result.update(config)
    for key, value in result.items():
        if key != "version" and finite(value) is None:
            raise ValueError("Nonfinite article risk threshold: " + key)
    integer_keys = (
        "price_history_days",
        "pivot_right_left_days",
        "touch_separation_days",
        "role_conversion_closes",
        "significant_touch_count",
        "large_unlock_days",
        "flow_severe_negative_days_5",
        "flow_divergence_negative_days_5",
    )
    if any(result[key] < 1 or int(result[key]) != result[key] for key in integer_keys):
        raise ValueError("Invalid article integer window threshold")
    if result["price_tick"] <= 0 or result["price_history_days"] < 15:
        raise ValueError("Invalid article price threshold")
    if not all(0 < result[key] < 1 for key in ("active_risk_max", "selective_risk_max")):
        raise ValueError("Invalid article risk distance cap")
    if not 0 < result["level_width_floor_ratio"] <= result["level_width_cap_ratio"]:
        raise ValueError("Invalid article band width")
    if result["geometric_rr_min"] <= 0 or result["invalidation_atr_multiple"] < 0:
        raise ValueError("Invalid article geometry threshold")
    return result


def config_hash(config: dict | None = None) -> str:
    return stable_id(risk_config(config))


def stable_id(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def finite(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def day_key(value: object) -> str:
    if value is None:
        return ""
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def dated_rows(rows: list[dict], cutoff: str) -> tuple[dict[str, dict], list[str]]:
    """Never silently choose one of two contradictory records for a trading day."""
    result, conflicts = {}, []
    for row in rows:
        day = day_key(row.get("date", row.get("trade_date", "")))
        if not day or day > cutoff:
            continue
        if day in result and result[day] != row:
            conflicts.append(day)
        else:
            result[day] = row
    return result, sorted(set(conflicts))


def calendar_window(trading_dates: list[str], signal_date: str, n: int) -> list[str]:
    dates = sorted({day_key(day) for day in trading_dates})
    signal = day_key(signal_date)
    if signal not in dates:
        return []
    index = dates.index(signal)
    return dates[max(0, index - n + 1) : index + 1]


def high_divergence_flag(metrics: dict, config: dict | None = None) -> bool | None:
    cfg = risk_config(config)
    values = {
        key: finite(metrics.get(key))
        for key in ("ret20", "amount_ratio", "upper_shadow", "close_location")
    }
    if any(value is None for value in values.values()):
        return None
    return (
        values["ret20"] >= cfg["high_divergence_ret20"]
        and values["amount_ratio"] >= cfg["high_divergence_amount_ratio"]
        and (
            values["upper_shadow"] >= cfg["high_divergence_upper_shadow"]
            or values["close_location"] <= cfg["high_divergence_close_location"]
        )
    )


def evaluate_moneyflow(
    rows: list[dict],
    bars: list[dict],
    trading_dates: list[str],
    *,
    signal_date: str,
    setup: dict | str,
    support_broken: bool = False,
    high_divergence: bool = False,
    source_status: str = "available",
    units_verified: bool = False,
    config: dict | None = None,
) -> dict:
    """Compute ratio-of-sums on calendar windows, using raw moneyflow 万元 fields."""
    cfg, signal = risk_config(config), day_key(signal_date)
    by_day, conflicts = dated_rows(rows, signal)
    prices, price_conflicts = dated_rows(bars, signal)
    expected = calendar_window(trading_dates, signal, 5)
    observations, invalid = {}, []
    for day in expected:
        row, bar = by_day.get(day), prices.get(day)
        if row is None or bar is None:
            invalid.append(day)
            continue
        amount = finite(bar.get("amount_cny"))
        net = finite(row.get("net_mf_amount"))
        parts = [
            finite(row.get(key))
            for key in ("buy_lg_amount", "buy_elg_amount", "sell_lg_amount", "sell_elg_amount")
        ]
        if (
            amount is None
            or amount < 0
            or net is None
            or any(value is None or value < 0 for value in parts)
            or day in conflicts
            or day in price_conflicts
        ):
            invalid.append(day)
            continue
        converted_net = finite(net * 10_000)
        converted_large = finite((parts[0] + parts[1] - parts[2] - parts[3]) * 10_000)
        if converted_net is None or converted_large is None:
            invalid.append(day)
            continue
        observations[day] = {
            "date": day,
            "amount_cny": amount,
            "net_cny": converted_net,
            "large_order_difference_cny": converted_large,
        }
    windows = {}
    for n in (1, 3, 5):
        days = calendar_window(trading_dates, signal, n)
        complete = (
            source_status == "available"
            and units_verified
            and len(days) == n
            and all(day in observations for day in days)
        )
        amount = finite(sum(observations[day]["amount_cny"] for day in days)) if complete else None
        complete = complete and amount is not None and amount > 0
        net = finite(sum(observations[day]["net_cny"] for day in days)) if complete else None
        large = (
            finite(sum(observations[day]["large_order_difference_cny"] for day in days))
            if complete
            else None
        )
        complete = complete and net is not None and large is not None
        flow_ratio = finite(net / amount) if complete else None
        large_ratio = finite(large / amount) if complete else None
        complete = complete and flow_ratio is not None and large_ratio is not None
        windows[str(n)] = {
            "status": "complete" if complete else "unknown",
            "dates": days,
            "amount_cny": amount if complete else None,
            "net_cny": net,
            "large_order_difference_cny": large,
            "flow_ratio": flow_ratio if complete else None,
            "large_order_ratio": large_ratio if complete else None,
        }
    complete = all(item["status"] == "complete" for item in windows.values())
    negative = sum(observations[day]["net_cny"] < 0 for day in expected) if complete else None
    consecutive = None
    if complete:
        consecutive = 0
        for day in reversed(expected):
            if observations[day]["net_cny"] >= 0:
                break
            consecutive += 1
    returns = {}
    for n in (1, 3, 5):
        days = calendar_window(trading_dates, signal, n + 1)
        endpoints = [finite(prices.get(day, {}).get("close")) for day in days]
        returns[str(n)] = (
            endpoints[-1] / endpoints[0] - 1
            if len(days) == n + 1
            and all(x is not None and x > 0 for x in endpoints)
            and not any(day in price_conflicts for day in days)
            else None
        )
    result = {
        "status": "complete" if complete else "unknown",
        "signal_date": signal,
        "source_status": source_status,
        "units_verified": units_verified,
        "windows": windows,
        "negative_days_5": negative,
        "consecutive_outflow_days": consecutive,
        "consecutive_outflow_observation_window_days": 5,
        "consecutive_outflow_may_extend_before_window": bool(complete and consecutive == 5),
        "price_returns": returns,
        "observations": list(observations.values()) if units_verified else [],
        "missing_or_invalid_dates": sorted(set(invalid + conflicts + price_conflicts)),
        "state": "unknown",
        "action": "review_required",
        "reason_codes": [],
        "rule_results": [],
        "labels_are_supplier_statistics": True,
    }
    for n in (1, 3, 5):
        result[f"net_ratio_{n}"] = windows[str(n)]["flow_ratio"]
        result[f"large_ratio_{n}"] = windows[str(n)]["large_order_ratio"]
    if not complete:
        result["reason_codes"] = ["moneyflow_incomplete_or_invalid"]
        return result
    f1, f3, f5 = [windows[str(n)]["flow_ratio"] for n in (1, 3, 5)]
    g1, g3 = [windows[str(n)]["large_order_ratio"] for n in (1, 3)]
    severe = (
        f1 <= cfg["flow_severe_1"]
        and f3 <= cfg["flow_severe_3"]
        and g3 <= cfg["large_severe_3"]
        and negative >= cfg["flow_severe_negative_days_5"]
    )
    route = setup if isinstance(setup, str) else setup.get("route", setup.get("setup"))
    opposing = (
        route == "base_breakout"
        and f1 <= cfg["breakout_opposing_ratio"]
        and g1 <= cfg["breakout_opposing_ratio"]
    )
    ret5 = returns["5"]
    mixed = (
        ret5 is not None
        and ret5 >= cfg["flow_divergence_ret5"]
        and f5 <= cfg["flow_divergence_ratio_5"]
        and negative >= cfg["flow_divergence_negative_days_5"]
    )
    supportive = (
        f1 >= cfg["flow_supportive_1"]
        and f3 >= cfg["flow_supportive_3"]
        and g3 >= cfg["large_supportive_3"]
    )
    result.update({"state": "supportive" if supportive else "neutral", "action": "pass"})
    for code, triggered in (
        ("strong_persistent_outflow", severe),
        ("breakout_opposing_funds", opposing),
        ("price_funds_divergence", mixed),
        ("funds_supportive", supportive),
    ):
        result["rule_results"].append(
            {
                "code": code,
                "triggered": triggered,
                "status": "pass"
                if (triggered if code == "funds_supportive" else not triggered)
                else "fail",
                "values": {
                    "net_ratio_1": f1,
                    "net_ratio_3": f3,
                    "net_ratio_5": f5,
                    "large_ratio_1": g1,
                    "large_ratio_3": g3,
                    "negative_days_5": negative,
                    "ret5": ret5,
                },
                "thresholds": {
                    key: value
                    for key, value in cfg.items()
                    if key.startswith(
                        ("flow_", "large_supportive_", "large_severe_", "breakout_opposing_")
                    )
                },
                "unit": "ratio_and_trading_days",
                "dates": expected,
                "source": "TuShare moneyflow supplier statistics plus normalized daily amount",
            }
        )
        if triggered:
            result["reason_codes"].append(code)
    if severe:
        result.update({"state": "persistent_outflow", "action": "watch_only"})
        if support_broken or high_divergence:
            result["action"] = "exclude"
            result["reason_codes"].append("severe_outflow_with_structure_risk")
    elif opposing:
        result.update({"state": "opposing", "action": "watch_only"})
    elif mixed:
        result["state"] = "mixed"
    if high_divergence and result["action"] == "pass":
        result["action"] = "watch_only"
        result["reason_codes"].append("high_volume_high_position_divergence")
    return result


def qualify_lhb_unit_evidence(
    top_list_row: dict,
    daily_row: dict,
    *,
    official_source_id: str | None,
    official_unit: str | None,
    official_scope_start: str | None,
    official_scope_end: str | None,
    top_inst_unit_confirmed: bool,
    config: dict | None = None,
) -> dict:
    """Explicit single-day cross-check, never a magnitude-based unit guess.

    The adapter must have an issuer/exchange receipt that confirms both unit and
    scope. A matching daily denominator on its own does not validate semantics.
    """
    cfg = risk_config(config)
    report = day_key(top_list_row.get("trade_date", top_list_row.get("report_date", "")))
    daily_day = day_key(daily_row.get("trade_date", daily_row.get("date", "")))
    stated, amount = finite(top_list_row.get("amount")), finite(daily_row.get("amount"))
    tolerance = max(
        cfg["lhb_amount_abs_tolerance_cny"],
        (amount * 1000 if amount is not None else 0) * cfg["lhb_amount_relative_tolerance"],
    )
    checks = {
        "official_receipt": bool(official_source_id),
        "official_unit_cny": official_unit == "CNY",
        "scope_single_day_verified": (
            report != ""
            and report == daily_day == day_key(official_scope_start) == day_key(official_scope_end)
        ),
        "top_inst_unit_confirmed": top_inst_unit_confirmed,
        "daily_denominator_matches": (
            stated is not None
            and stated > 0
            and amount is not None
            and amount > 0
            and abs(stated - amount * 1000) <= tolerance
        ),
    }
    return {
        "units_verified": all(checks.values()),
        "checks": checks,
        "official_source_id": official_source_id,
        "amount_tolerance_cny": tolerance,
        "unit": "CNY" if all(checks.values()) else "unknown",
    }


def _seen_by(value: object, cutoff_at: str) -> bool:
    try:
        seen, cutoff = datetime.fromisoformat(str(value)), datetime.fromisoformat(cutoff_at)
        return seen.tzinfo is not None and cutoff.tzinfo is not None and seen <= cutoff
    except (ValueError, TypeError):
        return False


def _publication_before_seen(row: dict) -> bool:
    try:
        published = datetime.fromisoformat(str(row.get("published_at")))
        first_seen = datetime.fromisoformat(str(row.get("first_seen_at")))
        return (
            published.tzinfo is not None
            and first_seen.tzinfo is not None
            and published <= first_seen
        )
    except (ValueError, TypeError):
        return False


def _institution_rows(row: dict, seats: list[dict]) -> tuple[list[dict], str]:
    """Use one canonical disclosed ranking response, never concatenate reasons."""
    linked = []
    event_id = row.get("report_event_id")

    def contradicts_event(seat: dict) -> bool:
        # A convenience event ID must never erase explicit subject, date or
        # statistical-scope contradictions. Missing fields may use an adapter's
        # verified association receipt; explicit unknown/false fields may not.
        if "report_event_id" in seat and seat["report_event_id"] != event_id:
            return True
        for key in ("ts_code", "scope_type"):
            if key in seat and seat[key] != row.get(key):
                return True
        for key in ("scope_start", "scope_end"):
            if key in seat and day_key(seat[key]) != day_key(row.get(key)):
                return True
        if "report_date" in seat or "trade_date" in seat:
            if day_key(seat.get("report_date", seat.get("trade_date"))) != day_key(
                row.get("report_date", row.get("trade_date"))
            ):
                return True
        return "scope_verified" in seat and seat["scope_verified"] is not True

    conflicting_association = False
    for seat in seats:
        same_event = event_id is not None and seat.get("report_event_id") == event_id
        same_scope = (
            seat.get("ts_code") == row.get("ts_code")
            and seat.get("scope_type") == row.get("scope_type")
            and day_key(seat.get("scope_start")) == day_key(row.get("scope_start"))
            and day_key(seat.get("scope_end")) == day_key(row.get("scope_end"))
            and day_key(seat.get("report_date", seat.get("trade_date")))
            == day_key(row.get("report_date", row.get("trade_date")))
            and seat.get("scope_verified") is True
        )
        if same_event or same_scope:
            conflicting_association = conflicting_association or contradicts_event(seat)
            linked.append(seat)
    if row.get("institution_rows") is not None:
        linked = list(row["institution_rows"])
        conflicting_association = any(contradicts_event(item) for item in linked)
    if (
        conflicting_association
        or row.get("institutions_complete") is not True
        or row.get("institution_scope_verified") is not True
    ):
        return linked, "unknown"
    # Explicit response identities distinguish genuine repeated same-name rows
    # from a replayed request. An absent identity is not silently deduplicated.
    batches = {item.get("response_batch_id") for item in linked if item.get("response_batch_id")}
    if len(batches) > 1:
        canonical = row.get("canonical_institution_batch_id")
        if canonical not in batches:
            return linked, "unknown"
        linked = [item for item in linked if item.get("response_batch_id") == canonical]
    for item in linked:
        side, buy, sell = item.get("side"), finite(item.get("buy")), finite(item.get("sell"))
        classified = item.get("institution")
        if (
            str(side) not in {"0", "1"}
            or buy is None
            or sell is None
            or buy < 0
            or sell < 0
            or not isinstance(classified, bool)
            or not item.get("classification_source_id")
        ):
            return linked, "unknown"
    return linked, "disclosed" if any(item["institution"] for item in linked) else "none_disclosed"


def normalize_lhb_events(
    rows: list[dict],
    institution_rows: list[dict],
    *,
    target_date: str,
    cutoff_at: str,
    source_status: str = "available",
    config: dict | None = None,
) -> dict:
    """Normalize independent events; never offset overlapping scopes or repeat reasons."""
    cfg, target = risk_config(config), day_key(target_date)
    if source_status != "available":
        return {
            "status": "unknown",
            "action": "review_required",
            "events": [],
            "reason_codes": ["lhb_source_incomplete"],
        }
    if not rows:
        return {
            "status": "not_listed",
            "action": "pass",
            "events": [],
            "institution_status": "not_listed",
            "reason_codes": [],
        }
    events, merge_keys = [], {}
    for raw in sorted(rows, key=lambda item: stable_id(item)):
        row = dict(raw)
        report_date = day_key(row.get("report_date", row.get("trade_date", "")))
        scope_start, scope_end = (
            day_key(row.get("scope_start", "")),
            day_key(row.get("scope_end", "")),
        )
        fields = {
            key: finite(row.get(key))
            for key in ("l_buy", "l_sell", "net_amount", "amount", "l_amount", "net_rate")
        }
        numeric = (
            all(value is not None for value in fields.values())
            and fields["amount"] > 0
            and fields["l_buy"] >= 0
            and fields["l_sell"] >= 0
            and fields["l_amount"] >= 0
        )
        tolerance = (
            max(
                cfg["lhb_amount_abs_tolerance_cny"],
                max(fields["l_buy"], fields["l_sell"]) * cfg["lhb_amount_relative_tolerance"],
            )
            if numeric
            else None
        )
        net_ratio = fields["net_amount"] / fields["amount"] if numeric else None
        numeric = numeric and finite(net_ratio) is not None
        identities = {
            "finite_nonnegative_components": numeric,
            "net_equals_buy_minus_sell": numeric
            and abs(fields["net_amount"] - fields["l_buy"] + fields["l_sell"]) <= tolerance,
            "listed_amount_equals_two_sides": numeric
            and abs(fields["l_amount"] - fields["l_buy"] - fields["l_sell"]) <= tolerance,
            "net_rate_matches_percentage": numeric
            and abs(fields["net_rate"] - 100 * net_ratio)
            <= cfg["lhb_rate_tolerance_percentage_points"],
        }
        scope_known = (
            row.get("scope_verified") is True
            and row.get("scope_type") in {"single_day", "multi_day"}
            and bool(scope_start and scope_end)
            and scope_start <= scope_end <= report_date
            and (row.get("scope_type") != "single_day" or scope_start == scope_end)
        )
        known = (
            _seen_by(row.get("first_seen_at"), cutoff_at)
            and _seen_by(row.get("published_at"), cutoff_at)
            and _publication_before_seen(row)
        )
        effective = day_key(row.get("effective_target_date", ""))
        timing = (
            row.get("newness_verified") is True
            and bool(effective)
            and known
            and effective >= report_date
            and not (row.get("historical_import") is True and effective == target)
        )
        trusted = (
            numeric
            and all(identities.values())
            and scope_known
            and timing
            and row.get("units_verified") is True
            and row.get("complete") is True
        )
        seats, institution_status = _institution_rows(row, institution_rows)
        inst_buy = (
            sum(
                finite(item["buy"])
                for item in seats
                if str(item["side"]) == "0" and item["institution"]
            )
            if institution_status != "unknown"
            else None
        )
        inst_sell = (
            sum(
                finite(item["sell"])
                for item in seats
                if str(item["side"]) == "1" and item["institution"]
            )
            if institution_status != "unknown"
            else None
        )
        inst_buy, inst_sell = finite(inst_buy), finite(inst_sell)
        if inst_buy is None or inst_sell is None:
            institution_status = "unknown"
            inst_buy = inst_sell = None
        inst_net = finite(inst_buy - inst_sell) if inst_buy is not None else None
        inst_ratio = (
            finite(inst_net / fields["amount"]) if inst_net is not None and numeric else None
        )
        if numeric and (inst_net is None or inst_ratio is None):
            institution_status = "unknown"
            inst_buy = inst_sell = inst_net = inst_ratio = None
        active = trusted and effective == target
        action, reasons = "pass", []
        if not trusted:
            action, reasons = "review_required", ["lhb_semantics_or_timing_unverified"]
        elif active:
            net_amount_trigger = fields["net_amount"] <= cfg["lhb_net_sell_cny"]
            net_ratio_trigger = net_ratio <= cfg["lhb_net_sell_ratio"]
            if net_amount_trigger and net_ratio_trigger:
                action, reasons = "exclude", ["lhb_severe_disclosed_net_sell"]
            elif net_amount_trigger or net_ratio_trigger:
                action, reasons = "watch_only", ["lhb_disclosed_sell_warning"]
            if institution_status == "unknown" and action != "exclude":
                action = "review_required"
                reasons.append("lhb_institution_scope_or_classification_unknown")
            elif inst_net is not None and (
                inst_net <= cfg["lhb_institution_sell_cny"]
                and inst_ratio <= cfg["lhb_institution_sell_ratio"]
            ):
                action = "exclude"
                reasons.append("lhb_institution_rank_side_severe_sell")
        event = {
            "event_id": row.get("report_event_id") or stable_id(row)[:20],
            "ts_code": row.get("ts_code"),
            "report_date": report_date,
            "reason_raw": [str(row.get("reason_raw", row.get("reason", "")))],
            "scope_type": row.get("scope_type", "unknown"),
            "scope_start": scope_start or None,
            "scope_end": scope_end or None,
            "source_ids": sorted(set(row.get("source_ids", []))),
            "first_seen_at": row.get("first_seen_at"),
            "published_at": row.get("published_at"),
            "effective_target_date": effective or None,
            "active_for_target": active,
            "scope_status": "verified" if scope_known else "unknown",
            "normalization_checks": identities,
            "trusted_semantics": bool(trusted),
            "buy_cny": fields["l_buy"],
            "sell_cny": fields["l_sell"],
            "net_cny": fields["net_amount"],
            "denominator_cny": fields["amount"],
            "net_ratio": net_ratio,
            "institution_status": institution_status,
            "institution_rank_buy_cny": inst_buy,
            "institution_rank_sell_cny": inst_sell,
            "institution_rank_net_cny": inst_net,
            "institution_rank_ratio": inst_ratio,
            "institutional_sides": seats,
            "action": action,
            "reason_codes": reasons,
            "historical_evidence": trusted and effective < target,
        }
        # Identical amounts alone are insufficient. Exact scope and canonical
        # seat multiset must both be verified; real same-name rows remain present.
        merge_key = None
        if trusted and institution_status != "unknown":
            seat_values = sorted(
                [
                    {
                        key: item.get(key)
                        for key in ("side", "exalter", "buy", "sell", "institution", "seat_id")
                    }
                    for item in seats
                ],
                key=stable_id,
            )
            merge_key = stable_id(
                [
                    event["ts_code"],
                    report_date,
                    scope_start,
                    scope_end,
                    row["scope_type"],
                    fields,
                    seat_values,
                    effective,
                ]
            )
        if merge_key is not None and merge_key in merge_keys:
            prior = events[merge_keys[merge_key]]
            prior["reason_raw"] = sorted(set(prior["reason_raw"] + event["reason_raw"]))
            prior["source_ids"] = sorted(set(prior["source_ids"] + event["source_ids"]))
        else:
            if merge_key is not None:
                merge_keys[merge_key] = len(events)
            events.append(event)
    precedence = {"pass": 0, "watch_only": 1, "review_required": 2, "exclude": 3}
    action = max((event["action"] for event in events), key=precedence.__getitem__)
    return {
        "status": "complete" if all(e["trusted_semantics"] for e in events) else "unknown",
        "action": action,
        "events": sorted(events, key=lambda e: (e["report_date"], e["event_id"])),
        "reason_codes": sorted({code for event in events for code in event["reason_codes"]}),
        "no_cross_event_netting": True,
    }


def evaluate_unlock_risk(
    rows: list[dict],
    trading_dates: list[str],
    *,
    signal_date: str,
    total_shares: float | None,
    source_status: str = "available",
    config: dict | None = None,
) -> dict:
    """Actual unlock volume / total shares; a float-share ratio is not interchangeable."""
    cfg, total = risk_config(config), finite(total_shares)
    dates = [
        day_key(day) for day in sorted(set(trading_dates)) if day_key(day) > day_key(signal_date)
    ]
    days = dates[: int(cfg["large_unlock_days"])]
    if (
        source_status != "available"
        or total is None
        or total <= 0
        or len(days) < cfg["large_unlock_days"]
    ):
        return {"action": "review_required", "status": "unknown", "ratio_total_shares": None}
    relevant = [row for row in rows if day_key(row.get("float_date")) in days]
    volumes = [finite(row.get("float_share")) for row in relevant]
    if any(value is None or value < 0 for value in volumes):
        return {"action": "review_required", "status": "unknown", "ratio_total_shares": None}
    # Input float_share is normalized to shares by the source adapter.
    ratio = sum(volumes) / total
    return {
        "action": "watch_only" if ratio >= cfg["large_unlock_ratio"] else "pass",
        "status": "complete",
        "ratio_total_shares": ratio,
        "dates": days,
        "reason_codes": ["large_unlock"] if ratio >= cfg["large_unlock_ratio"] else [],
    }
