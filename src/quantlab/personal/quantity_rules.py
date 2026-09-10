"""Quantity-rule resolution for conservative personal reference plans.

Reference planning prefers the authoritative point-in-time execution rule book.
When that book intentionally has no coverage, the current product may retain its
existing engineering fallback, but the fallback remains explicit, unverified,
and incapable of creating execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from quantlab.data.models import Security
from quantlab.execution.rules import PITRuleBook, default_a_share_rule_book

_AUTHORITATIVE_SCOPE = {
    ("SSE", "主板"): "MAIN",
    ("SSE", "科创板"): "STAR",
    ("SZSE", "主板"): "MAIN",
    ("SZSE", "创业板"): "CHINEXT",
}

_ENGINEERING_FALLBACK = {
    ("SSE", "主板"): (100, 100, 100, 100, True, "reference_fallback_sse_main_v1"),
    ("SSE", "科创板"): (200, 1, 200, 1, True, "reference_fallback_sse_star_v1"),
    ("SZSE", "主板"): (100, 100, 100, 100, True, "reference_fallback_szse_main_v1"),
    ("SZSE", "创业板"): (
        100,
        100,
        100,
        100,
        True,
        "reference_fallback_szse_chinext_v1",
    ),
}


@dataclass(frozen=True)
class ReferenceQuantityRule:
    rule_id: str
    rule_status: str
    buy_min_quantity: int
    buy_quantity_step: int
    sell_min_quantity: int
    sell_quantity_step: int
    allow_full_odd_lot_exit: bool
    limitations: tuple[str, ...]

    @property
    def authoritative(self) -> bool:
        return self.rule_status == "authoritative_pit_rule"


def resolve_reference_quantity_rule(
    security: Security,
    intended_session: date,
    *,
    rule_book: PITRuleBook | None = None,
) -> ReferenceQuantityRule | None:
    """Resolve quantity rules for one security/session without inventing authority."""
    scope = _AUTHORITATIVE_SCOPE.get((security.exchange, security.board))
    if scope is None:
        return None

    book = rule_book or default_a_share_rule_book()
    rule = book.resolve(security.exchange, scope, intended_session)
    if rule is not None:
        return ReferenceQuantityRule(
            rule_id=rule.rule_id,
            rule_status="authoritative_pit_rule",
            buy_min_quantity=rule.buy_min_quantity,
            buy_quantity_step=rule.buy_quantity_step,
            sell_min_quantity=rule.sell_min_quantity,
            sell_quantity_step=rule.sell_quantity_step,
            allow_full_odd_lot_exit=rule.allow_full_odd_lot_exit,
            limitations=rule.limitations,
        )

    fallback = _ENGINEERING_FALLBACK.get((security.exchange, security.board))
    if fallback is None:
        return None
    buy_min, buy_step, sell_min, sell_step, full_exit, rule_id = fallback
    return ReferenceQuantityRule(
        rule_id=rule_id,
        rule_status="engineering_fallback_unverified",
        buy_min_quantity=buy_min,
        buy_quantity_step=buy_step,
        sell_min_quantity=sell_min,
        sell_quantity_step=sell_step,
        allow_full_odd_lot_exit=full_exit,
        limitations=(
            "authoritative PIT rule-book coverage is unavailable for the intended session",
            "fallback quantities are engineering assumptions and require user review before trade",
        ),
    )


def floor_reference_buy_quantity(raw_quantity: int, rule: ReferenceQuantityRule) -> int:
    """Floor a desired buy quantity to the resolved minimum/step contract."""
    if raw_quantity < rule.buy_min_quantity:
        return 0
    return rule.buy_min_quantity + (
        (raw_quantity - rule.buy_min_quantity) // rule.buy_quantity_step
    ) * rule.buy_quantity_step


def floor_reference_sell_quantity(
    *,
    desired_quantity: int,
    sellable_quantity: int,
    total_position_quantity: int,
    rule: ReferenceQuantityRule,
) -> int:
    """Floor a reference sale while preserving authoritative full odd-lot exits."""
    candidate = min(desired_quantity, sellable_quantity)
    if candidate <= 0:
        return 0
    if (
        rule.allow_full_odd_lot_exit
        and desired_quantity == total_position_quantity
        and candidate == total_position_quantity
    ):
        return candidate
    if candidate < rule.sell_min_quantity:
        return 0
    return rule.sell_min_quantity + (
        (candidate - rule.sell_min_quantity) // rule.sell_quantity_step
    ) * rule.sell_quantity_step
