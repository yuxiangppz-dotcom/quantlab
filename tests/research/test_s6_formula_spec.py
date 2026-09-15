from __future__ import annotations

from dataclasses import replace

import pytest

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


def _input(
    position: int,
    input_id: str,
    *,
    kind: S6FormulaInputKind = S6FormulaInputKind.FINANCIAL_FIELD,
    semantic_id: str | None = None,
) -> S6FormulaInputRef:
    return S6FormulaInputRef(
        position=position,
        input_id=input_id,
        kind=kind,
        semantic_id=semantic_id or input_id,
    )


def _ratio(
    metric_id: str = "synthetic_ratio",
    *,
    evidence_status: S6FormulaEvidenceStatus = S6FormulaEvidenceStatus.VERIFIED,
    direction: S6MetricDirection = S6MetricDirection.HIGHER_IS_BETTER,
    period_alignment: S6PeriodAlignmentPolicy = (
        S6PeriodAlignmentPolicy.LATEST_PIT_COMPATIBLE
    ),
    missing_policy: S6MissingInputPolicy = S6MissingInputPolicy.EXCLUDE_OBSERVATION,
    zero_policy: S6DenominatorPolicy = S6DenominatorPolicy.EXCLUDE_OBSERVATION,
    negative_policy: S6DenominatorPolicy = S6DenominatorPolicy.ALLOW_SIGNED,
    documentation_fingerprint: str | None = "documentation",
    rationale_fingerprint: str | None = "rationale",
    inputs: tuple[S6FormulaInputRef, ...] | None = None,
) -> S6FormulaSpec:
    return S6FormulaSpec(
        metric_id=metric_id,
        family=S6MetricFamily.VALUE,
        operator=S6FormulaOperator.RATIO,
        inputs=inputs
        or (
            _input(0, "numerator"),
            _input(
                1,
                "denominator",
                kind=S6FormulaInputKind.MARKET_FIELD,
            ),
        ),
        direction=direction,
        period_alignment=period_alignment,
        missing_input_policy=missing_policy,
        zero_denominator_policy=zero_policy,
        negative_denominator_policy=negative_policy,
        evidence_status=evidence_status,
        documentation_fingerprint=documentation_fingerprint,
        rationale_fingerprint=rationale_fingerprint,
    )


def test_complete_verified_ratio_spec_is_admitted_without_computation_authority() -> None:
    catalog = build_s6_formula_spec_catalog((_ratio(),))

    assert catalog.admitted_count == 1
    assert catalog.blocked_count == 0
    assert catalog.admission_rows[0].admitted is True
    assert catalog.admission_rows[0].reasons == ()
    assert catalog.concrete_candidate_formulas_frozen is False
    assert catalog.numeric_values_included is False
    assert catalog.metric_computation_authority is False
    assert catalog.performance_claim is False
    assert catalog.broker_order_authority is False
    assert catalog.fingerprint


def test_unknown_and_missing_choices_remain_explicit_blockers() -> None:
    spec = _ratio(
        evidence_status=S6FormulaEvidenceStatus.UNKNOWN,
        direction=S6MetricDirection.UNKNOWN,
        period_alignment=S6PeriodAlignmentPolicy.UNKNOWN,
        missing_policy=S6MissingInputPolicy.UNKNOWN,
        zero_policy=S6DenominatorPolicy.UNKNOWN,
        negative_policy=S6DenominatorPolicy.UNKNOWN,
        documentation_fingerprint=None,
        rationale_fingerprint=None,
    )

    row = build_s6_formula_spec_catalog((spec,)).admission_rows[0]

    assert row.admitted is False
    assert row.reasons == (
        "documentation_evidence_missing",
        "economic_rationale_evidence_missing",
        "formula_evidence_not_verified",
        "metric_direction_unknown",
        "missing_input_policy_unknown",
        "negative_denominator_policy_unknown",
        "period_alignment_unknown",
        "zero_denominator_policy_unknown",
    )


def test_draft_formula_is_inventoried_but_not_admitted() -> None:
    spec = _ratio(evidence_status=S6FormulaEvidenceStatus.DRAFT_UNVERIFIED)

    catalog = build_s6_formula_spec_catalog((spec,))

    assert catalog.admitted_count == 0
    assert catalog.blocked_count == 1
    assert catalog.admission_rows[0].reasons == ("formula_evidence_not_verified",)


