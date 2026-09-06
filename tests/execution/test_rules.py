from dataclasses import replace
from datetime import date

import pytest

from quantlab.execution import (
    ExecutionValidationError,
    PITRuleBook,
    Side,
    TradingCalendar,
    default_a_share_rule_book,
)


def test_board_and_effective_date_quantity_rules() -> None:
    rules = default_a_share_rule_book()
    main = rules.resolve("SSE", "MAIN", date(2021, 1, 4))
    star = rules.resolve("SSE", "STAR", date(2021, 1, 4))
    chinext_before = rules.resolve("SZSE", "CHINEXT", date(2020, 8, 21))
    chinext_after = rules.resolve("SZSE", "CHINEXT", date(2020, 8, 24))
    assert main is not None and star is not None
    assert chinext_before is not None and chinext_after is not None

    assert main.quantity_is_admissible(
        side=Side.BUY, quantity=200, total_position_quantity=0
    )
    assert not main.quantity_is_admissible(
        side=Side.BUY, quantity=201, total_position_quantity=0
    )
    assert star.quantity_is_admissible(
        side=Side.BUY, quantity=201, total_position_quantity=0
    )
    assert not star.quantity_is_admissible(
        side=Side.BUY, quantity=199, total_position_quantity=0
    )
    assert chinext_before.quantity_is_admissible(
        side=Side.BUY, quantity=400_000, total_position_quantity=0
    )
    assert not chinext_after.quantity_is_admissible(
        side=Side.BUY, quantity=400_000, total_position_quantity=0
    )


def test_full_odd_lot_exit_but_not_arbitrary_odd_lot() -> None:
    rule = default_a_share_rule_book().resolve("SSE", "MAIN", date(2022, 1, 4))
    assert rule is not None
    assert rule.quantity_is_admissible(
        side=Side.SELL, quantity=150, total_position_quantity=150
    )
    assert not rule.quantity_is_admissible(
        side=Side.SELL, quantity=150, total_position_quantity=250
    )


def test_rule_sources_are_exchange_primary_and_byte_hashed() -> None:
    rule_book = default_a_share_rule_book()
    assert rule_book.rules
    for rule in rule_book.rules:
        assert rule.sources
        assert rule.sellability_lag_sessions == 1
        for source in rule.sources:
            assert source.url.startswith("https://")
            assert source.authority in {
                "Shanghai Stock Exchange",
                "Shenzhen Stock Exchange",
            }
            assert len(source.document_sha256) == 64


def test_current_rule_is_never_applied_retroactively_or_beyond_coverage() -> None:
    rules = default_a_share_rule_book()
    historical = rules.resolve("SSE", "MAIN", date(2020, 3, 12))
    replacement = rules.resolve("SSE", "MAIN", date(2020, 3, 13))
    assert historical is not None and historical.version == "2018"
    assert replacement is not None and replacement.version == "2020.2"
    assert rules.resolve("SSE", "MAIN", date(2026, 1, 5)) is None
    # The missing byte-verified 2023 SZSE attachment is represented as a gap.
    assert rules.resolve("SZSE", "MAIN", date(2024, 1, 5)) is None


def test_overlapping_rules_fail_at_construction() -> None:
    original = default_a_share_rule_book().rules[0]
    overlapping = replace(original, rule_id="overlap", effective_from=date(2020, 1, 1))
    with pytest.raises(ExecutionValidationError, match="overlapping"):
        PITRuleBook((original, overlapping))


def test_calendar_next_session_is_explicit_and_weekend_safe() -> None:
    friday = date(2026, 1, 9)
    monday = date(2026, 1, 12)
    calendar = TradingCalendar(
        sessions=(friday, monday),
        coverage_start=friday,
        coverage_end=monday,
        source_id="synthetic-calendar",
        source_sha256="a" * 64,
    )
    assert calendar.next_session(friday) == monday
    assert calendar.next_session(date(2026, 1, 10)) is None
    assert calendar.session_status(date(2026, 1, 8)) is None
