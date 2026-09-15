from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from quantlab.research.s6_financial_field_admission import (
    S6FinancialConsolidationScope,
    S6FinancialFieldDefinition,
    S6FinancialFieldEvidenceStatus,
    S6FinancialPeriodBasis,
    S6FinancialSignConvention,
    S6FinancialUnitKind,
    S6FinancialValueNature,
    build_s6_financial_field_catalog,
)
from quantlab.research.s6_financial_vintage import (
    S6FinancialSelectionVerdict,
    S6FinancialStatement,
    S6FinancialVintageStatus,
    S6FiscalReportKind,
    make_s6_financial_vintage_record,
)
from quantlab.research.s6_formula_financial_readiness import (
    S6FormulaFinancialReadinessVerdict,
    audit_s6_formula_financial_readiness,
)
from quantlab.research.s6_formula_input_binding import (
    S6FormulaBindingContext,
    audit_s6_formula_input_bindings,
)
from quantlab.research.s6_formula_spec import (
    S6DenominatorPolicy,
    S6FormulaEvidenceStatus,
    S6FormulaInputKind,
    S6FormulaInputRef,
    S6FormulaOperator,
    S6FormulaSpec,
    S6MetricDirection,
    S6MetricFamily,
    S6MissingInputPolicy,
    S6PeriodAlignmentPolicy,
    build_s6_formula_spec_catalog,
)

_CONTEXT = S6FormulaBindingContext("synthetic-provider", "synthetic-source")
_PERIOD_END = date(2024, 3, 31)
_ANNOUNCED = datetime(2024, 4, 20, 8, tzinfo=UTC)
_AVAILABLE = datetime(2024, 4, 20, 9, tzinfo=UTC)
_AS_OF = datetime(2024, 4, 24, tzinfo=UTC)


def _definition(raw_field_id: str = "net_income") -> S6FinancialFieldDefinition:
    return S6FinancialFieldDefinition(
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        raw_field_id=raw_field_id,
        semantic_field_id="net_income_attributable",
        statement=S6FinancialStatement.INCOME_STATEMENT,
        value_nature=S6FinancialValueNature.PERIOD_FLOW,
        period_basis=S6FinancialPeriodBasis.YEAR_TO_DATE,
        consolidation_scope=S6FinancialConsolidationScope.CONSOLIDATED,
        unit_kind=S6FinancialUnitKind.CURRENCY,
        currency="CNY",
        sign_convention=S6FinancialSignConvention.AS_REPORTED_DOCUMENTED,
        evidence_status=S6FinancialFieldEvidenceStatus.VERIFIED,
        documentation_fingerprint="field-documentation",
    )


def _spec(kind: S6FormulaInputKind = S6FormulaInputKind.FINANCIAL_FIELD):
    return S6FormulaSpec(
        metric_id="earnings_identity",
        family=S6MetricFamily.PROFITABILITY,
        operator=S6FormulaOperator.IDENTITY,
        inputs=(
            S6FormulaInputRef(
                position=0,
                input_id="earnings",
                kind=kind,
                semantic_id="net_income_attributable",
            ),
        ),
        direction=S6MetricDirection.HIGHER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.SAME_FISCAL_PERIOD,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        negative_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        evidence_status=S6FormulaEvidenceStatus.VERIFIED,
        documentation_fingerprint="formula-documentation",
        rationale_fingerprint="formula-rationale",
    )


def _binding(*, field_catalog=None, spec=None):
    bound_spec = spec or _spec()
    catalog = field_catalog or build_s6_financial_field_catalog((_definition(),))
    return (
        audit_s6_formula_input_bindings(
            spec=bound_spec,
            formula_catalog=build_s6_formula_spec_catalog((bound_spec,)),
            field_catalog=catalog,
            context=_CONTEXT,
        ),
        catalog,
    )


def _record(
    *,
    revision_id: str = "original",
    available_at: datetime = _AVAILABLE,
    status: S6FinancialVintageStatus = (
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED
    ),
    raw_field_ids=("net_income",),
    provider_id: str = _CONTEXT.provider_id,
    content_fingerprint: str = "content-original",
):
    return make_s6_financial_vintage_record(
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        report_kind=S6FiscalReportKind.Q1,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        provider_id=provider_id,
        source_id=_CONTEXT.source_id,
        revision_id=revision_id,
        announced_at=_ANNOUNCED,
        available_at=available_at,
        retrieved_at=max(available_at, datetime(2024, 5, 1, tzinfo=UTC)),
        raw_field_ids=raw_field_ids,
        content_fingerprint=content_fingerprint,
        status=status,
    )


def _audit(records, *, binding=None, catalog=None, as_of=_AS_OF):
    if binding is None or catalog is None:
        binding, catalog = _binding()
    return audit_s6_formula_financial_readiness(
        binding_audit=binding,
        field_catalog=catalog,
        vintage_records=records,
        instrument_id="000001.SZ",
        fiscal_period_end=_PERIOD_END,
        as_of=as_of,
    )


