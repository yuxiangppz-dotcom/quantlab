from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

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
    S6FinancialStatement,
    S6FinancialVintageStatus,
    S6FiscalReportKind,
    make_s6_financial_vintage_record,
)
from quantlab.research.s6_formula_financial_readiness import (
    audit_s6_formula_financial_readiness,
)
from quantlab.research.s6_formula_input_binding import (
    S6FormulaBindingContext,
    audit_s6_formula_input_bindings,
)
from quantlab.research.s6_formula_market_readiness import (
    audit_s6_formula_market_readiness,
)
from quantlab.research.s6_formula_readiness_preflight import (
    build_s6_formula_readiness_preflight,
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
from quantlab.research.s6_market_input_admission import (
    S6MarketCorporateActionBasis,
    S6MarketFieldDefinition,
    S6MarketFieldEvidenceStatus,
    S6MarketInputKind,
    S6MarketObservationTiming,
    S6MarketPriceBasis,
    S6MarketShareScope,
    build_s6_market_input_catalog,
)
from quantlab.research.s6_market_observation import (
    S6MarketObservationStatus,
    make_s6_market_observation_record,
)

_CONTEXT = S6FormulaBindingContext("synthetic-provider", "synthetic-source")
_INSTRUMENT = "000001.SZ"
_PERIOD_END = date(2024, 3, 31)
_TRADE_DATE = date(2024, 6, 3)
_AS_OF = datetime(2024, 6, 4, tzinfo=UTC)


def _field_definition() -> S6FinancialFieldDefinition:
    return S6FinancialFieldDefinition(
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        raw_field_id="net_income",
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


def _market_definition() -> S6MarketFieldDefinition:
    return S6MarketFieldDefinition(
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        raw_field_id="total_mv",
        semantic_field_id="market_cap",
        input_kind=S6MarketInputKind.MARKET_CAP,
        price_basis=S6MarketPriceBasis.RAW_UNADJUSTED,
        share_scope=S6MarketShareScope.TOTAL_OUTSTANDING,
        currency="CNY",
        observation_timing=S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE,
        corporate_action_basis=(
            S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE
        ),
        evidence_status=S6MarketFieldEvidenceStatus.VERIFIED,
        documentation_fingerprint="market-documentation",
    )


def _spec() -> S6FormulaSpec:
    return S6FormulaSpec(
        metric_id="earnings_to_price",
        family=S6MetricFamily.VALUE,
        operator=S6FormulaOperator.RATIO,
        inputs=(
            S6FormulaInputRef(
                0,
                "earnings",
                S6FormulaInputKind.FINANCIAL_FIELD,
                "net_income_attributable",
            ),
            S6FormulaInputRef(
                1,
                "market_value",
                S6FormulaInputKind.MARKET_FIELD,
                "market_cap",
            ),
        ),
        direction=S6MetricDirection.HIGHER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.LATEST_PIT_COMPATIBLE,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.EXCLUDE_OBSERVATION,
        negative_denominator_policy=S6DenominatorPolicy.EXCLUDE_OBSERVATION,
        evidence_status=S6FormulaEvidenceStatus.VERIFIED,
        documentation_fingerprint="formula-documentation",
        rationale_fingerprint="formula-rationale",
    )


def _financial_record():
    return make_s6_financial_vintage_record(
        instrument_id=_INSTRUMENT,
        fiscal_period_end=_PERIOD_END,
        report_kind=S6FiscalReportKind.Q1,
        statement=S6FinancialStatement.INCOME_STATEMENT,
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        revision_id="original",
        announced_at=datetime(2024, 4, 20, 8, tzinfo=UTC),
        available_at=datetime(2024, 4, 20, 9, tzinfo=UTC),
        retrieved_at=datetime(2024, 5, 1, tzinfo=UTC),
        raw_field_ids=("net_income",),
        content_fingerprint="financial-content",
        status=S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
    )


def _children(*, spec=None, formula_specs=None, financial_records=None):
    bound_spec = spec or _spec()
    specs = formula_specs or (bound_spec,)
    field_catalog = build_s6_financial_field_catalog((_field_definition(),))
    market_catalog = build_s6_market_input_catalog((_market_definition(),))
    binding = audit_s6_formula_input_bindings(
        spec=bound_spec,
        formula_catalog=build_s6_formula_spec_catalog(specs),
        field_catalog=field_catalog,
        context=_CONTEXT,
        market_catalog=market_catalog,
    )
    financial = audit_s6_formula_financial_readiness(
        binding_audit=binding,
        field_catalog=field_catalog,
        vintage_records=(
            (_financial_record(),)
            if financial_records is None
            else financial_records
        ),
        instrument_id=_INSTRUMENT,
        fiscal_period_end=_PERIOD_END,
        as_of=_AS_OF,
    )
    definition = market_catalog.definitions[0]
    observation = make_s6_market_observation_record(
        definition=definition,
        instrument_id=_INSTRUMENT,
        trade_date=_TRADE_DATE,
        revision_id="original",
        observed_at=datetime(2024, 6, 3, 7, tzinfo=UTC),
        available_at=datetime(2024, 6, 3, 7, 5, tzinfo=UTC),
        retrieved_at=_AS_OF,
        content_fingerprint="market-content",
        status=S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED,
    )
    market = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=market_catalog,
        observation_records=(observation,),
        instrument_id=_INSTRUMENT,
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )
    return binding, financial, market


def test_ready_children_complete_metadata_preflight_without_numeric_authority() -> None:
    binding, financial, market = _children()

    result = build_s6_formula_readiness_preflight(
        binding_audit=binding,
        financial_readiness=financial,
        market_readiness=market,
    )

    assert result.binding_input_count == 2
    assert result.financial_input_count == 1
    assert result.market_input_count == 1
    assert result.derived_input_count == 0
    assert result.child_input_coverage_complete is True
    assert result.all_direct_inputs_ready is True
    assert result.metadata_preflight_ready is True
    assert result.numeric_values_included is False
    assert result.formula_computation_ready is False
    assert result.performance_claim is False
    assert result.portfolio_or_risk_authority is False
    assert result.account_mutation_authority is False
    assert result.broker_order_authority is False
    assert result.fingerprint


def test_failed_direct_child_blocks_preflight() -> None:
    binding, financial, market = _children(financial_records=())

    result = build_s6_formula_readiness_preflight(
        binding_audit=binding,
        financial_readiness=financial,
        market_readiness=market,
    )

    assert result.child_input_coverage_complete is True
    assert result.all_direct_inputs_ready is False
    assert result.metadata_preflight_ready is False


def test_child_binding_and_target_mismatches_fail_closed() -> None:
    binding, financial, market = _children()

    with pytest.raises(ValueError, match="exact binding audit"):
        build_s6_formula_readiness_preflight(
            binding_audit=binding,
            financial_readiness=replace(
                financial,
                binding_audit_fingerprint="different-binding",
            ),
            market_readiness=market,
        )
    with pytest.raises(ValueError, match="as-of targets"):
        build_s6_formula_readiness_preflight(
            binding_audit=binding,
            financial_readiness=financial,
            market_readiness=replace(market, as_of=_AS_OF + timedelta(days=1)),
        )
    with pytest.raises(ValueError, match="instrument targets"):
        build_s6_formula_readiness_preflight(
            binding_audit=binding,
            financial_readiness=financial,
            market_readiness=replace(market, instrument_id="600000.SH"),
        )


def test_unexpanded_derived_input_blocks_metadata_preflight() -> None:
    dependency = _spec()
    derived = S6FormulaSpec(
        metric_id="derived_identity",
        family=S6MetricFamily.VALUE,
        operator=S6FormulaOperator.IDENTITY,
        inputs=(
            S6FormulaInputRef(
                0,
                "derived_ep",
                S6FormulaInputKind.DERIVED_METRIC,
                dependency.metric_id,
            ),
        ),
        direction=S6MetricDirection.HIGHER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.LATEST_PIT_COMPATIBLE,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        negative_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        evidence_status=S6FormulaEvidenceStatus.VERIFIED,
        documentation_fingerprint="derived-documentation",
        rationale_fingerprint="derived-rationale",
    )
    binding, financial, market = _children(
        spec=derived,
        formula_specs=(dependency, derived),
    )

    result = build_s6_formula_readiness_preflight(
        binding_audit=binding,
        financial_readiness=financial,
        market_readiness=market,
    )

    assert binding.overall_admissible is True
    assert result.binding_input_count == 1
    assert result.derived_input_count == 1
    assert result.all_direct_inputs_ready is False
    assert result.derived_inputs_expanded is False
    assert result.metadata_preflight_ready is False


def test_direct_construction_cannot_add_authority_or_fake_counts() -> None:
    binding, financial, market = _children()
    result = build_s6_formula_readiness_preflight(
        binding_audit=binding,
        financial_readiness=financial,
        market_readiness=market,
    )

    with pytest.raises(ValueError, match="coverage"):
        replace(result, binding_input_count=3)
    with pytest.raises(ValueError, match="derived inputs"):
        replace(result, derived_inputs_expanded=True)
    with pytest.raises(ValueError, match="cannot acquire"):
        replace(result, formula_computation_ready=True)
