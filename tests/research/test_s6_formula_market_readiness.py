from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from quantlab.research.s6_financial_field_admission import (
    build_s6_financial_field_catalog,
)
from quantlab.research.s6_formula_input_binding import (
    S6FormulaBindingContext,
    audit_s6_formula_input_bindings,
)
from quantlab.research.s6_formula_market_readiness import (
    S6FormulaMarketReadinessVerdict,
    audit_s6_formula_market_readiness,
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
    S6MarketObservationSelectionVerdict,
    S6MarketObservationStatus,
    make_s6_market_observation_record,
)

_CONTEXT = S6FormulaBindingContext("synthetic-provider", "synthetic-source")
_TRADE_DATE = date(2024, 6, 3)
_OBSERVED = datetime(2024, 6, 3, 7, tzinfo=UTC)
_AVAILABLE = datetime(2024, 6, 3, 7, 5, tzinfo=UTC)
_RETRIEVED = datetime(2024, 6, 4, tzinfo=UTC)
_AS_OF = datetime(2024, 6, 4, tzinfo=UTC)


def _definition(
    raw_field_id: str = "total_mv",
    semantic_field_id: str = "market_cap",
) -> S6MarketFieldDefinition:
    return S6MarketFieldDefinition(
        provider_id=_CONTEXT.provider_id,
        source_id=_CONTEXT.source_id,
        raw_field_id=raw_field_id,
        semantic_field_id=semantic_field_id,
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


def _spec(kind: S6FormulaInputKind = S6FormulaInputKind.MARKET_FIELD) -> S6FormulaSpec:
    return S6FormulaSpec(
        metric_id="market_identity",
        family=S6MetricFamily.VALUE,
        operator=S6FormulaOperator.IDENTITY,
        inputs=(
            S6FormulaInputRef(
                position=0,
                input_id="market_value",
                kind=kind,
                semantic_id="market_cap",
            ),
        ),
        direction=S6MetricDirection.LOWER_IS_BETTER,
        period_alignment=S6PeriodAlignmentPolicy.LATEST_PIT_COMPATIBLE,
        missing_input_policy=S6MissingInputPolicy.EXCLUDE_OBSERVATION,
        zero_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        negative_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        evidence_status=S6FormulaEvidenceStatus.VERIFIED,
        documentation_fingerprint="formula-documentation",
        rationale_fingerprint="formula-rationale",
    )


def _binding(
    *,
    spec: S6FormulaSpec | None = None,
    market_catalog=None,
):
    bound_spec = spec or _spec()
    catalog = market_catalog or build_s6_market_input_catalog((_definition(),))
    return (
        audit_s6_formula_input_bindings(
            spec=bound_spec,
            formula_catalog=build_s6_formula_spec_catalog((bound_spec,)),
            field_catalog=build_s6_financial_field_catalog(()),
            context=_CONTEXT,
            market_catalog=catalog,
        ),
        catalog,
    )


def _record(
    definition: S6MarketFieldDefinition,
    *,
    available_at: datetime = _AVAILABLE,
    status: S6MarketObservationStatus = (
        S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED
    ),
):
    return make_s6_market_observation_record(
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        revision_id="original",
        observed_at=_OBSERVED,
        available_at=available_at,
        retrieved_at=_RETRIEVED,
        content_fingerprint="market-content",
        status=status,
    )


def test_exact_visible_observation_passes_only_the_market_gate() -> None:
    binding, catalog = _binding()
    observation = _record(catalog.definitions[0])

    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=(observation,),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    assert audit.market_input_count == 1
    assert audit.ready_count == 1
    assert audit.rows[0].readiness_verdict is S6FormulaMarketReadinessVerdict.READY
    assert audit.rows[0].definition_fingerprint == (
        catalog.definitions[0].fingerprint
    )
    assert audit.rows[0].selected_record_fingerprint == observation.fingerprint
    assert audit.all_direct_market_inputs_ready is True
    assert audit.market_gate_ready is True
    assert audit.formula_computation_ready is False
    assert audit.numeric_values_included is False
    assert audit.performance_claim is False
    assert audit.portfolio_or_risk_authority is False
    assert audit.broker_order_authority is False
    assert audit.fingerprint


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        ((), S6FormulaMarketReadinessVerdict.MISSING),
        (
            (
                S6MarketObservationStatus.LATEST_ONLY_UNVERIFIED,
            ),
            S6FormulaMarketReadinessVerdict.UNVERIFIED_OBSERVATION,
        ),
    ],
)
def test_missing_and_unverified_observations_remain_distinct(
    records,
    expected: S6FormulaMarketReadinessVerdict,
) -> None:
    binding, catalog = _binding()
    definition = catalog.definitions[0]
    if records:
        observation_records = (_record(definition, status=records[0]),)
    else:
        observation_records = ()

    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=observation_records,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    assert audit.rows[0].readiness_verdict is expected
    assert audit.rows[0].selected_record_fingerprint is None
    assert audit.all_direct_market_inputs_ready is False
    assert audit.market_gate_ready is False


