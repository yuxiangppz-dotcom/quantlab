"""Canonical market evidence -> existing integer-share/cash execution kernel."""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_FLOOR, Decimal

import pandas as pd

from quantlab.pipeline.ingestion import verify_session
from quantlab.research.ml.io import decode_market_day, read_corporate_actions


def exact_integer(value, scale=1):
    number = Decimal(str(value)) * scale
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("market quantity/price is not representable in declared units")
    return int(number)


def market_day(storage, receipts, sessions, day, instruments, policy, corporate_path, *, hour):
    verify_session(storage, receipts, day)
    read_corporate_actions(corporate_path, day, day)
    i = sessions.index(day)
    if i < 20 or i + 1 >= len(sessions):
        raise ValueError("market adapter needs 20 prior sessions and one next session")
    bars = {b.instrument_id: b for b in storage.load_daily_bars_by_date(day)}
    limits = {r.instrument_id: r for r in storage.load_daily_price_limits_by_date(day)}
    suspended = {r.instrument_id for r in storage.load_suspensions_v1_by_date(day)}
    history = []
    for prior in sessions[i - 20 : i]:
        verify_session(storage, receipts, prior)
        history.append({b.instrument_id: b for b in storage.load_daily_bars_by_date(prior)})
    cutoff = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=hour)
    contexts, marks = [], []
    for code in sorted(instruments):
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
            "market_open": False if code in suspended else (True if bar else None),
            "corporate_actions_processed": True,
            "raw_close_fen": exact_integer(bar.close, 100) if bar else None,
            "low_fen": exact_integer(bar.low, 100) if bar else None,
            "high_fen": exact_integer(bar.high, 100) if bar else None,
            "down_limit_fen": exact_integer(limit.down_limit, 100) if limit else None,
            "up_limit_fen": exact_integer(limit.up_limit, 100) if limit else None,
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
    payload = {
        "session": str(day),
        "contexts": contexts,
        "marks": marks,
        "corporate_processing_complete": True,
    }
    decode_market_day(payload)  # Run the kernel's type/rate/date validations now.
    return payload


def load_policy(path):
    policy = json.loads(path.read_text())
    if policy.get("schema") != "quantlab_execution_evidence_v1" or not policy.get("policies"):
        raise ValueError("explicit dated fees, quantity rules, participation and sources required")
    return policy
