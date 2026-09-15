from __future__ import annotations

from dataclasses import replace

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
from quantlab.research.s6_financial_vintage import S6FinancialStatement
from quantlab.research.s6_formula_input_binding import (
    S6FormulaBindingContext,
    S6FormulaInputBindingVerdict,
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


def _field(
    raw_field_id: str,
    semantic_field_id: str,
    *,
    evidence: S6FinancialFieldEvidenceStatus = (
        S6FinancialFieldEvidenceStatus.VERIFIED
    ),
) -> S6FinancialFieldDefinition:
    return S6FinancialFieldDefinition(
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        raw_field_id=raw_field_id,
        semantic_field_id=semantic_field_id,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        value_nature=S6FinancialValueNature.PERIOD_FLOW,
        period_basis=S6FinancialPeriodBasis.YEAR_TO_DATE,
        consolidation_scope=S6FinancialConsolidationScope.CONSOLIDATED,
        unit_kind=S6FinancialUnitKind.CURRENCY,
        currency="CNY",
        sign_convention=S6FinancialSignConvention.AS_REPORTED_DOCUMENTED,
        evidence_status=evidence,
        documentation_fingerprint=(
            "field-documentation"
            if evidence is not S6FinancialFieldEvidenceStatus.UNKNOWN
            else None
        ),
    )


def _input(
    position: int,
    input_id: str,
    semantic_id: str,
    kind: S6FormulaInputKind = S6FormulaInputKind.FINANCIAL_FIELD,
) -> S6FormulaInputRef:
    return S6FormulaInputRef(position, input_id, kind, semantic_id)


def _ratio(
    metric_id: str = "synthetic_ratio",
    *,
    inputs: tuple[S6FormulaInputRef, ...] | None = None,
    evidence: S6FormulaEvidenceStatus = S6FormulaEvidenceStatus.VERIFIED,
) -> S6FormulaSpec:
    return S6FormulaSpec(
        metric_id=metric_id,
        family=S6MetricFamily.QUALITY,
        operator=S6FormulaOperator.RATIO,
        inputs=inputs
        or (
            _input(0, "numerator", "semantic_numerator"),
            _input(1, "denominator", "semantic_denominator"),
        ),
        direction=S6MetricDirection.HIGHER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.SAME_FISCAL_PERIOD,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.EXCLUDE_OBSERVATION,
        negative_denominator_policy=S6DenominatorPolicy.ALLOW_SIGNED,
        evidence_status=evidence,
        documentation_fingerprint="formula-documentation",
        rationale_fingerprint="formula-rationale",
    )


def _identity(
    metric_id: str,
    input_ref: S6FormulaInputRef,
    *,
    evidence: S6FormulaEvidenceStatus = S6FormulaEvidenceStatus.VERIFIED,
) -> S6FormulaSpec:
    return S6FormulaSpec(
        metric_id=metric_id,
        family=S6MetricFamily.QUALITY,
        operator=S6FormulaOperator.IDENTITY,
        inputs=(input_ref,),
        direction=S6MetricDirection.HIGHER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.SAME_FISCAL_PERIOD,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        negative_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        evidence_status=evidence,
        documentation_fingerprint="formula-documentation",
        rationale_fingerprint="formula-rationale",
    )


def test_exact_financial_semantics_bind_every_input() -> None:
    spec = _ratio()
    formulas = build_s6_formula_spec_catalog((spec,))
    fields = build_s6_financial_field_catalog(
        (
            _field("provider_num", "semantic_numerator"),
            _field("provider_den", "semantic_denominator"),
        )
    )

    audit = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=formulas,
        field_catalog=fields,
        context=_CONTEXT,
    )

    assert [row.position for row in audit.rows] == [0, 1]
    assert all(
        row.verdict is S6FormulaInputBindingVerdict.ADMISSIBLE
        for row in audit.rows
    )
    assert audit.formula_admitted is True
    assert audit.all_inputs_admitted is True
    assert audit.overall_admissible is True
    assert audit.metadata_only is True
    assert audit.numeric_values_included is False
    assert audit.metric_computation_authority is False
    assert audit.performance_claim is False
    assert audit.broker_order_authority is False
    assert audit.fingerprint


def test_missing_unverified_and_ambiguous_financial_fields_are_distinct() -> None:
    spec = _ratio()
    formulas = build_s6_formula_spec_catalog((spec,))

    missing = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=formulas,
        field_catalog=build_s6_financial_field_catalog(()),
        context=_CONTEXT,
    )
    assert all(
        row.verdict is S6FormulaInputBindingVerdict.FINANCIAL_FIELD_MISSING
        for row in missing.rows
    )

    unverified = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=formulas,
        field_catalog=build_s6_financial_field_catalog(
            (
                _field(
                    "num",
                    "semantic_numerator",
                    evidence=S6FinancialFieldEvidenceStatus.UNKNOWN,
                ),
                _field(
                    "den",
                    "semantic_denominator",
                    evidence=S6FinancialFieldEvidenceStatus.UNKNOWN,
                ),
            )
        ),
        context=_CONTEXT,
    )
    assert all(
        row.verdict is S6FormulaInputBindingVerdict.FINANCIAL_FIELD_UNVERIFIED
        for row in unverified.rows
    )

    ambiguous = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=formulas,
        field_catalog=build_s6_financial_field_catalog(
            (
                _field("num_a", "semantic_numerator"),
                _field("num_b", "semantic_numerator"),
                _field("den", "semantic_denominator"),
            )
        ),
        context=_CONTEXT,
    )
    assert ambiguous.rows[0].verdict is (
        S6FormulaInputBindingVerdict.FINANCIAL_FIELD_AMBIGUOUS
    )
    assert ambiguous.overall_admissible is False


