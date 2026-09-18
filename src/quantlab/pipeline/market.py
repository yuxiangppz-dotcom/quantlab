"""Canonical market evidence -> existing integer-share/cash execution kernel."""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

import pandas as pd

from quantlab.pipeline.ingestion import verify_session
from quantlab.research.ml.io import decode_market_day, read_corporate_actions

# Vendor floats carry sub-fen representation noise (e.g. ...987.00000003 CNY).
# Anything within 0.01 fen of an integral value after scaling is representation
# error, not information; genuine finer precision stays rejected.
EXACT_NOISE_TOLERANCE = Decimal("0.01")
# stk_limit marks securities without a price limit (new listings, restructuring
# resumptions) with placeholder values; those are "no gate applies", not prices.
# Source: https://tushare.pro/document/2?doc_id=183
NO_PRICE_LIMIT_SENTINELS = (Decimal("999999.999"), Decimal("99999.999"))


def exact_integer(value, scale=1):
    number = Decimal(str(value)) * scale
    if not number.is_finite() or number < 0:
        raise ValueError("market quantity/price is not representable in declared units")
    rounded = number.to_integral_value(rounding=ROUND_HALF_UP)
    if abs(number - rounded) > EXACT_NOISE_TOLERANCE:
        raise ValueError("market quantity/price is not representable in declared units")
    return int(rounded)


def declared_limit(value):
    """Translate the vendor's no-limit placeholders into an absent gate."""
    if value is None:
        return None
    raw = Decimal(str(value))
    if any(abs(raw - sentinel) <= Decimal("0.001") for sentinel in NO_PRICE_LIMIT_SENTINELS):
        return None
    return value


def close_market_open(bar, records):
    """Interpret suspend_d at the modeled 15:00 close, not as a membership blacklist.

    R means resumption. Explicit S intervals ending before close do not block close.
    Unordered full-day S + R, unknown types and malformed timing remain unknown.
    Source: https://tushare.pro/document/2?doc_id=214
    """
    import re
    from datetime import time

    if any(r.suspend_type not in {"S", "R"} for r in records):
        return None
    suspensions = [r for r in records if r.suspend_type == "S"]
    resumed = any(r.suspend_type == "R" for r in records)
    closing_halt = False
    for row in suspensions:
        if not row.suspend_timing or not row.suspend_timing.strip():
            closing_halt = True
            continue
        for interval in re.split(r"[,;，；]", row.suspend_timing):
            match = re.fullmatch(
                r"\s*(\d{2}:\d{2}(?::\d{2})?)\s*-\s*(\d{2}:\d{2}(?::\d{2})?)\s*",
                interval,
            )
            if match is None:
                return None
            try:
                start, end = (time.fromisoformat(x) for x in match.groups())
            except ValueError:
                return None
            if start >= end:
                return None
            closing_halt |= start <= time(15) <= end
    if closing_halt:
        return None if resumed else False
    return True if bar is not None else None


