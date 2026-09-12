"""Reviewed recipient evidence for research; never a shareholder posting authority.

PDF hashes authenticate the reviewed bytes, not the reviewer's interpretation.
The caller must separately review the issuer's actual allocation provisions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from quantlab.data.models import DataValidationError
from quantlab.research.corporate_terms import MinimumCorporateTerms

ALLOCATIONS = {
    "ordinary_proportional",
    "restructuring_allocation",
    "other_restricted_allocation",
    "unknown",
}
HOSTS = {"static.cninfo.com.cn", "disc.static.szse.cn", "static.sse.com.cn"}


@dataclass(frozen=True)
class RecipientEvidence:
    observation_id: str
    instrument_id: str
    record_date: date | None
    ex_date: date | None
    total_stock_ratio: Decimal | None
    allocation: str
    document_kind: str
    url: str
    pdf_sha256: str
    recipient_pages: tuple[int, ...]


@dataclass(frozen=True)
class RecipientGuard:
    observation_id: str
    allocation: str
    ordinary_recipient_evidence_complete: bool
    blockers: tuple[str, ...]
    cashflow_eligible: bool = field(default=False, init=False)
    historical_pit_certified: bool = field(default=False, init=False)
    execution_authority: bool = field(default=False, init=False)


def recipient_guard(
    term: MinimumCorporateTerms,
    review: RecipientEvidence | None = None,
    original_pdf: bytes | None = None,
) -> RecipientGuard:
    """Keep numerical completeness distinct from ordinary recipient evidence.

    Even a passed recipient review needs event uniqueness, entitled holdings, tax,
    fractional allocation, actual availability and sellability before any posting.
    Supplementary issuer notices may expose a blocker but cannot clear this gate.
    """
    blockers = set(term.common_blockers + term.quantity_blockers)
    allocation = "unknown"
    if review is None:
        blockers.add("recipient_original_notice_unknown")
    else:
        if (
            review.observation_id != term.observation_id
            or review.instrument_id != term.instrument_id
        ):
            raise DataValidationError("recipient evidence belongs to a different observation")
        parsed = urlparse(review.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or not isinstance(original_pdf, bytes)
            or not original_pdf.startswith(b"%PDF-")
            or hashlib.sha256(original_pdf).hexdigest() != review.pdf_sha256
        ):
            raise DataValidationError("recipient evidence requires bound original PDF bytes")
        if review.allocation not in ALLOCATIONS or review.document_kind not in {
            "implementation",
            "supplementary",
        }:
            raise DataValidationError("unreviewed recipient classification")
        if (
            not isinstance(review.recipient_pages, tuple)
            or not review.recipient_pages
            or any(type(p) is not int or p < 1 for p in review.recipient_pages)
        ):
            raise DataValidationError("recipient provisions need one-based page references")
        if review.total_stock_ratio is not None and (
            not isinstance(review.total_stock_ratio, Decimal)
            or not review.total_stock_ratio.is_finite()
            or review.total_stock_ratio < 0
        ):
            raise DataValidationError("invalid reviewed stock ratio")
        allocation = review.allocation
        for key in ("record_date", "ex_date", "total_stock_ratio"):
            value = getattr(review, key)
            if value is None or value != getattr(term, key):
                blockers.add(f"recipient_{key}_unmatched")
        if review.document_kind != "implementation":
            blockers.add("original_implementation_notice_missing")
        if allocation != "ordinary_proportional":
            blockers.add("not_verified_ordinary_proportional_recipients")
    if term.total_stock_ratio is None or term.total_stock_ratio <= 0:
        blockers.add("no_known_positive_stock_distribution")
    return RecipientGuard(term.observation_id, allocation, not blockers, tuple(sorted(blockers)))