@pytest.mark.parametrize(
    ("operator", "inputs", "message"),
    [
        (S6FormulaOperator.IDENTITY, (), "exactly one"),
        (S6FormulaOperator.RATIO, (_input(0, "only"),), "exactly two"),
        (S6FormulaOperator.DIFFERENCE, (_input(0, "only"),), "exactly two"),
        (S6FormulaOperator.SUM, (_input(0, "only"),), "at least two"),
    ],
)
def test_operator_arity_is_structural_and_fails_closed(
    operator: S6FormulaOperator,
    inputs: tuple[S6FormulaInputRef, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(_ratio(), operator=operator, inputs=inputs)


def test_input_positions_ids_and_semantics_are_deterministic() -> None:
    with pytest.raises(ValueError, match="contiguous frozen positions"):
        _ratio(inputs=(_input(1, "first"), _input(2, "second")))
    with pytest.raises(ValueError, match="input IDs must be unique"):
        _ratio(inputs=(_input(0, "same"), _input(1, "same", semantic_id="other")))
    with pytest.raises(ValueError, match="semantic references must be unique"):
        _ratio(inputs=(_input(0, "first"), _input(1, "second", semantic_id="first")))


def test_self_and_catalog_level_dependency_cycles_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot reference itself"):
        _ratio(
            metric_id="self_metric",
            inputs=(
                _input(
                    0,
                    "self",
                    kind=S6FormulaInputKind.DERIVED_METRIC,
                    semantic_id="self_metric",
                ),
                _input(1, "denominator"),
            ),
        )

    first = _ratio(
        metric_id="metric_a",
        inputs=(
            _input(
                0,
                "derived_b",
                kind=S6FormulaInputKind.DERIVED_METRIC,
                semantic_id="metric_b",
            ),
            _input(1, "denominator_a"),
        ),
    )
    second = _ratio(
        metric_id="metric_b",
        inputs=(
            _input(
                0,
                "derived_a",
                kind=S6FormulaInputKind.DERIVED_METRIC,
                semantic_id="metric_a",
            ),
            _input(1, "denominator_b"),
        ),
    )

    with pytest.raises(ValueError, match="circular derived-metric dependency"):
        build_s6_formula_spec_catalog((first, second))


def test_denominator_policies_are_required_only_for_ratios() -> None:
    with pytest.raises(ValueError, match="explicit denominator policies"):
        _ratio(zero_policy=S6DenominatorPolicy.NOT_APPLICABLE)
    with pytest.raises(ValueError, match="zero denominator cannot allow signed"):
        _ratio(zero_policy=S6DenominatorPolicy.ALLOW_SIGNED)

    identity = replace(
        _ratio(),
        operator=S6FormulaOperator.IDENTITY,
        inputs=(_input(0, "value"),),
        zero_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
        negative_denominator_policy=S6DenominatorPolicy.NOT_APPLICABLE,
    )
    assert build_s6_formula_spec_catalog((identity,)).admitted_count == 1

    with pytest.raises(ValueError, match="not-applicable denominator policies"):
        replace(identity, zero_denominator_policy=S6DenominatorPolicy.FAIL_RUN)


def test_catalog_is_order_invariant_and_constructor_cannot_bypass_invariants() -> None:
    first = _ratio("alpha_metric")
    second = _ratio("zeta_metric")
    catalog = build_s6_formula_spec_catalog((second, first))
    rebuilt = build_s6_formula_spec_catalog((first, second))

    assert catalog == rebuilt
    assert catalog.fingerprint == rebuilt.fingerprint
    assert [item.metric_id for item in catalog.specs] == [
        "alpha_metric",
        "zeta_metric",
    ]

    with pytest.raises(ValueError, match="deterministic metric order"):
        replace(catalog, specs=tuple(reversed(catalog.specs)))
    with pytest.raises(ValueError, match="duplicate formula metric identity"):
        replace(
            catalog,
            specs=(first, first),
            admission_rows=(catalog.admission_rows[0], catalog.admission_rows[0]),
        )
    with pytest.raises(ValueError, match="counts do not match"):
        replace(catalog, admitted_count=0, blocked_count=2)