def test_visible_verified_field_passes_only_the_financial_gate() -> None:
    record = _record()
    audit = _audit((record,))

    assert audit.financial_input_count == 1
    assert audit.ready_count == 1
    assert audit.rows[0].readiness_verdict is (
        S6FormulaFinancialReadinessVerdict.READY
    )
    assert audit.rows[0].selection_verdict is S6FinancialSelectionVerdict.ADMISSIBLE
    assert audit.rows[0].selected_record_fingerprint == record.fingerprint
    assert audit.rows[0].field_admission_fingerprint
    assert audit.all_direct_financial_inputs_ready is True
    assert audit.financial_gate_ready is True
    assert audit.formula_computation_ready is False
    assert audit.numeric_values_included is False
    assert audit.performance_claim is False
    assert audit.portfolio_or_risk_authority is False
    assert audit.broker_order_authority is False
    assert audit.fingerprint


def test_revision_replaces_original_only_after_its_source_availability() -> None:
    original = _record()
    revision_time = datetime(2024, 4, 25, tzinfo=UTC)
    revision = _record(
        revision_id="revision-1",
        available_at=revision_time,
        status=S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED,
        content_fingerprint="content-revision",
    )

    before = _audit((revision, original))
    after = _audit((original, revision), as_of=revision_time)

    assert before.rows[0].selected_record_fingerprint == original.fingerprint
    assert after.rows[0].selected_record_fingerprint == revision.fingerprint
    assert before.vintage_inventory_fingerprint == after.vintage_inventory_fingerprint


@pytest.mark.parametrize(
    ("records", "as_of", "expected"),
    [
        ((), _AS_OF, S6FormulaFinancialReadinessVerdict.MISSING_VINTAGE),
        (
            (_record(status=S6FinancialVintageStatus.LATEST_ONLY_UNVERIFIED),),
            _AS_OF,
            S6FormulaFinancialReadinessVerdict.UNVERIFIED_VINTAGE,
        ),
        (
            (_record(),),
            datetime(2024, 4, 19, tzinfo=UTC),
            S6FormulaFinancialReadinessVerdict.NOT_YET_AVAILABLE,
        ),
    ],
)
def test_missing_unverified_and_future_vintages_remain_distinct(
    records, as_of, expected
) -> None:
    audit = _audit(records, as_of=as_of)

    assert audit.rows[0].readiness_verdict is expected
    assert audit.rows[0].selected_record_fingerprint is None
    assert audit.financial_gate_ready is False


def test_selected_vintage_must_contain_the_bound_raw_field() -> None:
    audit = _audit((_record(raw_field_ids=("revenue",)),))

    assert audit.rows[0].readiness_verdict is (
        S6FormulaFinancialReadinessVerdict.FIELD_MISSING_FROM_VINTAGE
    )
    assert audit.rows[0].selected_record_fingerprint
    assert audit.rows[0].field_admission_fingerprint is None
    assert audit.financial_gate_ready is False


def test_provider_scope_prevents_cross_source_substitution() -> None:
    audit = _audit((_record(provider_id="other-provider"),))

    assert audit.rows[0].readiness_verdict is (
        S6FormulaFinancialReadinessVerdict.MISSING_VINTAGE
    )
    assert audit.financial_gate_ready is False


def test_blocked_binding_is_preserved_without_selecting_a_vintage() -> None:
    empty_catalog = build_s6_financial_field_catalog(())
    binding, catalog = _binding(field_catalog=empty_catalog)
    audit = _audit((_record(),), binding=binding, catalog=catalog)

    assert audit.rows[0].readiness_verdict is (
        S6FormulaFinancialReadinessVerdict.BINDING_BLOCKED
    )
    assert audit.rows[0].selection_verdict is None
    assert audit.financial_gate_ready is False


def test_field_catalog_must_match_binding_exactly() -> None:
    binding, _ = _binding()
    changed = build_s6_financial_field_catalog((_definition("provider_net_income"),))

    with pytest.raises(ValueError, match="exact field catalog"):
        _audit((_record(),), binding=binding, catalog=changed)


def test_no_direct_financial_inputs_never_claims_readiness() -> None:
    derived = _spec(S6FormulaInputKind.DERIVED_METRIC)
    binding, catalog = _binding(spec=derived)
    audit = _audit((), binding=binding, catalog=catalog)

    assert audit.rows == ()
    assert audit.all_direct_financial_inputs_ready is False
    assert audit.financial_gate_ready is False


def test_summary_authority_and_naive_time_fail_closed() -> None:
    audit = _audit((_record(),))

    with pytest.raises(ValueError, match="financial_input_count"):
        replace(audit, financial_input_count=0)
    with pytest.raises(ValueError, match="ready_count"):
        replace(audit, ready_count=0)
    with pytest.raises(ValueError, match="all_direct_financial_inputs_ready"):
        replace(audit, all_direct_financial_inputs_ready=False)
    with pytest.raises(ValueError, match="financial_gate_ready"):
        replace(audit, binding_overall_admissible=False)
    with pytest.raises(ValueError, match="cannot acquire"):
        replace(audit, formula_computation_ready=True)
    with pytest.raises(ValueError, match="timezone-aware"):
        _audit((_record(),), as_of=datetime(2024, 4, 24))
