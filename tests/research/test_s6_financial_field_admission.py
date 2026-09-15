from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from quantlab.research.s6_financial_field_admission import (
    S6FinancialConsolidationScope,
    S6FinancialFieldAdmissionVerdict,
    S6FinancialFieldDefinition,
    S6FinancialFieldEvidenceStatus,
    S6FinancialPeriodBasis,
    S6FinancialSignConvention,
    S6FinancialUnitKind,
    S6FinancialValueNature,
    audit_s6_financial_field_admission,
    build_s6_financial_field_catalog,
)
from quantlab.research.s6_financial_vintage import (
    S6FinancialStatement,
    S6FinancialVintageStatus,
    S6FiscalReportKind,
    make_s6_financial_vintage_record,
)


def _record(
    status: S6FinancialVintageStatus = (
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED
    ),
):
    return make_s6_financial_vintage_record(
        instrument_id="000001.SZ",
        fiscal_period_end=date(2024, 3, 31),
        report_kind=S6FiscalReportKind.Q1,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        provider_id="synthetic-provider",
        source_id="synthetic-source",
        revision_id="original",
        announced_at=datetime(2024, 4, 20, 8, tzinfo=UTC),
        available_at=datetime(2024, 4, 20, 9, tzinfo=UTC),
        retrieved_at=datetime(2024, 5, 1, tzinfo=UTC),
        raw_field_ids=("net_income", "revenue"),
        content_fingerprint="record-content",
        status=status,
    )


def _definition(
    raw_field_id: str,
    *,
    semantic_field_id: str | None = None,
    provider_id: str = "synthetic-provider",
    source_id: str = "synthetic-source",
    statement: S6FinancialStatement = S6FinancialStatement.INCOME_STATEMENT,
    evidence_status: S6FinancialFieldEvidenceStatus = (
        S6FinancialFieldEvidenceStatus.VERIFIED
    ),
    consolidation_scope: S6FinancialConsolidationScope = (
        S6FinancialConsolidationScope.CONSOLIDATED
    ),
    sign_convention: S6FinancialSignConvention = (
        S6FinancialSignConvention.AS_REPORTED_DOCUMENTED
    ),
):
    return S6FinancialFieldDefinition(
        provider_id=provider_id,
        source_id=source_id,
        raw_field_id=raw_field_id,
        semantic_field_id=semantic_field_id or raw_field_id,
        statement=statement,
        value_nature=S6FinancialValueNature.PERIOD_FLOW,
        period_basis=S6FinancialPeriodBasis.YEAR_TO_DATE,
        consolidation_scope=consolidation_scope,
        unit_kind=S6FinancialUnitKind.CURRENCY,
        currency="CNY",
        sign_convention=sign_convention,
        evidence_status=evidence_status,
        documentation_fingerprint=(
            "documentation"
            if evidence_status is not S6FinancialFieldEvidenceStatus.UNKNOWN
            else None
        ),
    )


def test_catalog_is_deterministic_and_retains_all_evidence_levels() -> None:
    definitions = (
        _definition("revenue"),
        _definition(
            "net_income",
            evidence_status=(
                S6FinancialFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
            ),
        ),
        _definition("unknown_field", evidence_status=S6FinancialFieldEvidenceStatus.UNKNOWN),
    )

    first = build_s6_financial_field_catalog(definitions)
    second = build_s6_financial_field_catalog(reversed(definitions))

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.verified_count == 1
    assert first.unverified_count == 1
    assert first.unknown_count == 1
    assert first.numeric_values_included is False
    assert first.factor_formulas_included is False
    assert first.performance_claim is False
    assert first.broker_order_authority is False


def test_verified_complete_fields_admit_a_verified_vintage() -> None:
    catalog = build_s6_financial_field_catalog(
        (_definition("revenue"), _definition("net_income"))
    )

    audit = audit_s6_financial_field_admission(record=_record(), catalog=catalog)

    assert [row.raw_field_id for row in audit.rows] == ["net_income", "revenue"]
    assert all(
        row.verdict is S6FinancialFieldAdmissionVerdict.ADMISSIBLE
        for row in audit.rows
    )
    assert audit.vintage_verified is True
    assert audit.all_fields_admissible is True
    assert audit.overall_admissible is True
    assert audit.numeric_values_included is False
    assert audit.factor_computation_authority is False
    assert audit.performance_claim is False
    assert audit.broker_order_authority is False
    assert audit.fingerprint


