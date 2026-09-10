from __future__ import annotations

from datetime import date

from quantlab.data.models import Security
from quantlab.personal.quantity_rules import (
    floor_reference_buy_quantity,
    floor_reference_sell_quantity,
    resolve_reference_quantity_rule,
)


def _security(exchange: str, market: str, board: str) -> Security:
    return Security(
        instrument_id="000001.SZ" if exchange == "SZSE" else "600000.SH",
        symbol="000001" if exchange == "SZSE" else "600000",
        name="测试",
        exchange=exchange,
        market=market,
        board=board,
        list_status="L",
        list_date=date(2000, 1, 1),
        delist_date=None,
    )


def test_sse_main_covered_date_uses_authoritative_pit_rule() -> None:
    rule = resolve_reference_quantity_rule(
        _security("SSE", "SH", "主板"),
        date(2024, 1, 2),
    )

    assert rule is not None
    assert rule.rule_status == "authoritative_pit_rule"
    assert rule.authoritative is True
    assert rule.rule_id == "sse-main-2023"
    assert (rule.buy_min_quantity, rule.buy_quantity_step) == (100, 100)


def test_sse_star_covered_date_uses_star_quantity_contract() -> None:
    rule = resolve_reference_quantity_rule(
        _security("SSE", "SH", "科创板"),
        date(2024, 1, 2),
    )

    assert rule is not None
    assert rule.rule_status == "authoritative_pit_rule"
    assert rule.rule_id == "sse-star-2023"
    assert (rule.buy_min_quantity, rule.buy_quantity_step) == (200, 1)
    assert floor_reference_buy_quantity(237, rule) == 237


def test_szse_chinext_covered_date_uses_authoritative_rule() -> None:
    rule = resolve_reference_quantity_rule(
        _security("SZSE", "SZ", "创业板"),
        date(2022, 1, 4),
    )

    assert rule is not None
    assert rule.rule_status == "authoritative_pit_rule"
    assert rule.rule_id == "szse-chinext-special-2021"
    assert (rule.buy_min_quantity, rule.buy_quantity_step) == (100, 100)


def test_uncovered_2026_session_uses_explicit_unverified_fallback() -> None:
    rule = resolve_reference_quantity_rule(
        _security("SSE", "SH", "主板"),
        date(2026, 9, 10),
    )

    assert rule is not None
    assert rule.authoritative is False
    assert rule.rule_status == "engineering_fallback_unverified"
    assert rule.rule_id == "reference_fallback_sse_main_v1"
    assert "unavailable" in rule.limitations[0]


def test_unsupported_exchange_board_identity_returns_unknown() -> None:
    assert (
        resolve_reference_quantity_rule(
            _security("BSE", "BJ", "北交所"),
            date(2026, 9, 10),
        )
        is None
    )
    assert (
        resolve_reference_quantity_rule(
            _security("SSE", "SH", "创业板"),
            date(2024, 1, 2),
        )
        is None
    )


def test_rounding_and_full_odd_lot_exit_follow_resolved_rule() -> None:
    rule = resolve_reference_quantity_rule(
        _security("SSE", "SH", "主板"),
        date(2024, 1, 2),
    )
    assert rule is not None

    assert floor_reference_buy_quantity(99, rule) == 0
    assert floor_reference_buy_quantity(278, rule) == 200
    assert (
        floor_reference_sell_quantity(
            desired_quantity=278,
            sellable_quantity=278,
            total_position_quantity=278,
            rule=rule,
        )
        == 278
    )
    assert (
        floor_reference_sell_quantity(
            desired_quantity=178,
            sellable_quantity=178,
            total_position_quantity=278,
            rule=rule,
        )
        == 100
    )
