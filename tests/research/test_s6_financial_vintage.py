from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from quantlab.research.s6_financial_vintage import (
    S6FinancialSelectionVerdict,
    S6FinancialStatement,
    S6FinancialVintageStatus,
    S6FiscalReportKind,
    build_s6_financial_vintage_inventory,
    make_s6_financial_vintage_record,
    select_s6_financial_vintage,
)

_PERIOD_END = date(2024, 3, 31)
_ANNOUNCED = datetime(2024, 4, 20, 8, tzinfo=UTC)
_AVAILABLE = datetime(2024, 4, 20, 9, tzinfo=UTC)
_RETRIEVED = datetime(2024, 5, 1, tzinfo=UTC)


def _record(
    *,
    revision_id: str = "original",
    available_at: datetime = _AVAILABLE,
    retrieved_at: datetime = _RETRIEVED,
    status: S6FinancialVintageStatus = (
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED
    ),
    content_fingerprint: str = "content-original",
):
    return make_s6_financial_vintage_record(
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        report_kind=S6FiscalReportKind.Q1,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        provider_id="synthetic-provider",
        source_id="synthetic-source",
        revision_id=revision_id,
        announced_at=_ANNOUNCED,
        available_at=available_at,
        retrieved_at=retrieved_at,
        raw_field_ids=("revenue", "net_income"),
        content_fingerprint=content_fingerprint,
        status=status,
    )


def test_record_normalizes_aware_times_fields_and_identity() -> None:
    china = timezone(timedelta(hours=8))
    record = make_s6_financial_vintage_record(
        instrument_id=" 000001.SZ ",
        fiscal_period_end=_PERIOD_END,
        report_kind=S6FiscalReportKind.Q1,
        statement=S6FinancialStatement.BALANCE_SHEET,
        provider_id=" synthetic-provider ",
        source_id=" synthetic-source ",
        revision_id=" original ",
        announced_at=datetime(2024, 4, 20, 16, tzinfo=china),
        available_at=datetime(2024, 4, 20, 17, tzinfo=china),
        retrieved_at=datetime(2024, 5, 1, 8, tzinfo=china),
        raw_field_ids=(" total_assets ", "cash"),
        content_fingerprint=" fingerprint ",
        status=S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
    )

    assert record.instrument_id == "000001.SZ"
    assert record.announced_at == _ANNOUNCED
    assert record.available_at == _AVAILABLE
    assert record.retrieved_at == _RETRIEVED
    assert record.raw_field_ids == ("cash", "total_assets")
    assert record.content_fingerprint == "fingerprint"
    assert record.fingerprint


def test_inventory_retains_every_evidence_status_and_is_order_invariant() -> None:
    records = (
        _record(),
        _record(
            revision_id="revision-1",
            available_at=datetime(2024, 4, 25, tzinfo=UTC),
            status=S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED,
            content_fingerprint="content-revision",
        ),
        _record(
            revision_id="latest-only",
            available_at=datetime(2024, 4, 26, tzinfo=UTC),
            status=S6FinancialVintageStatus.LATEST_ONLY_UNVERIFIED,
            content_fingerprint="content-latest",
        ),
        _record(
            revision_id="unknown",
            available_at=datetime(2024, 4, 27, tzinfo=UTC),
            status=S6FinancialVintageStatus.UNKNOWN,
            content_fingerprint="content-unknown",
        ),
    )

    first = build_s6_financial_vintage_inventory(records)
    second = build_s6_financial_vintage_inventory(reversed(records))

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert {
        row.status: row.count for row in first.status_counts
    } == {
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED: 1,
        S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED: 1,
        S6FinancialVintageStatus.LATEST_ONLY_UNVERIFIED: 1,
        S6FinancialVintageStatus.UNKNOWN: 1,
    }
    assert first.source_time_pit_only is True
    assert first.historical_local_knowledge_proven is False
    assert first.numeric_values_included is False
    assert first.factor_scores_included is False
    assert first.performance_claim is False
    assert first.broker_order_authority is False