def test_future_observation_remains_not_yet_available() -> None:
    binding, catalog = _binding()
    future = _record(
        catalog.definitions[0],
        available_at=datetime(2024, 6, 5, tzinfo=UTC),
    )
    future = replace(future, retrieved_at=datetime(2024, 6, 6, tzinfo=UTC))

    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=(future,),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    assert audit.rows[0].readiness_verdict is (
        S6FormulaMarketReadinessVerdict.NOT_YET_AVAILABLE
    )
    assert audit.rows[0].observation_verdict is (
        S6MarketObservationSelectionVerdict.NOT_YET_AVAILABLE
    )
    assert audit.market_gate_ready is False


def test_blocked_binding_is_preserved_without_selecting_an_observation() -> None:
    empty_catalog = build_s6_market_input_catalog(())
    binding, catalog = _binding(market_catalog=empty_catalog)

    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=(),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    assert audit.rows[0].readiness_verdict is (
        S6FormulaMarketReadinessVerdict.BINDING_BLOCKED
    )
    assert audit.rows[0].observation_verdict is None
    assert audit.binding_overall_admissible is False
    assert audit.market_gate_ready is False


def test_market_catalog_must_match_the_binding_audit_exactly() -> None:
    binding, _ = _binding()
    changed_catalog = build_s6_market_input_catalog(
        (_definition("provider_total_mv"),)
    )

    with pytest.raises(ValueError, match="exact market catalog"):
        audit_s6_formula_market_readiness(
            binding_audit=binding,
            market_catalog=changed_catalog,
            observation_records=(),
            instrument_id="000001.SZ",
            trade_date=_TRADE_DATE,
            as_of=_AS_OF,
        )


def test_no_direct_market_inputs_never_claims_readiness() -> None:
    financial_spec = _spec(S6FormulaInputKind.FINANCIAL_FIELD)
    binding, catalog = _binding(spec=financial_spec)

    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=(),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    assert audit.rows == ()
    assert audit.market_input_count == 0
    assert audit.all_direct_market_inputs_ready is False
    assert audit.market_gate_ready is False
    assert audit.formula_computation_ready is False


def test_direct_audit_construction_cannot_bypass_summary_or_authority() -> None:
    binding, catalog = _binding()
    audit = audit_s6_formula_market_readiness(
        binding_audit=binding,
        market_catalog=catalog,
        observation_records=(_record(catalog.definitions[0]),),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=_AS_OF,
    )

    with pytest.raises(ValueError, match="market_input_count"):
        replace(audit, market_input_count=0)
    with pytest.raises(ValueError, match="ready_count"):
        replace(audit, ready_count=0)
    with pytest.raises(ValueError, match="all_direct_market_inputs_ready"):
        replace(audit, all_direct_market_inputs_ready=False)
    with pytest.raises(ValueError, match="market_gate_ready"):
        replace(audit, binding_overall_admissible=False)
    with pytest.raises(ValueError, match="cannot acquire"):
        replace(audit, formula_computation_ready=True)


def test_naive_as_of_is_rejected() -> None:
    binding, catalog = _binding()

    with pytest.raises(ValueError, match="timezone-aware"):
        audit_s6_formula_market_readiness(
            binding_audit=binding,
            market_catalog=catalog,
            observation_records=(),
            instrument_id="000001.SZ",
            trade_date=_TRADE_DATE,
            as_of=datetime(2024, 6, 4),
        )