def market_day(
    storage,
    receipts,
    sessions,
    day,
    instruments,
    policy,
    corporate_path,
    *,
    hour,
    sparse_scope=False,
):
    verify_session(storage, receipts, day)
    read_corporate_actions(corporate_path, day, day)
    i = sessions.index(day)
    if i < 20 or i + 1 >= len(sessions):
        raise ValueError("market adapter needs 20 prior sessions and one next session")
    bars = {b.instrument_id: b for b in storage.load_daily_bars_by_date(day)}
    limits = {r.instrument_id: r for r in storage.load_daily_price_limits_by_date(day)}
    suspensions = {}
    for row in storage.load_suspensions_v1_by_date(day):
        if row.trade_date != day:
            raise ValueError("suspension evidence date mismatch")
        suspensions.setdefault(row.instrument_id, []).append(row)
    history = []
    for prior in sessions[i - 20 : i]:
        verify_session(storage, receipts, prior)
        history.append({b.instrument_id: b for b in storage.load_daily_bars_by_date(prior)})
    cutoff = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=hour)
    contexts, marks = [], []
    for code in sorted(instruments):
        if sparse_scope and code not in bars and code not in limits and code not in suspensions:
            # Historical context also contains long-removed securities. Do not
            # fabricate their daily contexts. If a replay actually holds/orders
            # this name, the quantity scheduler still rejects missing evidence.
            continue
        policies = [
            p
            for p in policy["policies"]
            if code in p["instruments"]
            and date.fromisoformat(p["start"]) <= day <= date.fromisoformat(p["end"])
        ]
        if len(policies) != 1:
            raise ValueError(f"missing/overlapping dated execution policy:{code}:{day}")
        selected = policies[0]
        known = pd.Timestamp(selected["known_at"])
        if not selected.get("source_id") or known.tzinfo is None or known > cutoff:
            raise ValueError("execution policy unavailable at decision cutoff")
        bar, limit = bars.get(code), limits.get(code)
        amounts = [exact_integer(h[code].amount, 100) for h in history if code in h]
        average = (
            int((Decimal(sum(amounts)) / 20).to_integral_value(rounding=ROUND_FLOOR))
            if len(amounts) == 20
            else None
        )
        item = {
            "instrument_id": code,
            "execution_date": str(day),
            "next_session": str(sessions[i + 1]),
            "evidence_date": str(day),
            "calendar_verified": True,
            "market_open": close_market_open(bar, suspensions.get(code, [])),
            "corporate_actions_processed": True,
            "raw_close_fen": exact_integer(bar.close, 100) if bar else None,
            "low_fen": exact_integer(bar.low, 100) if bar else None,
            "high_fen": exact_integer(bar.high, 100) if bar else None,
            "down_limit_fen": exact_integer(declared_limit(limit.down_limit), 100)
            if limit and declared_limit(limit.down_limit) is not None
            else None,
            "up_limit_fen": exact_integer(declared_limit(limit.up_limit), 100)
            if limit and declared_limit(limit.up_limit) is not None
            else None,
            "prior20_amount_fen": average,
            "prior20_asof": str(sessions[i - 1]),
            "prior20_sessions": len(amounts),
            "session_amount_fen": exact_integer(bar.amount, 100) if bar else None,
            "session_volume_shares": exact_integer(bar.volume) if bar else None,
            "participation": selected["participation"],
            "rules": selected["rules"],
            "fees": selected["fees"],
        }
        contexts.append(item)
        if bar:
            marks.append(
                {"instrument_id": code, "session": str(day), "price_fen": item["raw_close_fen"]}
            )
    # A separate explicitly sourced mark is required for a missing/suspended held asset.
    for mark in policy.get("valuation_overrides", []):
        if mark["session"] != str(day) or mark["instrument_id"] not in instruments:
            continue
        known = pd.Timestamp(mark["known_at"])
        if not mark.get("source_id") or known.tzinfo is None or known > cutoff:
            raise ValueError("valuation evidence unavailable")
        if any(m["instrument_id"] == mark["instrument_id"] for m in marks):
            raise ValueError("valuation override conflicts with observed bar")
        marks.append({k: mark[k] for k in ("instrument_id", "session", "price_fen")})
    valuation_log = []
    stale = policy.get("stale_valuation")
    if stale:
        max_age = stale.get("max_sessions")
        known = pd.Timestamp(stale["known_at"])
        if (
            stale.get("mode") != "last_raw_close_known_halt"
            or type(max_age) is not int
            or not 1 <= max_age <= 20
            or not stale.get("source_id")
            or known.tzinfo is None
            or known > cutoff
        ):
            raise ValueError("invalid declared stale valuation policy")
        marked = {m["instrument_id"] for m in marks}
        for item in contexts:
            code = item["instrument_id"]
            if code in marked or item["market_open"] is not False:
                continue
            for age in range(1, max_age + 1):
                prior = sessions[i - age]
                previous = history[-age].get(code)
                if previous is None:
                    records = [
                        r
                        for r in storage.load_suspensions_v1_by_date(prior)
                        if r.instrument_id == code
                    ]
                    if close_market_open(None, records) is not False:
                        break  # An unexplained missing bar cannot be bridged.
                    continue
                events = read_corporate_actions(corporate_path, prior, day)
                if any(e.instrument_id == code and prior < e.ex_date <= day for e in events):
                    break  # Carrying a pre-action raw mark would double-count entitlement.
                marks.append(
                    {
                        "instrument_id": code,
                        "session": str(day),
                        "price_fen": exact_integer(previous.close, 100),
                    }
                )
                valuation_log.append(
                    {
                        "instrument_id": code,
                        "source_session": str(prior),
                        "stale_sessions": age,
                        "policy_source": stale["source_id"],
                        "tradable_price_created": False,
                    }
                )
                break
    payload = {
        "session": str(day),
        "contexts": contexts,
        "marks": marks,
        "corporate_processing_complete": True,
        "valuation_provenance": valuation_log,
    }
    decode_market_day(payload)  # Run the kernel's type/rate/date validations now.
    return payload


def load_policy(path):
    policy = json.loads(path.read_text())
    if policy.get("schema") != "quantlab_execution_evidence_v1" or not policy.get("policies"):
        raise ValueError("explicit dated fees, quantity rules, participation and sources required")
    return policy