def test_market_input_is_explicitly_blocked_until_its_contract_exists() -> None:
    spec = _identity(
        "market_identity",
        _input(
            0,
            "market_value",
            "market_cap",
            S6FormulaInputKind.MARKET_FIELD,
        ),
    )

    audit = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=build_s6_formula_spec_catalog((spec,)),
        field_catalog=build_s6_financial_field_catalog(()),
        context=_CONTEXT,
    )

    assert audit.rows[0].verdict is (
        S6FormulaInputBindingVerdict.MARKET_INPUT_CONTRACT_MISSING
    )
    assert audit.overall_admissible is False


def test_derived_metric_must_exist_and_be_admitted() -> None:
    missing = _identity(
        "consumer",
        _input(
            0,
            "derived",
            "absent_metric",
            S6FormulaInputKind.DERIVED_METRIC,
        ),
    )
    missing_audit = audit_s6_formula_input_bindings(
        spec=missing,
        formula_catalog=build_s6_formula_spec_catalog((missing,)),
        field_catalog=build_s6_financial_field_catalog(()),
        context=_CONTEXT,
    )
    assert missing_audit.rows[0].verdict is (
        S6FormulaInputBindingVerdict.DERIVED_METRIC_MISSING
    )

    blocked_dependency = _identity(
        "blocked_dependency",
        _input(0, "raw", "raw_semantic"),
        evidence=S6FormulaEvidenceStatus.DRAFT_UNVERIFIED,
    )
    blocked_consumer = _identity(
        "blocked_consumer",
        _input(
            0,
            "derived",
            "blocked_dependency",
            S6FormulaInputKind.DERIVED_METRIC,
        ),
    )
    blocked_catalog = build_s6_formula_spec_catalog(
        (blocked_dependency, blocked_consumer)
    )
    blocked_audit = audit_s6_formula_input_bindings(
        spec=blocked_consumer,
        formula_catalog=blocked_catalog,
        field_catalog=build_s6_financial_field_catalog(()),
        context=_CONTEXT,
    )
    assert blocked_audit.rows[0].verdict is (
        S6FormulaInputBindingVerdict.DERIVED_METRIC_BLOCKED
    )

    admitted_dependency = _identity(
        "admitted_dependency",
        _input(0, "raw", "raw_semantic"),
    )
    admitted_consumer = _identity(
        "admitted_consumer",
        _input(
            0,
            "derived",
            "admitted_dependency",
            S6FormulaInputKind.DERIVED_METRIC,
        ),
    )
    admitted_catalog = build_s6_formula_spec_catalog(
        (admitted_dependency, admitted_consumer)
    )
    admitted_audit = audit_s6_formula_input_bindings(
        spec=admitted_consumer,
        formula_catalog=admitted_catalog,
        field_catalog=build_s6_financial_field_catalog(()),
        context=_CONTEXT,
    )
    assert admitted_audit.rows[0].verdict is (
        S6FormulaInputBindingVerdict.ADMISSIBLE
    )
    assert admitted_audit.overall_admissible is True


def test_admitted_inputs_cannot_rescue_an_unverified_formula() -> None:
    spec = _ratio(evidence=S6FormulaEvidenceStatus.DRAFT_UNVERIFIED)
    fields = build_s6_financial_field_catalog(
        (
            _field("num", "semantic_numerator"),
            _field("den", "semantic_denominator"),
        )
    )

    audit = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=build_s6_formula_spec_catalog((spec,)),
        field_catalog=fields,
        context=_CONTEXT,
    )

    assert audit.all_inputs_admitted is True
    assert audit.formula_admitted is False
    assert audit.overall_admissible is False


def test_spec_must_match_the_exact_catalog_fingerprint() -> None:
    spec = _ratio()
    changed = replace(spec, rationale_fingerprint="changed")

    with pytest.raises(ValueError, match="not exactly bound"):
        audit_s6_formula_input_bindings(
            spec=changed,
            formula_catalog=build_s6_formula_spec_catalog((spec,)),
            field_catalog=build_s6_financial_field_catalog(()),
            context=_CONTEXT,
        )


def test_direct_audit_construction_cannot_bypass_order_or_summary() -> None:
    spec = _ratio()
    fields = build_s6_financial_field_catalog(
        (
            _field("num", "semantic_numerator"),
            _field("den", "semantic_denominator"),
        )
    )
    audit = audit_s6_formula_input_bindings(
        spec=spec,
        formula_catalog=build_s6_formula_spec_catalog((spec,)),
        field_catalog=fields,
        context=_CONTEXT,
    )

    with pytest.raises(ValueError, match="contiguous formula-input order"):
        replace(audit, rows=tuple(reversed(audit.rows)))
    with pytest.raises(ValueError, match="all_inputs_admitted"):
        replace(audit, all_inputs_admitted=False)
    with pytest.raises(ValueError, match="overall_admissible"):
        replace(audit, overall_admissible=False)