def test_selection_changes_only_when_verified_revision_becomes_available() -> None:
    original = _record()
    revision = _record(
        revision_id="revision-1",
        available_at=datetime(2024, 4, 25, tzinfo=UTC),
        status=S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED,
        content_fingerprint="content-revision",
    )

    before_revision = select_s6_financial_vintage(
        (revision, original),
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        as_of=datetime(2024, 4, 24, tzinfo=UTC),
    )
    after_revision = select_s6_financial_vintage(
        (original, revision),
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        as_of=datetime(2024, 4, 25, tzinfo=UTC),
    )

    assert before_revision.verdict is S6FinancialSelectionVerdict.ADMISSIBLE
    assert before_revision.selected is original
    assert after_revision.verdict is S6FinancialSelectionVerdict.ADMISSIBLE
    assert after_revision.selected is revision
    assert after_revision.historical_local_knowledge_proven is False
    assert after_revision.numeric_value_authority is False
    assert after_revision.performance_claim is False
    assert after_revision.broker_order_authority is False


@pytest.mark.parametrize(
    "status",
    [
        S6FinancialVintageStatus.LATEST_ONLY_UNVERIFIED,
        S6FinancialVintageStatus.UNKNOWN,
    ],
)
def test_weak_or_unknown_visible_vintage_is_never_selected(
    status: S6FinancialVintageStatus,
) -> None:
    result = select_s6_financial_vintage(
        (_record(status=status),),
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        as_of=datetime(2024, 4, 21, tzinfo=UTC),
    )

    assert result.verdict is S6FinancialSelectionVerdict.UNVERIFIED_VINTAGE
    assert result.selected is None


def test_future_verified_vintage_fails_closed_as_not_yet_available() -> None:
    result = select_s6_financial_vintage(
        (_record(),),
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        as_of=datetime(2024, 4, 19, tzinfo=UTC),
    )

    assert result.verdict is S6FinancialSelectionVerdict.NOT_YET_AVAILABLE
    assert result.selected is None


def test_missing_target_is_explicit() -> None:
    result = select_s6_financial_vintage(
        (_record(),),
        instrument_id="600000.SH",
        fiscal_period_end=_PERIOD_END,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        as_of=datetime(2024, 4, 21, tzinfo=UTC),
    )

    assert result.verdict is S6FinancialSelectionVerdict.MISSING
    assert result.selected is None


def test_duplicate_identity_and_same_time_conflict_are_rejected() -> None:
    original = _record()
    with pytest.raises(ValueError, match="duplicate financial vintage identity"):
        build_s6_financial_vintage_inventory((original, original))

    conflict = _record(
        revision_id="conflict",
        content_fingerprint="different-content",
    )
    with pytest.raises(ValueError, match="share one availability time"):
        build_s6_financial_vintage_inventory((original, conflict))


def test_period_alignment_and_timestamp_backdating_fail_closed() -> None:
    with pytest.raises(ValueError, match="does not align"):
        make_s6_financial_vintage_record(
            instrument_id="000001.SZ",
            fiscal_period_end=date(2024, 4, 1),
            report_kind=S6FiscalReportKind.Q1,
            statement=S6FinancialStatement.INCOME_STATEMENT,
            provider_id="provider",
            source_id="source",
            revision_id="original",
            announced_at=_ANNOUNCED,
            available_at=_AVAILABLE,
            retrieved_at=_RETRIEVED,
            raw_field_ids=("revenue",),
            content_fingerprint="content",
            status=S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
        )

    with pytest.raises(ValueError, match="available_at cannot precede announced_at"):
        _record(available_at=datetime(2024, 4, 20, 7, tzinfo=UTC))
    with pytest.raises(ValueError, match="retrieved_at cannot precede available_at"):
        _record(retrieved_at=datetime(2024, 4, 20, 8, 30, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        select_s6_financial_vintage(
            (_record(),),
            instrument_id="000001.SZ",
            fiscal_period_end=_PERIOD_END,
            statement=S6FinancialStatement.INCOME_STATEMENT,
            as_of=datetime(2024, 4, 21),
        )


def test_field_ids_must_be_nonempty_and_unique_after_normalization() -> None:
    with pytest.raises(ValueError, match="unique"):
        make_s6_financial_vintage_record(
            instrument_id="000001.SZ",
            fiscal_period_end=_PERIOD_END,
            report_kind=S6FiscalReportKind.Q1,
            statement=S6FinancialStatement.CASH_FLOW_STATEMENT,
            provider_id="provider",
            source_id="source",
            revision_id="original",
            announced_at=_ANNOUNCED,
            available_at=_AVAILABLE,
            retrieved_at=_RETRIEVED,
            raw_field_ids=("cash_flow", " cash_flow "),
            content_fingerprint="content",
            status=S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
        )

    with pytest.raises(ValueError, match="non-empty identifiers"):
        replace(_record(), raw_field_ids=("",))
