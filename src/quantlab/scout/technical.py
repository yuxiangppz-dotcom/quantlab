"""Deterministic D0 technical facts; prices are research prices, not orders.

SMA-seeded EMA uses alpha=2/(span+1). MACD=EMA12-EMA26, DEA=EMA9
of valid DIF, histogram=2*(DIF-DEA); output requires 78 contiguous bars.
Wilder RSI/ATR seed the first 14 changes/TR values, then alpha=1/14.
RSI with no gains AND no losses is unknown, not a manufactured neutral value.
Every missing/invalid session resets warmup; suspended bars are never filled.
"""

from __future__ import annotations

import math
from datetime import date
from statistics import mean, stdev

from quantlab.scout.models import finite

VERSION = "technical_d0_v1_120_sma_seed_wilder14_macd2"
HISTORY_SESSIONS = 120
MACD_WARMUP = 78
PARAMETERS = {
    "history_sessions": HISTORY_SESSIONS,
    "ma_windows": [5, 10, 20, 60],
    "ma20_slope_lag": 5,
    "macd": [12, 26, 9],
    "macd_histogram_multiplier": 2,
    "macd_warmup": MACD_WARMUP,
    "rsi_atr_wilder": 14,
    "volatility": "sample_std_20_daily_simple_returns_not_annualized",
    "price_anchor": "D0_factor_research_only",
    "state": "descriptive_only_no_return_claim",
    "state_thresholds": {
        "breakout_strictly_positive": 0,
        "close_to_ma20_strictly_positive": 0,
        "ma20_slope_strictly_positive": 0,
    },
}


def ema(values: list[float], span: int) -> list[float | None]:
    """SMA seed at span-1, with no fabricated leading EMA values."""
    output: list[float | None] = [None] * len(values)
    if len(values) < span:
        return output
    previous = mean(values[:span])
    output[span - 1] = previous
    alpha = 2 / (span + 1)
    for index in range(span, len(values)):
        previous += alpha * (values[index] - previous)
        output[index] = previous
    return output


def wilder(values: list[float], span: int = 14) -> float | None:
    if len(values) < span:
        return None
    result = mean(values[:span])
    for value in values[span:]:
        result += (value - result) / span
    return result


