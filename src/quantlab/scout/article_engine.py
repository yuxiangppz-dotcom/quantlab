"""Pure calendar-aligned article strategy calculations, no provider or model calls.

The dict interface keeps source evidence separate from deterministic research
labels. Thresholds are initial research choices, not established market laws.
All dates use exchange trading sessions; raw prices remain available for limits.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from statistics import mean, median
from zoneinfo import ZoneInfo

from quantlab.scout.article_config import (
    DEFAULT_CONFIG,
    TECHNICAL_RATIO_ABS_TOLERANCE,
    ArticleConfig,
    config_hash,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
PRICE_FIELDS = ("open", "high", "low", "close")


def _day(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text).isoformat()


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _stamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo is not None else None


def _calendar(sessions, signal_date) -> tuple[list[str], int]:
    calendar = sorted({_day(day) for day in sessions})
    signal = _day(signal_date)
    if signal not in calendar:
        raise ValueError("Signal date is not an exchange trading session")
    return calendar, calendar.index(signal)


def _rule(name, condition, value, threshold, unit, day, reason=None) -> dict:
    status = "unknown" if condition is None else "pass" if condition else "fail"
    return {
        "rule": name,
        "status": status,
        "value": value,
        "threshold": threshold,
        "unit": unit,
        "date": day,
        "source": "daily+adj_factor; configured research rule",
        "reason_code": reason or (f"{name}_{status}"),
    }


def _combine(rules: list[dict]) -> str:
    statuses = {row["status"] for row in rules}
    return "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"


def _ratio_at_least(value: float, threshold: float) -> bool:
    return value >= threshold or math.isclose(
        value, threshold, rel_tol=0, abs_tol=TECHNICAL_RATIO_ABS_TOLERANCE
    )


def _ratio_at_most(value: float, threshold: float) -> bool:
    return value <= threshold or math.isclose(
        value, threshold, rel_tol=0, abs_tol=TECHNICAL_RATIO_ABS_TOLERANCE
    )


def normalize_bars(raw_bars, sessions, signal_date, config: ArticleConfig = DEFAULT_CONFIG) -> dict:
    """Anchor every historical OHLC to factor[t], preserving exact-date gaps.

    Identical duplicate records are deduplicated; conflicting records invalidate
    that date. Future records are ignored, never used to repair past history.
    """
    calendar, index = _calendar(sessions, signal_date)
    signal = calendar[index]
    expected = calendar[max(0, index - config.history_sessions + 1) : index + 1]
    by_day: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for item in raw_bars:
        try:
            day = _day(item.get("date", item.get("trade_date")))
        except (ValueError, TypeError):
            errors["invalid_date"] = "invalid_date"
            continue
        if day > signal or day not in expected:
            continue
        canonical = {
            key: _number(item.get(key))
            for key in (*PRICE_FIELDS, "amount_cny", "volume_shares", "adj_factor")
        }
        canonical.update(
            date=day,
            up_limit=_number(item.get("up_limit")),
            down_limit=_number(item.get("down_limit")),
            source_ids=sorted(set(item.get("source_ids", ()))),
        )
        previous = by_day.get(day)
        if previous is not None and previous != canonical:
            errors[day] = "conflicting_duplicate"
        by_day[day] = canonical
    anchor = by_day.get(signal, {}).get("adj_factor")
    anchor = anchor if anchor is not None and anchor > 0 and signal not in errors else None
    rows = []
    for day in expected:
        if day not in by_day:
            errors.setdefault(day, "missing_trading_session")
            continue
        row = by_day[day]
        values = [row[field] for field in (*PRICE_FIELDS, "adj_factor")]
        invalid = any(value is None or value <= 0 for value in values)
        invalid |= any(
            row[field] is None or row[field] < 0 for field in ("amount_cny", "volume_shares")
        )
        if not invalid:
            invalid = row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(
                row["open"], row["close"], row["high"]
            )
        if invalid or anchor is None or day in errors:
            errors.setdefault(day, "invalid_bar" if invalid else "unknown_adjustment_anchor")
            continue
        factor = row["adj_factor"] / anchor
        rows.append(
            {
                **row,
                **{field: row[field] * factor for field in PRICE_FIELDS},
                "raw_open": row["open"],
                "raw_high": row["high"],
                "raw_low": row["low"],
                "raw_close": row["close"],
                "price_basis": f"adjusted_to_{signal}",
                "anchor_factor": anchor,
            }
        )
    missing = [day for day in expected if day in errors]
    return {
        "signal_date": signal,
        "expected_dates": expected,
        "rows": rows,
        "errors": errors,
        "missing_dates": missing,
        "history_status": (
            "complete" if len(expected) == config.history_sessions and not missing else "unknown"
        ),
        "price_basis": f"adjusted_to_{signal}",
    }


def _window(rows, calendar, end_index, size) -> list[dict] | None:
    start = end_index - size + 1
    if start < 0 or end_index < 0:
        return None
    dates = calendar[start : end_index + 1]
    if len(dates) != size or any(day not in rows for day in dates):
        return None
    return [rows[day] for day in dates]


def _average_close(rows, calendar, end_index, size):
    window = _window(rows, calendar, end_index, size)
    return mean(row["close"] for row in window) if window else None


def compute_metrics(adjusted_bars, sessions, signal_date) -> dict:
    calendar, index = _calendar(sessions, signal_date)
    rows = {row["date"]: row for row in adjusted_bars}
    today = rows.get(calendar[index])
    result = {"date": calendar[index], "price_basis": f"adjusted_to_{calendar[index]}"}
    for n in (1, 3, 5, 20):
        window = _window(rows, calendar, index, n + 1)
        result[f"ret{n}"] = window[-1]["close"] / window[0]["close"] - 1 if window else None
    for n in (5, 10, 20):
        result[f"ma{n}"] = _average_close(rows, calendar, index, n)
    result["ma10_prior3"] = _average_close(rows, calendar, index - 3, 10)
    result["ma20_prior5"] = _average_close(rows, calendar, index - 5, 20)
    prior20 = _window(rows, calendar, index - 1, 20)
    baseline = median(row["amount_cny"] for row in prior20) if prior20 else None
    result["median_amount20"] = baseline
    result["amount_ratio"] = (
        today["amount_cny"] / baseline if today and baseline is not None and baseline > 0 else None
    )
    result["box_high"] = max(row["high"] for row in prior20) if prior20 else None
    result["box_low"] = min(row["low"] for row in prior20) if prior20 else None
    result["close_location"] = result["upper_shadow"] = None
    result["one_price_bar"] = None
    if today:
        result["one_price_bar"] = today["high"] == today["low"]
        spread = today["high"] - today["low"]
        if spread > 0:
            result["close_location"] = (today["close"] - today["low"]) / spread
            result["upper_shadow"] = (today["high"] - max(today["open"], today["close"])) / spread
    result["ma20_distance"] = (
        today["close"] / result["ma20"] - 1 if today and result["ma20"] else None
    )
    result["above_ma20"] = today["close"] > result["ma20"] if today and result["ma20"] else None
    atr_window = _window(rows, calendar, index, 15)
    result["atr14"] = (
        mean(
            max(
                current["high"] - current["low"],
                abs(current["high"] - prior["close"]),
                abs(current["low"] - prior["close"]),
            )
            for prior, current in zip(atr_window[:-1], atr_window[1:], strict=True)
        )
        if atr_window
        else None
    )
    return result


def evaluate_routes(
    adjusted_bars, sessions, signal_date, metrics=None, config: ArticleConfig = DEFAULT_CONFIG
) -> dict:
    calendar, index = _calendar(sessions, signal_date)
    day = calendar[index]
    rows = {row["date"]: row for row in adjusted_bars}
    today = rows.get(day)
    m = metrics or compute_metrics(adjusted_bars, sessions, signal_date)
    rules_a = []

    def check_a(name, value, threshold, predicate, unit="ratio"):
        rules_a.append(
            _rule(
                name, predicate(value) if value is not None else None, value, threshold, unit, day
            )
        )

    box_high, box_low = m["box_high"], m["box_low"]
    width = box_high / box_low - 1 if box_high is not None and box_low else None
    distance = today["close"] / box_high - 1 if today and box_high else None
    check_a(
        "box_width",
        width,
        config.box_width_max,
        lambda value: _ratio_at_most(value, config.box_width_max),
    )
    check_a(
        "close_breakout",
        distance,
        config.breakout_min,
        lambda v: _ratio_at_least(v, config.breakout_min),
    )
    tick_break = (
        Decimal(str(today["close"])) - Decimal(str(box_high)) >= Decimal(str(config.tick))
        if today and box_high is not None
        else None
    )
    rules_a.append(_rule("breakout_tick", tick_break, distance, config.tick, "CNY", day))
    check_a(
        "breakout_distance",
        distance,
        config.breakout_max,
        lambda v: _ratio_at_most(v, config.breakout_max),
    )
    check_a(
        "moderate_amount",
        m["amount_ratio"],
        [config.amount_ratio_min, config.amount_ratio_max],
        lambda v: (
            _ratio_at_least(v, config.amount_ratio_min)
            and _ratio_at_most(v, config.amount_ratio_max)
        ),
    )
    check_a(
        "close_quality",
        m["close_location"],
        config.close_location_min,
        lambda v: _ratio_at_least(v, config.close_location_min),
    )
    check_a(
        "upper_shadow",
        m["upper_shadow"],
        config.upper_shadow_max,
        lambda v: _ratio_at_most(v, config.upper_shadow_max),
    )
    check_a("ret5_heat", m["ret5"], config.ret5_max, lambda v: _ratio_at_most(v, config.ret5_max))
    check_a(
        "ma20_heat",
        m["ma20_distance"],
        config.ma20_distance_max,
        lambda v: _ratio_at_most(v, config.ma20_distance_max),
    )
    trend = (
        today["close"] > m["ma10"] and m["ma10"] >= m["ma10_prior3"]
        if today and m["ma10"] is not None and m["ma10_prior3"] is not None
        else None
    )
    rules_a.append(
        _rule(
            "trend_improvement",
            trend,
            [m["ma10"], m["ma10_prior3"]],
            "C>MA10>=MA10[t-3]",
            "CNY",
            day,
        )
    )
    route_a = {
        "route": "base_breakout",
        "status": _combine(rules_a),
        "rule_results": rules_a,
        "breakout_distance": distance,
        "box_high": box_high,
        "box_low": box_low,
        "B": box_high,
        "F": box_low,
        "reference_dates": calendar[max(0, index - 20) : index],
        "signal_date": day,
    }

    rules_b = []
    prior = _window(rows, calendar, index - 1, 20)
    peak = max(prior, key=lambda row: (row["high"], row["date"])) if prior else None
    p_index = calendar.index(peak["date"]) if peak else None
    distance_p = index - p_index if p_index is not None else None
    rules_b.append(
        _rule(
            "peak_sequence",
            None
            if distance_p is None
            else config.peak_distance_min <= distance_p <= config.peak_distance_max,
            distance_p,
            [config.peak_distance_min, config.peak_distance_max],
            "trading_sessions",
            day,
        )
    )
    q_window = _window(rows, calendar, p_index - 3, 18) if p_index is not None else None
    trough = (
        min(q_window, key=lambda row: (row["low"], -calendar.index(row["date"])))
        if q_window
        else None
    )
    rise = peak["high"] / trough["low"] - 1 if peak and trough else None
    pullback = (
        _window(rows, calendar, index - 1, distance_p - 1)
        if distance_p is not None and distance_p > 1
        else None
    )
    end_rise = _window(rows, calendar, p_index, 6) if p_index is not None else None
    low = min(row["low"] for row in pullback) if pullback else None
    retreat = 1 - low / peak["high"] if low is not None and peak else None
    amount_mean = mean(row["amount_cny"] for row in pullback) if pullback else None
    rise_amount = mean(row["amount_cny"] for row in end_rise) if end_rise else None
    contraction = amount_mean / rise_amount if amount_mean is not None and rise_amount else None

    def check_b(name, value, threshold, predicate, unit="ratio"):
        rules_b.append(
            _rule(
                name, predicate(value) if value is not None else None, value, threshold, unit, day
            )
        )

    check_b(
        "prior_rise",
        rise,
        [config.rise_min, config.rise_max],
        lambda v: _ratio_at_least(v, config.rise_min) and _ratio_at_most(v, config.rise_max),
    )
    check_b(
        "pullback_depth",
        retreat,
        [config.pullback_min, config.pullback_max],
        lambda v: (
            _ratio_at_least(v, config.pullback_min) and _ratio_at_most(v, config.pullback_max)
        ),
    )
    check_b(
        "pullback_contraction",
        contraction,
        config.pullback_amount_ratio_max,
        lambda v: _ratio_at_most(v, config.pullback_amount_ratio_max),
    )
    ma_checks = []
    for row in pullback or []:
        ma = _average_close(rows, calendar, calendar.index(row["date"]), 20)
        ma_checks.append(None if ma is None else row["close"] >= config.pullback_ma20_floor * ma)
    structure = (
        False if False in ma_checks else None if not ma_checks or None in ma_checks else True
    )
    rules_b.append(
        _rule(
            "pullback_structure",
            structure,
            [{"date": row["date"], "close": row["close"]} for row in pullback or []],
            f"C[j]>={config.pullback_ma20_floor}*MA20[j]",
            "CNY",
            day,
        )
    )
    rising_ma = (
        m["ma20"] >= m["ma20_prior5"]
        if m["ma20"] is not None and m["ma20_prior5"] is not None
        else None
    )
    rules_b.append(
        _rule(
            "ma20_slope", rising_ma, [m["ma20"], m["ma20_prior5"]], "MA20[t]>=MA20[t-5]", "CNY", day
        )
    )
    yesterday = rows.get(calendar[index - 1]) if index else None
    confirmed = (
        today["close"] > yesterday["high"]
        and today["close"] > today["open"]
        and today["low"] >= yesterday["low"]
        and _ratio_at_least(m["close_location"], config.close_location_min)
        if today and yesterday and m["close_location"] is not None
        else None
    )
    rules_b.append(
        _rule(
            "confirmation",
            confirmed,
            {"today": today, "prior": yesterday},
            "C>H[t-1],C>O,L>=L[t-1],close_location>=threshold",
            "OHLC",
            day,
        )
    )
    confirm_amount = today["amount_cny"] / amount_mean if today and amount_mean else None
    check_b(
        "confirmation_amount",
        confirm_amount,
        config.confirmation_amount_multiple,
        lambda v: _ratio_at_least(v, config.confirmation_amount_multiple),
    )
    check_b(
        "confirmation_not_extreme",
        m["amount_ratio"],
        config.amount_ratio_max,
        lambda v: _ratio_at_most(v, config.amount_ratio_max),
    )
    peak_ratio = today["close"] / peak["high"] if today and peak else None
    check_b(
        "prior_peak_heat",
        peak_ratio,
        config.prior_peak_max_multiple,
        lambda v: _ratio_at_most(v, config.prior_peak_max_multiple),
    )
    check_b(
        "ma20_heat",
        m["ma20_distance"],
        config.ma20_distance_max,
        lambda v: _ratio_at_most(v, config.ma20_distance_max),
    )
    check_b("ret5_heat", m["ret5"], config.ret5_max, lambda v: _ratio_at_most(v, config.ret5_max))
    status_b = _combine(rules_b)
    process_rules = [
        row for row in rules_b if row["rule"] not in {"confirmation", "confirmation_amount"}
    ]
    awaiting = confirmed is False and _combine(process_rules) == "pass"
    route_b = {
        "route": "pullback_recovery",
        "status": status_b,
        "process_state": "awaiting_confirmation" if awaiting else status_b,
        "rule_results": rules_b,
        "signal_date": day,
        "p_date": peak["date"] if peak else None,
        "q_date": trough["date"] if trough else None,
        "p": peak["date"] if peak else None,
        "q": trough["date"] if trough else None,
        "p_high": peak["high"] if peak else None,
        "q_low": trough["low"] if trough else None,
        "rise": rise,
        "pullback_low": low,
        "pullback_depth": retreat,
        "pullback_amount_ratio": contraction,
        "pullback_amount_mean": amount_mean,
        "rise_end_amount_mean": rise_amount,
        "confirmation_amount_ratio": confirm_amount,
        "pullback_dates": [row["date"] for row in pullback or []],
        "rise_end_dates": [row["date"] for row in end_rise or []],
    }
    return {"base_breakout": route_a, "pullback_recovery": route_b}


def _mainboard(code: str) -> bool:
    return (code.endswith(".SH") and code.startswith(("600", "601", "603", "605"))) or (
        code.endswith(".SZ") and code.startswith(("000", "001", "002", "003"))
    )


def analyze_stock(
    ts_code, raw_bars, sessions, signal_date, security=None, config: ArticleConfig = DEFAULT_CONFIG
) -> dict:
    """One stock, no momentum identity gate. Unknown eligibility never means rejection."""
    security = security or {}
    calendar, index = _calendar(sessions, signal_date)
    day = calendar[index]
    normalized = normalize_bars(raw_bars, sessions, day, config)
    metrics = compute_metrics(normalized["rows"], sessions, day)
    routes = evaluate_routes(normalized["rows"], sessions, day, metrics, config)
    listing_age = None
    if security.get("list_date"):
        listed = _day(security["list_date"])
        listing_age = sum(listed <= candidate <= day for candidate in calendar)
        # A short calendar cannot prove age when listing predates its first row.
        if listed < calendar[0] and listing_age < config.listing_sessions:
            listing_age = None
    statuses = {
        "mainboard": _mainboard(ts_code),
        "listing_age": listing_age >= config.listing_sessions if listing_age is not None else None,
        "not_st": None if security.get("is_st") is None else not security["is_st"],
        "not_delisting": None
        if security.get("is_delisting") is None
        else not security["is_delisting"],
    }
    base_rules = [
        _rule(
            name,
            value,
            listing_age if name == "listing_age" else value,
            config.listing_sessions if name == "listing_age" else True,
            "trading_sessions" if name == "listing_age" else "boolean",
            day,
        )
        for name, value in statuses.items()
    ]
    market_status = _combine(base_rules)
    suspended = security.get("suspended_by_date", {}).get(day, security.get("is_suspended"))
    tradable = None if suspended is None else not suspended
    candidate_rules = [
        *base_rules,
        _rule("tradable", tradable, suspended, "not_suspended", "boolean", day),
        _rule(
            "history_complete",
            normalized["history_status"] == "complete"
            if normalized["history_status"] == "complete"
            else None,
            len(normalized["rows"]),
            config.history_sessions,
            "trading_sessions",
            day,
        ),
        _rule(
            "liquidity",
            metrics["median_amount20"] >= config.median_amount_min
            if metrics["median_amount20"] is not None
            else None,
            metrics["median_amount20"],
            config.median_amount_min,
            "CNY",
            day,
        ),
    ]
    candidate_status = _combine(candidate_rules)
    return {
        "ts_code": ts_code,
        "security": security,
        "signal_date": day,
        "strategy_version": config.version,
        "config_hash": config_hash(config),
        "universe": {
            "market_status": market_status,
            "candidate_status": candidate_status,
            "listing_age_sessions": listing_age,
            "rejection_reasons": [
                row["reason_code"] for row in candidate_rules if row["status"] == "fail"
            ],
            "unknown_reasons": [
                row["reason_code"] for row in candidate_rules if row["status"] == "unknown"
            ],
        },
        "adjusted_bars": normalized["rows"],
        "history": normalized,
        "metrics": metrics,
        "routes": routes,
        "rule_results": candidate_rules,
        "eligibility": "excluded"
        if candidate_status == "fail"
        else "review_required"
        if candidate_status == "unknown"
        else "priority_candidate",
    }


def _at_limit(raw_close, limit, tick):
    if raw_close is None or limit is None or limit <= 0:
        return None
    return round(raw_close / tick) == round(limit / tick)


def _historical_market_status(stock, calendar, index, config):
    day = calendar[index]
    if day == stock["signal_date"]:
        return stock["universe"]["market_status"]
    # Never classify past ST/delisting status from today's security metadata.
    security = stock.get("security", {})
    historic = security.get("status_by_date", {}).get(day)
    if historic is None:
        return "unknown"
    listed = security.get("list_date")
    age = sum(_day(listed) <= candidate <= day for candidate in calendar) if listed else None
    facts = [
        _mainboard(stock["ts_code"]),
        age >= config.listing_sessions if age is not None else None,
        not historic["is_st"] if historic.get("is_st") is not None else None,
        not historic["is_delisting"] if historic.get("is_delisting") is not None else None,
    ]
    return "fail" if False in facts else "unknown" if None in facts else "pass"


def _suspension_status(stock, day):
    security = stock.get("security", {})
    suspended = security.get("suspended_by_date", {}).get(day)
    if day == stock["signal_date"] and suspended is None:
        suspended = security.get("is_suspended")
    return suspended


def _market_day(analyses, calendar, index, config):
    day = calendar[index]
    expected = valid = advances = down_limits = up_limits = 0
    unknown_down_limits = unknown_up_limits = False
    returns1, returns3, above_ma, amounts = [], [], [], []
    for stock in analyses:
        market_status = _historical_market_status(stock, calendar, index, config)
        if market_status == "fail":
            continue
        suspended = _suspension_status(stock, day)
        if suspended is True:
            continue
        expected += 1
        if market_status != "pass":
            continue
        rows = {row["date"]: row for row in stock["adjusted_bars"]}
        window = _window(rows, calendar, index, 2)
        if not window:
            continue
        today, prior = window[1], window[0]
        ret1 = today["close"] / prior["close"] - 1
        returns1.append(ret1)
        advances += ret1 > 0
        amounts.append(today["amount_cny"])
        valid += 1
        limit_down = _at_limit(today["raw_close"], today["down_limit"], config.tick)
        limit_up = _at_limit(today["raw_close"], today["up_limit"], config.tick)
        unknown_down_limits |= limit_down is None
        unknown_up_limits |= limit_up is None
        down_limits += limit_down is True
        up_limits += limit_up is True
        past3 = _window(rows, calendar, index, 4)
        if past3:
            returns3.append(today["close"] / past3[0]["close"] - 1)
        ma20 = _average_close(rows, calendar, index, 20)
        if ma20 is not None:
            above_ma.append(today["close"] > ma20)
    coverage = valid / expected if expected else None
    complete = coverage is not None and coverage >= config.market_coverage_min and valid > 0

    def window_coverage(count, size):
        ratio = count / expected if expected else None
        return {
            "window_sessions": size,
            "expected_count": expected,
            "valid_count": count,
            "coverage": ratio,
            "required_coverage": config.market_coverage_min,
            "status": "complete"
            if count and ratio is not None and ratio >= config.market_coverage_min
            else "unknown",
        }

    windows = {
        "ret1": window_coverage(len(returns1), 2),
        "ret3": window_coverage(len(returns3), 4),
        "above_ma20": window_coverage(len(above_ma), 20),
    }
    return {
        "date": day,
        "expected_count": expected,
        "valid_count": valid,
        "coverage": coverage,
        "core_status": "complete" if complete else "unknown",
        "adv_ratio": advances / valid if valid else None,
        "median_ret1": median(returns1) if returns1 else None,
        "median_ret3": median(returns3) if windows["ret3"]["status"] == "complete" else None,
        "down_limit_ratio": down_limits / valid if valid and not unknown_down_limits else None,
        "up_limit_ratio": up_limits / valid if valid and not unknown_up_limits else None,
        "down_limit_count": down_limits if not unknown_down_limits else None,
        "up_limit_count": up_limits if not unknown_up_limits else None,
        "total_amount": sum(amounts) if valid else None,
        "above_ma20_ratio": sum(above_ma) / len(above_ma)
        if windows["above_ma20"]["status"] == "complete"
        else None,
        "window_coverage": windows,
    }


def assess_market(
    stock_analyses,
    sessions,
    signal_date,
    config: ArticleConfig = DEFAULT_CONFIG,
    *,
    limit_structure=None,
) -> dict:
    calendar, index = _calendar(sessions, signal_date)
    analyses = (
        list(stock_analyses.values()) if isinstance(stock_analyses, dict) else list(stock_analyses)
    )
    daily = [
        _market_day(analyses, calendar, j, config) for j in range(max(0, index - 5), index + 1)
    ]
    today = daily[-1]
    recent = daily[-3:]
    history_complete = len(recent) == 3 and all(row["core_status"] == "complete" for row in recent)
    adv, ret1, down = today["adv_ratio"], today["median_ret1"], today["down_limit_ratio"]
    flags = [
        None if adv is None else adv <= config.risk_adv_max,
        None if ret1 is None else ret1 <= config.risk_median_ret_max,
        None if down is None else down >= config.risk_down_limit_min,
        sum(row["adv_ratio"] <= config.risk_history_adv_max for row in recent)
        >= config.risk_history_days
        if history_complete
        else None,
    ]
    state = "unknown"
    if today["core_status"] == "complete":
        if sum(flag is True for flag in flags) >= 2:
            state = "risk_off"
        elif None not in flags and history_complete:
            active = (
                adv >= config.active_adv_min
                and ret1 > 0
                and down < config.risk_down_limit_min
                and sum(row["adv_ratio"] >= config.active_history_adv_min for row in recent)
                >= config.active_history_days
            )
            state = "active" if active else "selective"
    totals = {"active": (5, 3), "selective": (3, 1), "risk_off": (3, 0), "unknown": (3, 0)}
    total_cap, priority_cap = totals[state]
    u = set((limit_structure or {}).get("U", ()))
    z = set((limit_structure or {}).get("Z", ()))
    failure_rate = len(z - u) / len(u | z) if u | z else None
    prior_amounts = [row["total_amount"] for row in daily[-6:-1]]
    amount_base = (
        median(prior_amounts) if len(prior_amounts) == 5 and None not in prior_amounts else None
    )
    return {
        "state": state,
        "market_mode": "沪深主板样本环境",
        "signal_date": calendar[index],
        "priority_cap": priority_cap,
        "total_cap": total_cap,
        "metrics": today,
        "daily_metrics": daily,
        "risk_flags": dict(zip(("R1", "R2", "R3", "R4"), flags, strict=True)),
        "rule_results": [
            _rule(f"R{j + 1}", flag, flag, "risk_trigger", "boolean", calendar[index])
            for j, flag in enumerate(flags)
        ],
        "broken_limit_rate": failure_rate,
        "amount_change": today["total_amount"] / amount_base - 1 if amount_base else None,
        "empty_reason": "data_insufficient"
        if state == "unknown"
        else "strategy_pauses_priority"
        if state == "risk_off"
        else None,
    }


def _percentiles(values: dict[str, float]) -> dict[str, float | None]:
    if len(values) < 2:
        return {key: None for key in values}
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    result = {}
    for key, value in ordered:
        ranks = [j + 1 for j, (_, candidate) in enumerate(ordered) if candidate == value]
        result[key] = (mean(ranks) - 1) / (len(ordered) - 1)
    return result


def assess_groups(
    group_snapshots,
    stock_analyses,
    sessions,
    signal_date,
    as_of,
    config: ArticleConfig = DEFAULT_CONFIG,
    *,
    historical_cutoffs=None,
) -> list[dict]:
    """Separate industry/theme ranks; persistence requires actual per-day availability.

    snapshots are exact-date membership evidence, not today's members retrofilled.
    historical_cutoffs maps each past signal date to its actual frozen cutoff; by
    default past dates conservatively use that day's 23:59:59 Shanghai time.
    A newly downloaded historical snapshot remains unusable for past persistence.
    """
    calendar, index = _calendar(sessions, signal_date)
    cutoff = _stamp(as_of)
    if cutoff is None:
        raise ValueError("Group as_of requires a timezone-aware actual cutoff")
    analyses = (
        stock_analyses
        if isinstance(stock_analyses, dict)
        else {stock["ts_code"]: stock for stock in stock_analyses}
    )
    group_snapshots = [
        row
        for row in group_snapshots
        if _day(row["snapshot_date"]) <= calendar[index]
        and _stamp(row.get("known_at")) is not None
        and _stamp(row["known_at"]) <= cutoff
    ]
    identities = sorted({(row["group_type"], row["group_id"]) for row in group_snapshots})
    by_day: dict[str, list[dict]] = {}
    for j in range(max(0, index - 5), index + 1):
        day = calendar[j]
        day_cutoff = (
            cutoff
            if j == index
            else (
                _stamp((historical_cutoffs or {}).get(day))
                or datetime.combine(date.fromisoformat(day), time(23, 59, 59), SHANGHAI)
            )
        )
        day_cutoff = min(day_cutoff, cutoff)
        market = _market_day(list(analyses.values()), calendar, j, config)
        calculated = []
        for group_type, group_id in identities:
            candidates = [
                row
                for row in group_snapshots
                if row["group_type"] == group_type
                and row["group_id"] == group_id
                and _day(row["snapshot_date"]) == day
                and _stamp(row.get("known_at")) is not None
                and _stamp(row["known_at"]) <= day_cutoff
            ]
            candidates.sort(key=lambda row: (_stamp(row["known_at"]), str(row.get("source", ""))))
            snapshot = candidates[-1] if candidates else None
            conflict = False
            if snapshot is not None:
                latest = _stamp(snapshot["known_at"])
                conflicts = [row for row in candidates if _stamp(row["known_at"]) == latest]
                signatures = {
                    (tuple(sorted(set(row["members"]))), row.get("complete")) for row in conflicts
                }
                conflict = len(signatures) > 1
                if conflict:
                    snapshot = None
            if snapshot is None or snapshot.get("complete") is not True or conflict:
                calculated.append(
                    {
                        "group_id": group_id,
                        "group_type": group_type,
                        "date": day,
                        "coverage_status": "membership_incomplete",
                        "metrics": {},
                        "strength": None,
                        "snapshot": snapshot,
                        "reason_code": "conflicting_membership_snapshot"
                        if conflict
                        else "membership_unavailable_or_partial",
                    }
                )
                continue
            members = sorted(set(snapshot["members"]))
            expected_members = [
                code
                for code in members
                if code in analyses
                and _historical_market_status(analyses[code], calendar, j, config) != "fail"
                and _suspension_status(analyses[code], day) is not True
            ]
            unknown_members = [
                code for code in members if code not in analyses and _mainboard(code)
            ]
            denominator = len(expected_members) + len(unknown_members)
            values = []
            unknown_limits = False
            for code in expected_members:
                stock = analyses[code]
                if _historical_market_status(stock, calendar, j, config) != "pass":
                    continue
                raw_rows = {row["date"]: row for row in stock["adjusted_bars"]}
                window = _window(raw_rows, calendar, j, 4)
                if not window:
                    continue
                last = window[-1]
                at_limit = _at_limit(last["raw_close"], last["up_limit"], config.tick)
                unknown_limits |= at_limit is None
                values.append(
                    {
                        "ret1": last["close"] / window[-2]["close"] - 1,
                        "ret3": last["close"] / window[0]["close"] - 1,
                        "amount": last["amount_cny"],
                        "up_limit": at_limit,
                    }
                )
            count = len(values)
            coverage = count / denominator if denominator else None
            valid = (
                denominator >= config.group_members_min
                and coverage is not None
                and coverage >= config.group_coverage_min
                and market["core_status"] == "complete"
            )
            metrics = {
                "expected_count": denominator,
                "valid_count": count,
                "coverage": coverage,
                "breadth": sum(row["ret1"] > 0 for row in values) / count if count else None,
                "median_ret1": median(row["ret1"] for row in values) if count else None,
                "median_ret3": median(row["ret3"] for row in values) if count else None,
                "excess3": median(row["ret3"] for row in values) - market["median_ret3"]
                if count and market["median_ret3"] is not None
                else None,
                "limit_count": sum(row["up_limit"] is True for row in values)
                if not unknown_limits
                else None,
                "limit_density": sum(row["up_limit"] is True for row in values) / count
                if count and not unknown_limits
                else None,
                "amount_share": sum(row["amount"] for row in values) / market["total_amount"]
                if market["total_amount"]
                else None,
                "market_limit_density": market["up_limit_ratio"],
            }
            calculated.append(
                {
                    "group_id": group_id,
                    "group_type": group_type,
                    "date": day,
                    "coverage_status": "complete" if valid else "partial",
                    "metrics": metrics,
                    "strength": None,
                    "snapshot": snapshot,
                }
            )
        for group_type in sorted({identity[0] for identity in identities}):
            eligible = [
                row
                for row in calculated
                if row["group_type"] == group_type
                and row["coverage_status"] == "complete"
                and all(
                    row["metrics"].get(key) is not None
                    for key in ("excess3", "breadth", "limit_density")
                )
            ]
            ranks = [
                _percentiles({row["group_id"]: row["metrics"][key] for row in eligible})
                for key in ("excess3", "breadth", "limit_density")
            ]
            for row in eligible:
                scores = [rank[row["group_id"]] for rank in ranks]
                row["strength"] = mean(scores) if None not in scores else None
            final_ranks = _percentiles(
                {
                    row["group_id"]: row["strength"]
                    for row in eligible
                    if row["strength"] is not None
                }
            )
            for row in eligible:
                row["strength_percentile"] = final_ranks.get(row["group_id"])
        by_day[day] = calculated
    output = []
    for row in by_day[calendar[index]]:
        matching = [
            next(
                candidate
                for candidate in by_day[day]
                if candidate["group_id"] == row["group_id"]
                and candidate["group_type"] == row["group_type"]
            )
            for day in calendar[max(0, index - 5) : index + 1]
        ]
        history5 = matching[-5:]
        complete5 = len(history5) == 5 and all(
            candidate.get("strength_percentile") is not None for candidate in history5
        )
        persistence = (
            sum(
                candidate["strength_percentile"] >= config.persistence_percentile_min
                for candidate in history5
            )
            if complete5
            else None
        )
        prior_shares = [candidate["metrics"].get("amount_share") for candidate in matching[:-1]]
        share_base = (
            median(prior_shares) if len(prior_shares) == 5 and None not in prior_shares else None
        )
        m = row["metrics"]
        expansion = (
            m.get("amount_share") / share_base
            if share_base and m.get("amount_share") is not None
            else None
        )
        status = "inactive"
        if row["coverage_status"] != "complete":
            status = (
                "membership_incomplete"
                if row["coverage_status"] == "membership_incomplete"
                else "partial"
            )
        elif m["breadth"] < config.group_pause_breadth_max:
            status = "paused"
        elif (
            persistence is not None
            and persistence >= config.persistence_days_min
            and m["excess3"] is not None
            and m["excess3"] > 0
            and m["median_ret3"] > 0
            and m["breadth"] >= config.established_breadth_min
        ):
            status = "established"
        elif (
            m["breadth"] >= config.emerging_breadth_min
            and m["median_ret1"] > 0
            and m["excess3"] is not None
            and m["excess3"] > 0
            and (
                (expansion is not None and expansion >= config.emerging_share_expansion_min)
                or (
                    m["limit_density"] is not None
                    and m["market_limit_density"] is not None
                    and m["limit_density"]
                    >= config.emerging_limit_density_multiple * m["market_limit_density"]
                    and m["limit_count"] >= config.emerging_limit_count_min
                )
            )
        ):
            status = "emerging"
        snapshot = row.get("snapshot") or {}
        output.append(
            {
                **row,
                "group_state": status,
                "status": status,
                "persistence": persistence,
                "share_expansion": expansion,
                "membership_source": snapshot.get("source"),
                "snapshot_at": snapshot.get("known_at"),
                "members": sorted(set(snapshot.get("members", ()))),
                "daily_history": matching,
                "priority_support": status == "established",
                "member_roles": _member_roles(
                    snapshot.get("members", ()), analyses, calendar, index, config
                ),
            }
        )
    return sorted(output, key=_group_order)


def _member_roles(members, analyses, calendar, index, config):
    """Descriptive current-member roles; never used to manufacture persistence."""
    eligible = [analyses[code] for code in sorted(set(members)) if code in analyses]
    windows = {
        stock["ts_code"]: _window(
            {row["date"]: row for row in stock["adjusted_bars"]}, calendar, index, 5
        )
        for stock in eligible
    }
    denominator = (
        sum(sum(row["amount_cny"] for row in window) for window in windows.values() if window)
        if windows and all(window is not None for window in windows.values())
        else None
    )
    ret20_values = [stock["metrics"]["ret20"] for stock in eligible]
    group_ret20 = median(ret20_values) if ret20_values and None not in ret20_values else None
    output = []
    for stock in eligible:
        rows = {row["date"]: row for row in stock["adjusted_bars"]}
        active = 0
        active_status = "complete"
        for j in range(index, -1, -1):
            recent = _window(rows, calendar, j, 2)
            prior20 = _window(rows, calendar, j - 1, 20)
            if not recent or not prior20:
                active_status = "partial_boundary" if active else "unknown"
                break
            baseline = median(row["amount_cny"] for row in prior20)
            if baseline <= 0:
                active_status = "unknown"
                break
            if not (
                recent[-1]["close"] > recent[-2]["close"]
                and recent[-1]["amount_cny"] / baseline >= config.role_active_amount_min
            ):
                break
            active += 1
        window = windows[stock["ts_code"]]
        ret20 = stock["metrics"]["ret20"]
        output.append(
            {
                "ts_code": stock["ts_code"],
                "amount_share5": sum(row["amount_cny"] for row in window) / denominator
                if window and denominator
                else None,
                "relative_ret20": ret20 - group_ret20
                if ret20 is not None and group_ret20 is not None
                else None,
                "consecutive_active_days": active if active_status != "unknown" else None,
                "active_status": active_status,
                "active_definition": "ret1>0 and amount_ratio>=configured role threshold",
                "membership_basis": "current_members_historical_description",
            }
        )
    return output


def _descending(value):
    number = _number(value)
    return -number if number is not None else math.inf


def _ascending(value):
    number = _number(value)
    return number if number is not None else math.inf


def _group_order(row):
    return (
        0 if row.get("group_state", row.get("status")) == "established" else 1,
        _descending(row.get("persistence")),
        _descending(row.get("strength")),
        row.get("group_id", ""),
        row.get("group_type", ""),
    )


def _route_order(candidate, route, config=DEFAULT_CONFIG):
    metrics = candidate.get("metrics", {})
    setup = candidate.get("routes", {}).get(route, {})
    liquidity = _descending(metrics.get("median_amount20"))
    if route == "base_breakout":
        amount = _number(metrics.get("amount_ratio"))
        return (
            _ascending(setup.get("breakout_distance")),
            _ascending(metrics.get("upper_shadow")),
            abs(amount - config.breakout_amount_sort_target) if amount is not None else math.inf,
            liquidity,
            candidate["ts_code"],
        )
    return (
        _ascending(setup.get("pullback_amount_ratio")),
        _descending(metrics.get("close_location")),
        _ascending(metrics.get("ma20_distance")),
        liquidity,
        candidate["ts_code"],
    )


def allocate_budget(
    candidates,
    groups,
    config: ArticleConfig = DEFAULT_CONFIG,
    *,
    context_limit=None,
    deep_limit=None,
) -> dict:
    """Globally serve possible-priority before watch, then group x route round-robin.

    Same stock consumes one slot while preserving all its group/route evidence.
    No prior momentum shortlist. Excluded records are archived without AI budget.
    """
    context_limit = config.context_limit if context_limit is None else context_limit
    deep_limit = config.deep_limit if deep_limit is None else deep_limit
    if not 0 <= deep_limit <= context_limit:
        raise ValueError("Invalid bounded research budget")
    candidate_rows = list(candidates.values()) if isinstance(candidates, dict) else list(candidates)
    by_code = {}
    for row in candidate_rows:
        code = row["ts_code"]
        if code in by_code and by_code[code] != row:
            raise ValueError("Conflicting duplicate candidate")
        by_code[code] = row
    groups = sorted(groups, key=_group_order)
    unit_order = [
        (group["group_id"], group.get("group_type", ""), route)
        for group in groups
        if group.get("group_state", group.get("status")) in {"established", "emerging"}
        for route in config.routes
    ]
    excluded = sorted(
        code
        for code, row in by_code.items()
        if any(
            row.get(key) in {"excluded", "exclude", "reject"}
            for key in ("eligibility", "eligibility_ceiling")
        )
    )

    def tier(row):
        status = row.get("eligibility_ceiling", row.get("eligibility", "review_required"))
        return 0 if status in {"priority_candidate", "priority", "focus"} else 1

    def units_for(pool, eligibility_tier):
        units = defaultdict(list)
        for group_id, group_type, route in unit_order:
            for row in pool:
                if row["ts_code"] in excluded or tier(row) != eligibility_tier:
                    continue
                membership = row.get("group_ids", ())
                if not membership:
                    membership = [
                        group["group_id"]
                        for group in groups
                        if row["ts_code"] in group.get("members", ())
                    ]
                if (
                    group_id not in membership
                    or row.get("routes", {}).get(route, {}).get("status") != "pass"
                ):
                    continue
                units[(group_id, group_type, route)].append(row)
            units[(group_id, group_type, route)].sort(
                key=lambda row: _route_order(row, route, config)
            )
        return units

    def round_robin(pool, limit):
        chosen, seen = [], set()
        for eligibility_tier in (0, 1):
            units = units_for(pool, eligibility_tier)
            while len(chosen) < limit:
                advanced = False
                for identity in unit_order:
                    queue = units[identity]
                    while queue and queue[0]["ts_code"] in seen:
                        queue.pop(0)
                    if not queue:
                        continue
                    item = queue.pop(0)
                    chosen.append(item)
                    seen.add(item["ts_code"])
                    advanced = True
                    if len(chosen) == limit:
                        break
                if not advanced:
                    break
        return chosen

    context = round_robin(list(by_code.values()), context_limit)
    deep = round_robin(context, deep_limit)
    return {
        "context": context,
        "deep": deep,
        "excluded": excluded,
        "unit_order": [list(identity) for identity in unit_order],
        "context_limit": context_limit,
        "deep_limit": deep_limit,
        "policy": "eligibility_then_group_route_round_robin_v1",
    }
