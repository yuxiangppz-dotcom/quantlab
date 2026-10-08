"""Point-in-time horizontal bands and frozen open-reference research scenarios."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from statistics import median

from quantlab.scout.article_risks import (
    calendar_window,
    dated_rows,
    day_key,
    finite,
    risk_config,
    stable_id,
)


def _valid_bar(row: dict) -> bool:
    values = [finite(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None or value <= 0 for value in values):
        return False
    op, high, low, close = values
    factor = finite(row.get("adj_factor", 1.0))
    return (
        high >= max(op, close)
        and low <= min(op, close)
        and high >= low
        and factor is not None
        and factor > 0
    )


def _ordered_bars(
    bars: list[dict], dates: list[str], signal_date: str, count: int
) -> tuple[list, list]:
    expected = calendar_window(dates, signal_date, count)
    indexed, conflicts = dated_rows(bars, day_key(signal_date))
    missing = [day for day in expected if day not in indexed or not _valid_bar(indexed[day])]
    missing += [day for day in conflicts if day in expected]
    rows = [
        {
            **indexed[day],
            "date": day,
            **{key: finite(indexed[day][key]) for key in ("open", "high", "low", "close")},
        }
        for day in expected
        if day in indexed and _valid_bar(indexed[day])
    ]
    return rows, sorted(set(missing))


def _atr14(rows: list[dict]) -> float | None:
    if len(rows) < 15:
        return None
    values = []
    for index in range(len(rows) - 14, len(rows)):
        row, prior = rows[index], rows[index - 1]
        values.append(
            max(
                row["high"] - row["low"],
                abs(row["high"] - prior["close"]),
                abs(row["low"] - prior["close"]),
            )
        )
    return sum(values) / 14


def level_epsilon(price: float, atr14: float, config: dict | None = None) -> float:
    cfg = risk_config(config)
    return max(
        cfg["price_tick"],
        min(
            cfg["level_width_cap_ratio"] * price,
            max(cfg["level_width_floor_ratio"] * price, cfg["level_atr_width"] * atr14),
        ),
    )


def merge_level_candidates(
    candidates: list[dict], atr14: float, config: dict | None = None
) -> list[dict]:
    """Greedy ascending clusters with bounded total center span, never chained neighbors."""
    cfg = risk_config(config)
    unique = {stable_id(row): dict(row) for row in candidates}
    ordered = sorted(unique.values(), key=lambda item: (item["price"], stable_id(item)))
    groups = []
    for candidate in ordered:
        if not groups:
            groups.append([candidate])
            continue
        test = groups[-1] + [candidate]
        centers = [item["price"] for item in test]
        midpoint = median(centers)
        if max(centers) - min(centers) <= 2 * level_epsilon(midpoint, atr14, cfg) + 1e-12:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    result = []
    for origins in groups:
        centers = [item["price"] for item in origins]
        center = median(centers)
        lower = min(item["price"] - level_epsilon(item["price"], atr14, cfg) for item in origins)
        upper = max(item["price"] + level_epsilon(item["price"], atr14, cfg) for item in origins)
        types = sorted({item["source_type"] for item in origins})
        result.append(
            {
                "level_id": stable_id(origins)[:20],
                "lower": lower,
                "upper": upper,
                "center": center,
                "source_types": types,
                "origins": origins,
                "formed_at": min(item["formed_at"] for item in origins),
                # Current merged bands cannot be used before every component was known.
                "confirmed_at": max(item["confirmed_at"] for item in origins),
                "significant_origin": any(item.get("significant", False) for item in origins),
                "independent_price_evidence_count": len(
                    {(item["formed_at"], item["price"]) for item in origins}
                ),
                "band_build_rule": "ascending_total_center_span_original_epsilons",
            }
        )
    return result


def evaluate_frozen_level(
    level: dict,
    bars: list[dict],
    trading_dates: list[str],
    *,
    signal_date: str,
    config: dict | None = None,
) -> dict:
    """Evaluate only this frozen band; never recompute it from future extrema.

    Historical support tests need the band that was available before that bar.
    Structural confirmed_at and band_frozen_at are distinct. Bars on/before
    their maximum cannot become prior support-test evidence. Current ATR must
    never silently rebuild the boundaries of a historically frozen band.
    """
    cfg, signal = risk_config(config), day_key(signal_date)
    rows, missing = _ordered_bars(bars, trading_dates, signal, len(trading_dates))
    lower, upper, center = [finite(level.get(key)) for key in ("lower", "upper", "center")]
    confirmed = day_key(level.get("confirmed_at", ""))
    band_known = day_key(level.get("band_frozen_at", confirmed))
    output = {
        **level,
        "touch_events": [],
        "independent_touches": 0,
        "last_touch": None,
        "status": "unknown",
        "current_role": "unknown",
        "role_events": [],
    }
    if (
        missing
        or not rows
        or not confirmed
        or confirmed > signal
        or not band_known
        or band_known > signal
        or lower is None
        or upper is None
        or center is None
        or not 0 < lower <= center <= upper
    ):
        output["reason_code"] = "level_history_or_band_invalid"
        return output
    indexed = {row["date"]: index for index, row in enumerate(rows)}
    known_index = indexed.get(max(confirmed, band_known))
    if known_index is None:
        output["reason_code"] = "level_confirmation_not_in_history"
        return output
    known_close = rows[known_index]["close"]
    original_role = level.get("initial_role") or (
        "support" if known_close > upper else "resistance" if known_close < lower else "contested"
    )
    role, above_count, below_count, last_touch_index = original_role, 0, 0, None
    broken, converted, tested_after_conversion = False, False, False
    for index in range(known_index + 1, len(rows)):
        row = rows[index]
        above_count = above_count + 1 if row["close"] > upper else 0
        below_count = below_count + 1 if row["close"] < lower else 0
        baseline = [finite(item.get("amount_cny")) for item in rows[max(0, index - 20) : index]]
        amount = finite(row.get("amount_cny"))
        amount_ratio = (
            amount / median(baseline)
            if len(baseline) == 20
            and all(x is not None and x >= 0 for x in baseline)
            and amount is not None
            and amount >= 0
            and median(baseline) > 0
            else None
        )
        if role == "resistance" and above_count >= cfg["role_conversion_closes"]:
            role, converted = "potential_support", True
            output["role_events"].append({"date": row["date"], "type": "two_closes_above_band"})
        if (
            role in {"support", "potential_support"}
            and not broken
            and (
                below_count >= cfg["role_conversion_closes"]
                or (
                    row["close"] < lower
                    and amount_ratio is not None
                    and amount_ratio >= cfg["support_break_amount_ratio"]
                )
            )
        ):
            broken = True
            output["role_events"].append(
                {
                    "date": row["date"],
                    "type": "support_invalidated",
                    "amount_ratio": amount_ratio,
                    "band_low_at_test": lower,
                    "band_high_at_test": upper,
                }
            )
        intersects = row["low"] <= upper and row["high"] >= lower
        valid = (role in {"support", "potential_support"} and row["close"] >= center) or (
            role == "resistance" and row["close"] <= center
        )
        if (
            intersects
            and valid
            and not broken
            and (
                last_touch_index is None or index - last_touch_index >= cfg["touch_separation_days"]
            )
        ):
            output["touch_events"].append(
                {
                    "date": row["date"],
                    "close": row["close"],
                    "role_at_touch": role,
                    "level_known_since": max(confirmed, band_known),
                    "band_low_at_test": lower,
                    "band_high_at_test": upper,
                }
            )
            last_touch_index = index
            if converted:
                tested_after_conversion = True
    close = rows[-1]["close"]
    current = "support" if close > upper else "resistance" if close < lower else "contested"
    if converted and current == "support" and not tested_after_conversion:
        current = "potential_support"
    output.update(
        {
            "status": "invalidated" if broken else "active",
            "current_role": current,
            "initial_role": original_role,
            "independent_touches": len(output["touch_events"]),
            "last_touch": output["touch_events"][-1]["date"] if output["touch_events"] else None,
            "significant": bool(level.get("significant_origin"))
            or len(output["touch_events"]) >= cfg["significant_touch_count"],
            "historical_tests_use_frozen_band": True,
            "band_frozen_at": band_known,
        }
    )
    return output


def compute_price_levels(
    bars: list[dict],
    trading_dates: list[str],
    *,
    signal_date: str,
    setup: dict,
    config: dict | None = None,
) -> dict:
    """Generate levels from signal-anchored, adjusted OHLC; ignore later injected bars."""
    cfg, signal = risk_config(config), day_key(signal_date)
    rows, missing = _ordered_bars(bars, trading_dates, signal, int(cfg["price_history_days"]))
    complete = len(rows) == cfg["price_history_days"] and not missing
    atr = _atr14(rows) if not missing else None
    result = {
        "status": "complete" if complete else "unknown",
        "signal_date": signal,
        "missing_dates": missing,
        "observed_days": len(rows),
        "atr14": atr,
        "levels": [],
        "dynamic_references": {},
        "highest_60": None,
        "latest_confirmed_swing_high": None,
        "price_basis": "signal_anchored_adjusted",
        "historical_support_test_mode": "frozen_band_after_confirmation_only",
    }
    if not rows or atr is None:
        return result
    candidates, gaps, swings = [], [], []

    def add(price: float | None, source: str, formed: str, confirmed: str, significant: bool):
        price = finite(price)
        if price is not None and price > 0:
            candidates.append(
                {
                    "price": price,
                    "source_type": source,
                    "formed_at": formed,
                    "confirmed_at": confirmed,
                    "significant": significant,
                }
            )

    wing = int(cfg["pivot_right_left_days"])
    for index in range(wing, len(rows) - wing):
        row = rows[index]
        others = rows[index - wing : index] + rows[index + 1 : index + wing + 1]
        confirmed = rows[index + wing]["date"]
        if all(row["high"] > other["high"] for other in others):
            add(row["high"], "swing_high", row["date"], confirmed, True)
            swings.append({"date": row["date"], "confirmed_at": confirmed, "price": row["high"]})
        if all(row["low"] < other["low"] for other in others):
            add(row["low"], "swing_low", row["date"], confirmed, True)
    if swings:
        result["latest_confirmed_swing_high"] = swings[-1]
    for n in (20, 60, 120):
        if len(rows) < n:
            continue
        window = rows[-n:]
        high = max(row["high"] for row in window)
        low = min(row["low"] for row in window)
        high_at = max(row["date"] for row in window if row["high"] == high)
        low_at = max(row["date"] for row in window if row["low"] == low)
        add(high, f"rolling_high_{n}", high_at, signal, False)
        add(low, f"rolling_low_{n}", low_at, signal, False)
        if n == 60:
            result["highest_60"] = {"date": high_at, "price": high, "known_at": signal}
    route = setup.get("route", setup.get("setup"))
    if route == "base_breakout":
        # B/F were computed with t excluded by the route engine.
        add(setup.get("B"), "box_upper", day_key(setup.get("box_high_date", signal)), signal, True)
        add(setup.get("F"), "box_lower", day_key(setup.get("box_low_date", signal)), signal, True)
    elif route == "pullback_recovery":
        p_day, p = day_key(setup.get("p", "")), None
        p = next((row for row in rows if row["date"] == p_day), None)
        if p:
            add(p["high"], "recovery_prior_peak", p_day, signal, True)
        add(
            setup.get("pullback_low"),
            "recovery_pullback_low",
            day_key(setup.get("pullback_low_date", signal)),
            signal,
            True,
        )
    for index in range(1, len(rows)):
        row, previous, future = rows[index], rows[index - 1], rows[index + 1 :]
        if row["low"] > previous["high"]:
            lower, upper = previous["high"], row["low"]
            if future and min(item["low"] for item in future) <= lower:
                continue
            if future:
                upper = min(upper, min(item["low"] for item in future))
            source = "unfilled_up_gap"
        elif row["high"] < previous["low"]:
            lower, upper = row["high"], previous["low"]
            if future and max(item["high"] for item in future) >= upper:
                continue
            if future:
                lower = max(lower, max(item["high"] for item in future))
            source = "unfilled_down_gap"
        else:
            continue
        origin = {
            "price": (lower + upper) / 2,
            "source_type": source,
            "formed_at": row["date"],
            "confirmed_at": row["date"],
            "significant": True,
        }
        gaps.append(
            {
                "level_id": stable_id(origin)[:20],
                "lower": lower,
                "upper": upper,
                "center": (lower + upper) / 2,
                "source_types": [source],
                "origins": [origin],
                "formed_at": row["date"],
                "confirmed_at": row["date"],
                "band_frozen_at": signal,
                "significant_origin": True,
                "independent_price_evidence_count": 1,
                "band_build_rule": "actual_unfilled_gap",
                "gap_interval_original": [previous["high"], row["low"]]
                if source == "unfilled_up_gap"
                else [row["high"], previous["low"]],
            }
        )
    merged = merge_level_candidates(candidates, atr, cfg)
    # Widths use today's ATR and merged boundaries use today's full candidate
    # set. The pivot confirmation time is real, but today's reconstructed band
    # was not available then. Only previously saved frozen bands may earn old
    # support/resistance-test records through evaluate_frozen_level.
    for level in merged:
        level["band_frozen_at"] = signal
    # Evaluate these current structures without claiming they were known on the
    # original extrema dates. Historical callers pass independently frozen bands.
    result["levels"] = sorted(
        [
            evaluate_frozen_level(
                level, rows, [row["date"] for row in rows], signal_date=signal, config=cfg
            )
            for level in merged + gaps
        ],
        key=lambda item: (item["lower"], item["level_id"]),
    )
    for n in (5, 10, 20):
        if len(rows) >= n:
            result["dynamic_references"][f"MA{n}"] = sum(row["close"] for row in rows[-n:]) / n
    return result


def conservative_tick(value: float, tick: float, *, up: bool) -> float:
    price, step = Decimal(str(value)), Decimal(str(tick))
    rounded = (price / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return float(rounded * step)


def nearest_significant_resistance(
    levels: list[dict],
    entry: float,
    *,
    route: str,
) -> tuple[dict | None, bool]:
    """Choose once at the initial entry; do not switch after tightening its cap."""
    eligible = []
    for level in levels:
        if not level.get("significant") or level.get("status") != "active":
            continue
        # The route A breakout reference itself is not an unbroken overhead
        # barrier. A merged band with another significant origin remains a barrier.
        origins = level.get("origins", [])
        significant_other = any(
            item.get("significant") and item.get("source_type") != "box_upper" for item in origins
        )
        if (
            route == "base_breakout"
            and "box_upper" in level.get("source_types", [])
            and not significant_other
        ):
            continue
        lower, upper = finite(level.get("lower")), finite(level.get("upper"))
        if lower is None or upper is None or upper < entry:
            continue
        # Previously broken resistance now above a lower entry remains relevant
        # only if still an active structural band; invalidated supports were removed.
        eligible.append(level)
    if not eligible:
        return None, False
    chosen = min(
        eligible, key=lambda item: (max(item["lower"], entry), item["lower"], item["level_id"])
    )
    return chosen, chosen["lower"] <= entry <= chosen["upper"]


def freeze_entry_reference(
    bars: list[dict],
    levels: dict,
    *,
    setup: dict,
    market_mode: str,
    signal_date: str,
    config: dict | None = None,
) -> dict:
    """Freeze conservative rounded prices; unknown geometry is never infinite reward."""
    cfg, signal = risk_config(config), day_key(signal_date)
    by_day, conflicts = dated_rows(bars, signal)
    dates = sorted(by_day)
    rows = [
        {
            **by_day[day],
            **{key: finite(by_day[day].get(key)) for key in ("open", "high", "low", "close")},
        }
        for day in dates
    ]
    by_day = dict(zip(dates, rows, strict=True))
    route = setup.get("route", setup.get("setup"))
    result = {
        "action": "watch_only",
        "status": "unknown",
        "reference": None,
        "invalidation": None,
        "entry_low": None,
        "entry_high": None,
        "entry_high_before_resistance": None,
        "entry_high_after_resistance": None,
        "nearest_resistance_id": None,
        "overhead_room": None,
        "risk_distance": None,
        "geometric_rr": None,
        "entry_mode": "open_reference",
        "entry_check": "pending",
        "price_basis": "signal_anchored_adjusted",
        "signal_date": signal,
        "reason_codes": [],
        "not_an_execution_or_profit_guarantee": True,
    }
    atr = finite(levels.get("atr14"))
    if (
        conflicts
        or not rows
        or dates[-1] != signal
        or not all(_valid_bar(row) for row in rows)
        or atr is None
        or atr < 0
        or levels.get("status") != "complete"
        or route not in {"base_breakout", "pullback_recovery"}
        or market_mode not in {"active", "selective", "risk_off", "unknown"}
    ):
        result["action"] = "review_required"
        result["reason_codes"] = ["price_history_or_setup_incomplete"]
        return result
    if market_mode not in {"active", "selective"}:
        result["reason_codes"] = ["market_does_not_allow_priority"]
        return result
    current = rows[-1]
    r_max = cfg[f"{market_mode}_risk_max"]
    close_cap = cfg[f"{market_mode}_close_entry_cap"]
    if route == "base_breakout":
        reference = finite(setup.get("B"))
        invalidation = (
            min(reference, current["low"]) - cfg["invalidation_atr_multiple"] * atr
            if reference is not None
            else None
        )
        peak_cap = reference * cfg["breakout_reference_cap"] if reference is not None else None
    else:
        p_day = day_key(setup.get("p", ""))
        peak = by_day.get(p_day)
        pullback = [row for day, row in by_day.items() if p_day < day <= signal]
        if len(rows) < 2 or not peak or not pullback:
            result["reason_codes"] = ["recovery_structure_missing"]
            return result
        reference = rows[-2]["high"]
        invalidation = min(row["low"] for row in pullback) - cfg["invalidation_atr_multiple"] * atr
        peak_cap = peak["high"] * cfg["recovery_peak_entry_cap"]
    if reference is None or reference <= 0 or invalidation is None or invalidation <= 0:
        result["reason_codes"] = ["nonpositive_structural_invalidation"]
        return result
    # A reported failure boundary rounded upward causes earlier invalidation,
    # rather than claiming a more generous lower structural stop.
    low = conservative_tick(reference, cfg["price_tick"], up=True)
    stop = conservative_tick(invalidation, cfg["price_tick"], up=True)
    high_raw = min(peak_cap, current["close"] * close_cap, invalidation / (1 - r_max))
    before = conservative_tick(high_raw, cfg["price_tick"], up=False)
    resistance, at_resistance = nearest_significant_resistance(levels["levels"], low, route=route)
    result.update(
        {
            "reference": reference,
            "invalidation": stop,
            "invalidation_raw": invalidation,
            "entry_low": low,
            "entry_high_before_resistance": before,
            "r_max": r_max,
        }
    )
    after = before
    if resistance is not None:
        nearest = resistance["lower"]
        result["nearest_resistance_id"] = resistance["level_id"]
        result["nearest_resistance_lower"] = nearest
        if at_resistance:
            result["reason_codes"].append("at_resistance")
        # Use the original, unraised S for geometry. Tick rounding of the
        # displayed boundary never beautifies room or creates priority.
        bound = (nearest + cfg["geometric_rr_min"] * invalidation) / (1 + cfg["geometric_rr_min"])
        strict_below = conservative_tick(nearest, cfg["price_tick"], up=False)
        if strict_below >= nearest:
            strict_below -= cfg["price_tick"]
        after = min(before, conservative_tick(bound, cfg["price_tick"], up=False), strict_below)
        distance = low - invalidation
        result["overhead_room"] = (nearest - low) / low
        result["risk_distance"] = distance / low
        result["geometric_rr"] = (nearest - low) / distance if distance > 0 else None
    else:
        result["reason_codes"].append("rr_unknown_no_significant_resistance")
        result["risk_distance"] = (low - invalidation) / low
    result.update({"entry_high": after, "entry_high_after_resistance": after})
    if after < low or stop >= low:
        result["status"] = "invalid"
        result["reason_codes"].append("no_valid_reference_interval")
    elif at_resistance:
        result["status"] = "at_resistance"
    else:
        result.update({"status": "valid", "action": "pass"})
    return result


def check_open_reference(
    frozen: dict,
    *,
    open_price: float | None,
    trading_status: str,
    price_scale_confirmed: bool = True,
) -> dict:
    """A later observed open updates the scenario without changing frozen bounds."""
    price = finite(open_price)
    if price is None or price <= 0 or trading_status == "unknown" or not price_scale_confirmed:
        return {"entry_check": "unobservable", "reason_code": "open_or_trade_status_unknown"}
    if trading_status != "tradable":
        return {"entry_check": "fail", "reason_code": "target_not_tradable"}
    low, high, stop = [
        finite(frozen.get(key)) for key in ("entry_low", "entry_high", "invalidation")
    ]
    if low is None or high is None or stop is None or frozen.get("status") != "valid":
        return {"entry_check": "fail", "reason_code": "frozen_interval_invalid"}
    if price <= stop:
        return {"entry_check": "fail", "reason_code": "open_not_above_invalidation"}
    if price < low:
        return {"entry_check": "fail", "reason_code": "open_below_reference_low"}
    if price > high:
        return {"entry_check": "fail", "reason_code": "open_above_reference_high"}
    return {
        "entry_check": "pass",
        "reason_code": "open_reference_price_condition_met",
        "fill_status": "not_inferred",
    }
