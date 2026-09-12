import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal

import pytest

from quantlab.data.models import DataValidationError
from quantlab.research.corporate_recipients import RecipientEvidence, recipient_guard
from quantlab.research.corporate_terms import minimum_terms

PDF = b"%PDF-1.7 synthetic fixture; not an issuer source"


def terms(**updates):
    return minimum_terms(
        dict(
            observation_id="a" * 64,
            content_id="b" * 64,
            response_fingerprint="c" * 64,
            instrument_id="000001.SZ",
            observed_at="2026-09-13T00:00:00+00:00",
            availability_bound_source="receipt_recorded_at",
            normalized_status="实施",
            implementation_candidate=True,
            quality_flags="[]",
            candidate_conflict=False,
            possible_event_conflict=False,
            record_date="2020-06-01",
            ex_date="2020-06-02",
            pay_date=None,
            div_listdate="2020-06-02",
            cash_div_tax=None,
            stk_div=0.5,
            stk_bo_rate=None,
            stk_co_rate=0.5,
            cashflow_eligible=False,
            historical_pit_certified=False,
        )
        | updates
    )


def evidence(**updates):
    base = RecipientEvidence(
        "a" * 64,
        "000001.SZ",
        date(2020, 6, 1),
        date(2020, 6, 2),
        Decimal("0.5"),
        "ordinary_proportional",
        "implementation",
        "https://static.cninfo.com.cn/fixture.pdf",
        hashlib.sha256(PDF).hexdigest(),
        (2,),
    )
    return replace(base, **updates)


def test_complete_numbers_do_not_identify_recipients():
    term = terms()
    assert term.quantity_fields_complete
    result = recipient_guard(term)
    assert not result.ordinary_recipient_evidence_complete
    assert result.blockers == ("recipient_original_notice_unknown",)
    assert term.cash_before_tax is None


@pytest.mark.parametrize(
    "allocation",
    [
        "restructuring_allocation",
        "other_restricted_allocation",
        "unknown",
    ],
)
def test_positive_issuance_to_others_never_passes_ordinary_gate(allocation):
    result = recipient_guard(terms(), evidence(allocation=allocation), PDF)
    assert not result.ordinary_recipient_evidence_complete
    assert "not_verified_ordinary_proportional_recipients" in result.blockers


def test_original_recipient_pass_does_not_grant_cash_share_or_pit_authority():
    term = terms()
    result = recipient_guard(term, evidence(), PDF)
    assert result.ordinary_recipient_evidence_complete and not result.blockers
    assert not result.cashflow_eligible and not result.execution_authority
    assert not result.historical_pit_certified and term.cash_before_tax is None
    with pytest.raises(FrozenInstanceError):
        result.execution_authority = True


def test_later_notice_cannot_replace_original_event_version():
    result = recipient_guard(terms(), evidence(document_kind="supplementary"), PDF)
    assert not result.ordinary_recipient_evidence_complete
    assert "original_implementation_notice_missing" in result.blockers


@pytest.mark.parametrize(
    "key,value",
    [
        ("record_date", date(2020, 6, 3)),
        ("ex_date", None),
        ("total_stock_ratio", Decimal("1.4")),
    ],
)
def test_unmatched_terms_remain_blocked(key, value):
    result = recipient_guard(terms(), evidence(**{key: value}), PDF)
    assert f"recipient_{key}_unmatched" in result.blockers
    assert not result.ordinary_recipient_evidence_complete


@pytest.mark.parametrize(
    "updates",
    [
        {"observation_id": "z" * 64},
        {"instrument_id": "000002.SZ"},
        {"url": "https://example.com/fixture.pdf"},
        {"url": "https://static.cninfo.com.cn.evil.example/fixture.pdf"},
        {"url": "http://static.cninfo.com.cn/fixture.pdf"},
        {"url": "https://user@static.cninfo.com.cn/fixture.pdf"},
        {"pdf_sha256": "0" * 64},
        {"allocation": "assumed_all_shareholders"},
        {"document_kind": "search_snippet"},
        {"recipient_pages": ()},
        {"recipient_pages": (True,)},
        {"recipient_pages": (0,)},
        {"total_stock_ratio": Decimal("NaN")},
        {"total_stock_ratio": 0.5},
    ],
)
def test_unbound_or_unreviewed_evidence_is_rejected(updates):
    with pytest.raises(DataValidationError):
        recipient_guard(terms(), evidence(**updates), PDF)


@pytest.mark.parametrize("body", [None, b"", b"<html>blocked</html>", PDF + b"changed"])
def test_original_bytes_required(body):
    with pytest.raises(DataValidationError):
        recipient_guard(terms(), evidence(), body)


def test_ordinary_review_does_not_clear_other_component_blockers():
    result = recipient_guard(terms(div_listdate=None), evidence(), PDF)
    assert not result.ordinary_recipient_evidence_complete
    assert "positive_stock_listing_date_unknown" in result.blockers


def test_zero_total_is_not_a_positive_share_action():
    result = recipient_guard(
        terms(stk_div=0.0, stk_co_rate=None), evidence(total_stock_ratio=Decimal(0)), PDF
    )
    assert not result.ordinary_recipient_evidence_complete
    assert "no_known_positive_stock_distribution" in result.blockers
