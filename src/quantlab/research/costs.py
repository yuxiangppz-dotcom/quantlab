"""User-reported commission scenarios and dated tax components, never fee authority."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

DEFAULT_COST_PROFILE = Path(__file__).resolve().parents[3] / "config/research_user_costs_v1.json"


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def load_research_cost_profile(path: Path = DEFAULT_COST_PROFILE) -> dict:
    """Load the limited research schema; changes require a new evidence fingerprint."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != "quantlab_research_user_costs_v1":
        raise ValueError("unsupported research cost profile")
    required = {
        "commission_includes_stamp_duty": False,
        "broker_verified": False,
        "execution_authority": False,
        "performance_claim": False,
        "stock_stamp_duty_rule": "cn_a_sell_20230828_half",
        "etf_scope": "secondary_market_units_only_no_primary_redemption",
        "aggregation": "single_hypothetical_order_aggregate_filled_notional",
        "rounding": "each_component_half_up_to_integer_fen",
        "additional_fee_coverage": "unverified",
        "slippage_spread_impact": "not_included_unknown",
        "dividend_tax": "not_included_requires_holding_and_corporate_action_evidence",
        "historical_commission_mode": (
            "constant_current_quote_comparison_scenario_not_actual_historical_broker_fees"
        ),
    }
    for key, expected in required.items():
        if value.get(key) != expected or type(value.get(key)) is not type(expected):
            raise ValueError(f"unsupported research cost assumption: {key}")
    if not isinstance(value.get("commission_rate"), str):
        raise ValueError("commission_rate must be an explicit decimal string")
    try:
        rate = Decimal(value["commission_rate"])
    except InvalidOperation as exc:
        raise ValueError("commission_rate must be a valid decimal") from exc
    if not rate.is_finite() or not Decimal("0") < rate <= Decimal("0.003"):
        raise ValueError("commission_rate must be finite and in (0, 0.003]")
    for key in ("stock_minimum_commission_fen", "etf_minimum_commission_fen"):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    start = date.fromisoformat(value["supported_start"])
    reviewed = date.fromisoformat(value["rules_reviewed_through"])
    if not date(2020, 1, 1) <= start <= reviewed <= date(2026, 9, 11):
        raise ValueError("cost profile exceeds its reviewed date scope")
    return {**value, "profile_fingerprint": _fingerprint(value)}


def estimate_research_order_components(
    filled_notionals_fen: list[int],
    *,
    asset_type: str,
    side: str,
    trade_date: date,
    profile_path: Path = DEFAULT_COST_PROFILE,
) -> dict:
    """Estimate declared components for one hypothetical order, not a broker fill.

    Aggregating partial amounts before applying a minimum is a declared scenario.
    Separate orders must call separately; actual broker aggregation is unverified.
    The subtotal must never be consumed as a complete cost or an order fee cap.
    """
    if asset_type not in {"stock", "etf"} or side not in {"buy", "sell"}:
        raise ValueError("research cost supports only stock/etf and buy/sell")
    if type(trade_date) is not date:
        raise ValueError("trade_date must be a date, not an intraday timestamp")
    if not isinstance(filled_notionals_fen, list) or any(
        type(amount) is not int or amount < 0 for amount in filled_notionals_fen
    ):
        raise ValueError("filled notionals must be a list of nonnegative integer fen")
    profile = load_research_cost_profile(profile_path)
    if not (
        date.fromisoformat(profile["supported_start"])
        <= trade_date
        <= date.fromisoformat(profile["rules_reviewed_through"])
    ):
        raise ValueError("trade date outside the reviewed research fee scope")
    notional = sum(filled_notionals_fen)
    commission_rate = Decimal(profile["commission_rate"])
    stamp_rate = Decimal("0")
    if asset_type == "stock" and side == "sell":
        stamp_rate = Decimal("0.0005") if trade_date >= date(2023, 8, 28) else Decimal("0.001")
    commission = (
        max(
            profile[f"{asset_type}_minimum_commission_fen"],
            int((notional * commission_rate).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
        )
        if notional
        else 0
    )
    stamp = int((notional * stamp_rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return {
        "schema": "quantlab_research_order_cost_components_v1",
        "profile_fingerprint": profile["profile_fingerprint"],
        "asset_type": asset_type,
        "side": side,
        "trade_date": trade_date.isoformat(),
        "filled_notional_fen": notional,
        "commission_rate": str(commission_rate),
        "stamp_duty_rate": str(stamp_rate),
        "commission_fen": commission,
        "stamp_duty_fen": stamp,
        "known_components_subtotal_fen": commission + stamp,
        "additional_fees_fen": None,
        "slippage_spread_impact_fen": None,
        "dividend_tax_fen": None,
        "complete_trading_cost_fen": None,
        "aggregation": profile["aggregation"],
        "rounding": profile["rounding"],
        "broker_verified": False,
        "execution_authority": False,
        "performance_claim": False,
        "limitations": [
            "constant user-reported commission scenario, not verified historical broker fees",
            "0.86 interpreted as per 10000; additional fee coverage remains unverified",
            "subtotal is not complete trading cost, a reservation fee cap or a realized fill",
            "ETF exemption applies only to secondary-market unit trading",
        ],
    }