def test_every_missing_or_unverified_raw_field_remains_visible() -> None:
    catalog = build_s6_financial_field_catalog(
        (
            _definition(
                "net_income",
                evidence_status=(
                    S6FinancialFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
                ),
            ),
        )
    )

    audit = audit_s6_financial_field_admission(record=_record(), catalog=catalog)
    rows = {row.raw_field_id: row for row in audit.rows}

    assert rows["net_income"].verdict is (
        S6FinancialFieldAdmissionVerdict.UNVERIFIED_DEFINITION
    )
    assert rows["revenue"].verdict is S6FinancialFieldAdmissionVerdict.MISSING_DEFINITION
    assert audit.all_fields_admissible is False
    assert audit.overall_admissible is False


def test_wrong_provider_or_statement_is_an_identity_mismatch() -> None:
    wrong_provider = build_s6_financial_field_catalog(
        (
            _definition("net_income"),
            _definition("revenue", provider_id="another-provider"),
        )
    )
    first = audit_s6_financial_field_admission(
        record=_record(),
        catalog=wrong_provider,
    )
    assert first.rows[1].verdict is S6FinancialFieldAdmissionVerdict.IDENTITY_MISMATCH

    wrong_statement = build_s6_financial_field_catalog(
        (
            _definition("net_income"),
            _definition("revenue", statement=S6FinancialStatement.BALANCE_SHEET),
        )
    )
    second = audit_s6_financial_field_admission(
        record=_record(),
        catalog=wrong_statement,
    )
    assert second.rows[1].verdict is S6FinancialFieldAdmissionVerdict.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("scope", "sign"),
    [
        (
            S6FinancialConsolidationScope.UNKNOWN,
            S6FinancialSignConvention.AS_REPORTED_DOCUMENTED,
        ),
        (
            S6FinancialConsolidationScope.CONSOLIDATED,
            S6FinancialSignConvention.UNKNOWN,
        ),
    ],
)
def test_unknown_scope_or_sign_never_becomes_admissible(
    scope: S6FinancialConsolidationScope,
    sign: S6FinancialSignConvention,
) -> None:
    definition = _definition(
        "revenue",
        consolidation_scope=scope,
        sign_convention=sign,
    )
    assert definition.semantically_admissible is False


def test_field_semantics_cannot_rescue_an_unverified_vintage() -> None:
    catalog = build_s6_financial_field_catalog(
        (_definition("revenue"), _definition("net_income"))
    )

    audit = audit_s6_financial_field_admission(
        record=_record(S6FinancialVintageStatus.LATEST_ONLY_UNVERIFIED),
        catalog=catalog,
    )

    assert audit.all_fields_admissible is True
    assert audit.vintage_verified is False
    assert audit.overall_admissible is False


def test_stock_flow_period_and_unit_currency_rules_fail_closed() -> None:
    flow = _definition("revenue")
    with pytest.raises(ValueError, match="period-flow"):
        replace(flow, period_basis=S6FinancialPeriodBasis.INSTANT)

    with pytest.raises(ValueError, match="point-in-time"):
        replace(
            flow,
            value_nature=S6FinancialValueNature.POINT_IN_TIME,
            period_basis=S6FinancialPeriodBasis.YEAR_TO_DATE,
        )

    with pytest.raises(ValueError, match="uppercase ISO-like"):
        replace(flow, currency="cny")

    with pytest.raises(ValueError, match="cannot declare currency"):
        replace(flow, unit_kind=S6FinancialUnitKind.RATIO)


def test_verified_definition_requires_documentation_evidence() -> None:
    with pytest.raises(ValueError, match="require documentation evidence"):
        replace(_definition("revenue"), documentation_fingerprint=None)


def test_direct_catalog_construction_cannot_bypass_identity_or_counts() -> None:
    first = _definition("net_income")
    second = _definition("revenue")
    catalog = build_s6_financial_field_catalog((first, second))

    with pytest.raises(ValueError, match="deterministic identity order"):
        replace(catalog, definitions=tuple(reversed(catalog.definitions)))
    with pytest.raises(ValueError, match="duplicate financial field identity"):
        replace(catalog, definitions=(first, first), verified_count=2)
    with pytest.raises(ValueError, match="counts do not match"):
        replace(catalog, verified_count=0, unknown_count=2)