def technical_snapshot(
    instrument_id: str,
    bars: list,
    factors: dict[tuple[str, date], float],
    sessions: list[date],
    asof: date,
    basic=None,
) -> dict:
    """Build one snapshot from dates <= D0, with a fixed D0 adjustment anchor.

    The full daily dependency sequence is returned as history for local archiving.
    short_context is deliberately not an additional model-visible numeric source;
    the program can expose a bounded subset through the common fact table.
    """
    days = sorted(set(day for day in sessions if day <= asof))[-HISTORY_SESSIONS:]
    by_day = {}
    duplicates = set()
    for bar in bars:
        if bar.trade_date > asof:
            continue
        if bar.trade_date in by_day:
            duplicates.add(bar.trade_date)
        by_day[bar.trade_date] = bar
    anchor = factors.get((instrument_id, asof))
    history = []
    segment = []
    for day in days:
        bar = by_day.get(day)
        factor = factors.get((instrument_id, day))
        status = "available"
        if not bar:
            status = "missing_bar"
        elif day in duplicates:
            status = "duplicate_bar"
        elif not finite(anchor) or anchor <= 0 or not finite(factor) or factor <= 0:
            status = "missing_adjustment"
        elif not all(
            finite(getattr(bar, key))
            for key in ("open", "high", "low", "close", "volume", "amount")
        ):
            status = "invalid_bar"
        elif bar.volume == 0 or bar.amount == 0:
            status = "no_trading_activity"
        elif (
            bar.low <= 0
            or bar.volume < 0
            or bar.amount < 0
            or not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        ):
            status = "invalid_bar"
        if status == "available":
            multiplier = factor / anchor
            adjusted = {
                key: getattr(bar, key) * multiplier for key in ("open", "high", "low", "close")
            }
            if not all(finite(value) and value > 0 for value in adjusted.values()):
                status = "invalid_adjusted_bar"
        item = {"session": day.isoformat(), "status": status}
        if status == "available":
            item.update(adjusted)
            item.update(
                {
                    "volume_shares": bar.volume,
                    "amount_cny": bar.amount,
                    "adj_factor": factor,
                    "raw_ohlc": [bar.open, bar.high, bar.low, bar.close],
                }
            )
            segment.append(item)
        else:
            segment = []
        history.append(item)
    if not days or days[-1] != asof:
        segment = []
    n = len(segment)
    values = {}
    current_status = history[-1]["status"] if history and days[-1] == asof else "missing_bar"

    def put(key, value, period, unit="ratio", required=1, status=None, deps="OHLC+adj_factor"):
        actual_status = status or ("available" if n >= required else "insufficient_warmup")
        if current_status != "available":
            actual_status = current_status
        if value is not None and not finite(value):
            value, actual_status = None, "invalid_calculation"
        if actual_status != "available":
            value = None
        values[key] = {
            "value": value,
            "unit": unit,
            "period": period,
            "status": actual_status,
            "subject_id": instrument_id,
            "asof_session": asof.isoformat(),
            "benchmark": None,
            "calculation_version": VERSION,
            "raw_dependencies": {
                "fields": deps,
                "required_contiguous_sessions": required,
                "observed_contiguous_sessions": n,
                "anchor_session": asof.isoformat(),
            },
        }

    closes = [row["close"] for row in segment]
    close = closes[-1] if n else None
    short_changes = [new - old for old, new in zip(closes[:-1], closes[1:], strict=True)][-10:]
    # A compact, fact-bound equivalent of the last ten daily close directions.
    # These remain one correlated price family, never ten independent catalysts.
    put(
        "positive_close_sessions_10d",
        sum(value > 0 for value in short_changes) if n >= 11 else None,
        "10d",
        "count",
        11,
    )
    put(
        "negative_close_sessions_10d",
        sum(value < 0 for value in short_changes) if n >= 11 else None,
        "10d",
        "count",
        11,
    )
    put("return_10d", close / closes[-11] - 1 if n >= 11 else None, "10d", required=11)
    for window in (5, 10, 20, 60):
        ma = mean(closes[-window:]) if n >= window else None
        put(f"ma{window}", ma, f"{window}d", "adjusted_CNY", window)
    ma20 = values["ma20"]["value"]
    lag_ma20 = mean(closes[-25:-5]) if n >= 25 else None
    put("ma20_slope_5d", ma20 / lag_ma20 - 1 if lag_ma20 else None, "20d_lag5d", required=25)
    put("close_to_ma20", close / ma20 - 1 if ma20 else None, "20d", required=20)
    for window in (20, 60):
        lower = min((row["low"] for row in segment[-window:]), default=None)
        upper = max((row["high"] for row in segment[-window:]), default=None)
        zero = n >= window and upper == lower
        position = (close - lower) / (upper - lower) if n >= window and not zero else None
        put(
            f"range_position_{window}d",
            position,
            f"{window}d_including_D0",
            required=window,
            status="zero_range" if zero else None,
        )
    prior_high = max((row["high"] for row in segment[-21:-1]), default=None) if n >= 21 else None
    put("prior_high_20d", prior_high, "20d_excluding_D0", "adjusted_CNY", 21)
    distance = close / prior_high - 1 if prior_high else None
    put("breakout_20d", distance, "20d_excluding_D0", required=21)
    put("distance_to_breakout_reference", distance, "20d_excluding_D0", required=21)
    peak = max(closes[-20:]) if n >= 20 else None
    put(
        "drawdown_from_peak_close_20d",
        close / peak - 1 if peak else None,
        "20d_including_D0",
        required=20,
    )
    maximum = None
    if n >= 20:
        rolling_peak = closes[-20]
        maximum = 0.0
        for value in closes[-20:]:
            rolling_peak = max(rolling_peak, value)
            maximum = min(maximum, value / rolling_peak - 1)
    put("max_drawdown_20d", maximum, "20d_including_D0", required=20)
    latest = segment[-1] if n else {}
    amplitude = latest.get("high", 0) - latest.get("low", 0)
    candle = {
        "body_ratio": abs(latest.get("close", 0) - latest.get("open", 0)),
        "upper_shadow_ratio": latest.get("high", 0) - max(latest.get("open", 0), close or 0),
        "lower_shadow_ratio": min(latest.get("open", 0), close or 0) - latest.get("low", 0),
        "close_location": (close or 0) - latest.get("low", 0),
    }
    for key, numerator in candle.items():
        put(
            key,
            numerator / amplitude if amplitude > 0 else None,
            "1d",
            status="zero_range" if n and amplitude == 0 else None,
        )
    put("open_gap_1d", latest["open"] / closes[-2] - 1 if n >= 2 else None, "1d", required=2)
    for field, unit in (("volume_shares", "shares"), ("amount_cny", "CNY")):
        put(field, latest.get(field), "1d", unit, deps=field)
        denominator = mean(row[field] for row in segment[-6:-1]) if n >= 6 else None
        name = "volume_ratio_5d" if field == "volume_shares" else "amount_ratio_5d"
        put(
            name,
            latest[field] / denominator if denominator else None,
            "5d_excluding_D0",
            "times",
            6,
            deps=field,
        )
    turnover = getattr(basic, "turnover_rate", None)
    put(
        "turnover_rate_pct",
        turnover * 100 if finite(turnover) else None,
        "1d",
        "percent",
        status=None if finite(turnover) else "missing_daily_basic",
        deps="daily_basic.turnover_rate",
    )
    circ_mv = getattr(basic, "circ_mv", None)
    put(
        "circ_mv_cny",
        circ_mv if finite(circ_mv) and circ_mv > 0 else None,
        "1d",
        "CNY",
        status=None if finite(circ_mv) and circ_mv > 0 else "missing_daily_basic",
        deps="daily_basic.circ_mv",
    )
    changes = [new - old for old, new in zip(closes[:-1], closes[1:], strict=True)]
    gain = wilder([max(x, 0) for x in changes])
    loss = wilder([max(-x, 0) for x in changes])
    rsi = None if gain is None or loss is None or gain + loss == 0 else 100 * gain / (gain + loss)
    put(
        "rsi14",
        rsi,
        "14d_wilder",
        "index_0_100",
        15,
        status="no_price_change" if gain == loss == 0 else None,
    )
    tr = [
        max(row["high"] - row["low"], abs(row["high"] - previous), abs(row["low"] - previous))
        for row, previous in zip(segment[1:], closes[:-1], strict=True)
    ]
    atr = wilder(tr)
    put("atr14", atr, "14d_wilder", "adjusted_CNY", 15)
    put(
        "atr14_to_close",
        atr / close if atr is not None and close else None,
        "14d_wilder",
        required=15,
    )
    returns = [new / old - 1 for old, new in zip(closes[:-1], closes[1:], strict=True)]
    put(
        "volatility_20d",
        stdev(returns[-20:]) if n >= 21 else None,
        "20d_sample_std_not_annualized",
        required=21,
    )
    fast, slow = ema(closes, 12), ema(closes, 26)
    difs = [fast[index] - slow[index] for index in range(25, n)] if n >= 26 else []
    deas = ema(difs, 9)
    dif = difs[-1] if difs else None
    dea = deas[-1] if deas else None
    hist = 2 * (dif - dea) if dea is not None else None
    previous_hist = 2 * (difs[-2] - deas[-2]) if len(deas) > 1 and deas[-2] is not None else None
    macd = {
        "macd_dif": dif,
        "macd_dea": dea,
        "macd_hist2": hist,
        "macd_hist2_change_1d": hist - previous_hist if previous_hist is not None else None,
    }
    for key, value in macd.items():
        put(key, value, "12_26_9_sma_seed", "adjusted_CNY", MACD_WARMUP)
        put(
            key + "_to_close",
            value / close if value is not None and close else None,
            "12_26_9_sma_seed",
            required=MACD_WARMUP,
        )
    state = "insufficient_evidence"
    if ma20 is not None and values["ma20_slope_5d"]["value"] is not None:
        slope = values["ma20_slope_5d"]["value"]
        state = (
            "breakout_expansion"
            if distance is not None and distance > 0
            else "trend_continuation"
            if close > ma20 and slope > 0
            else "pullback_repair"
            if close > ma20 and n >= 2 and closes[-1] > closes[-2]
            else "range_or_weak"
        )
    return {
        "version": VERSION,
        "parameters": PARAMETERS,
        "subject_id": instrument_id,
        "asof_session": asof.isoformat(),
        "history_sessions": len(days),
        "contiguous_sessions": n,
        "history_complete": len(days) == HISTORY_SESSIONS
        and all(row["status"] == "available" for row in history),
        "status": current_status,
        "technical_state": state,
        "metrics": values,
        "short_context": history[-10:],
        "history": history,
    }


def snapshot_facts(candidate: dict, asof_session: str | None = None) -> list[dict]:
    snapshot = candidate.get("technical_snapshot") or candidate.get("context", {}).get(
        "technical_snapshot"
    )
    if not snapshot:
        return []
    facts = []
    code = candidate["instrument_id"]
    for metric, item in snapshot.get("metrics", {}).items():
        value = item["value"]
        if value is not None and (not finite(value) or not math.isfinite(value)):
            raise ValueError("Technical fact is non-finite")
        facts.append(
            {
                "fact_id": f"fact:{code}:technical:{metric}",
                "subject_id": code,
                "metric": metric,
                "period": item["period"],
                "value": str(value) if value is not None else None,
                "unit": item["unit"],
                "status": item["status"],
                "asof_session": item.get("asof_session") or asof_session,
                "benchmark": item.get("benchmark"),
                "calculation_version": VERSION,
                "raw_dependencies": item["raw_dependencies"],
                "source_location": f"candidate:{code}:technical_snapshot:metrics:{metric}",
            }
        )
    return facts
