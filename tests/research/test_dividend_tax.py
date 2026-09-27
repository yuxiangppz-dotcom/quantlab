"""Hand-calculated personal A-share dividend tax cases under 2015/101."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from quantlab.research.ml.corporate import (
    CorporateEvent,
    apply_events,
    capture_entitlements,
    new_state,
    receivable_value,
)
from quantlab.research.ml.dividend_tax import charge_sale_tax, individual_dividend_tax_rate
from quantlab.research.quantity_kernel import ResearchBook, ResearchLot


def test_natural_month_and_year_boundaries_are_inclusive():
    bought = date(2023, 1, 8)
    assert individual_dividend_tax_rate(bought, date(2023, 2, 8)) == Decimal("0.20")
    assert individual_dividend_tax_rate(bought, date(2023, 2, 9)) == Decimal("0.10")
    assert individual_dividend_tax_rate(bought, date(2024, 1, 8)) == Decimal("0.10")
    assert individual_dividend_tax_rate(bought, date(2024, 1, 9)) == 0
    assert individual_dividend_tax_rate(date(2024, 1, 31), date(2024, 2, 29)) == Decimal(
        "0.20"
    )


def test_record_lots_partial_sale_before_pay_and_later_sale():
    record, ex, pay = date(2024, 2, 1), date(2024, 2, 2), date(2024, 2, 5)
    event = CorporateEvent(
        "declared-dividend",
        "A",
        "cash_dividend",
        record,
        ex,
        pay,
        "issuer-confirmed-synthetic",
        net_cash_per_share_fen=Decimal(100),
        gross_cash_per_share_fen=Decimal(100),
        tax_treatment="individual_a_share_2015",
        tax_evidence_sha256="0" * 64,
        tax_evidence_url="https://example.invalid/synthetic-issuer-notice",
    )
    old = ResearchLot("old", "A", 100, date(2024, 1, 1), date(2024, 1, 2))
    young = ResearchLot("young", "A", 100, date(2024, 1, 15), date(2024, 1, 16))
    record_book = ResearchBook(record, 0, (old, young))
    state = capture_entitlements(new_state(), record_book, (event,))
    assert len(state["tax_claims"]) == 2
    ex_book, state, _ = apply_events(record_book, ex, (event,), state, date(2024, 1, 1))
    assert receivable_value(state) == 20_000  # gross CNY 200, even before payment

    # The quantity kernel consumes the older 100 shares first, then 20 young shares.
    after_sale = replace(ex_book, lots=(replace(young, quantity=80),))
    state, tax, rows = charge_sale_tax(state, ex_book, after_sale, ex)
    assert tax == 1_400  # 100 * CNY 1 * 10% + 20 * CNY 1 * 20%
    assert [(r["lot_id"], r["cash_fen"]) for r in rows] == [
        ("old", -1_000),
        ("young", -400),
    ]
    assert state["tax_claims"][0]["remaining"] == 80

    paid_book, state, _ = apply_events(after_sale, pay, (event,), state, date(2024, 1, 1))
    assert paid_book.cash_fen == 20_000  # payment does not erase the remaining tax claim
    last_sale = date(2024, 2, 20)
    state, tax, rows = charge_sale_tax(state, paid_book, replace(paid_book, lots=()), last_sale)
    assert tax == 800  # the young lot now exceeds one month
    assert rows[0]["cash_fen"] == -800
    assert state["tax_claims"] == []


def test_tax_claim_needs_proven_gross_payment_and_no_upfront_withholding():
    base = CorporateEvent(
        "d", "A", "cash_dividend", date(2024, 1, 2), date(2024, 1, 3),
        date(2024, 1, 5), "synthetic", net_cash_per_share_fen=Decimal(90),
    )
    with pytest.raises(ValueError, match="sourced gross cash"):
        replace(
            base, gross_cash_per_share_fen=Decimal(100),
            tax_treatment="individual_a_share_2015",
        )
    with pytest.raises(ValueError, match="internally consistent"):
        replace(base, gross_cash_per_share_fen=Decimal(100))


def test_one_sale_taxes_each_prior_dividend_claim_on_the_same_lot():
    record = date(2024, 1, 31)
    lot = ResearchLot("a", "A", 100, date(2023, 12, 31), date(2024, 1, 2))
    book = ResearchBook(record, 0, (lot,))
    events = tuple(
        CorporateEvent(
            f"d{i}", "A", "cash_dividend", record, date(2024, 2, 1),
            date(2024, 2, 2), "synthetic", net_cash_per_share_fen=Decimal(rate),
            gross_cash_per_share_fen=Decimal(rate), tax_treatment="individual_a_share_2015",
            tax_evidence_sha256="0" * 64,
            tax_evidence_url="https://example.invalid/synthetic-issuer-notice",
        )
        for i, rate in enumerate((100, 200))
    )
    state = capture_entitlements(new_state(), book, events)
    state, tax, rows = charge_sale_tax(
        state, book, replace(book, lots=(replace(lot, quantity=50),)), date(2024, 2, 1)
    )
    assert tax == 1_500  # 50 * (CNY 1 + CNY 2) * 10%
    assert [r["cash_fen"] for r in rows] == [-500, -1_000]
    assert [c["remaining"] for c in state["tax_claims"]] == [50, 50]


def test_share_action_after_tax_record_blocks_unproven_lot_lineage():
    record, ex = date(2024, 2, 1), date(2024, 2, 2)
    lot = ResearchLot("a", "A", 100, date(2024, 1, 1), date(2024, 1, 2))
    book = ResearchBook(record, 0, (lot,))
    cash = CorporateEvent(
        "d", "A", "cash_dividend", record, ex, date(2024, 2, 5), "synthetic",
        net_cash_per_share_fen=Decimal(100), gross_cash_per_share_fen=Decimal(100),
        tax_treatment="individual_a_share_2015",
        tax_evidence_sha256="0" * 64,
        tax_evidence_url="https://example.invalid/synthetic-issuer-notice",
    )
    shares = CorporateEvent(
        "s", "A", "bonus_shares", record, ex, ex, "synthetic",
        share_numerator=1, share_denominator=10,
    )
    state = capture_entitlements(new_state(), book, (cash, shares))
    with pytest.raises(ValueError, match="dividend_tax_share_lineage_unverified"):
        apply_events(book, ex, (cash, shares), state, date(2024, 1, 1))
