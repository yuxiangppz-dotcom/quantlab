"""Metadata-only admission contract for future S6 financial metric formulas."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint

_SCHEMA = "quantlab_s6_formula_spec_catalog_v1"


class S6MetricFamily(StrEnum):
    VALUE = "value"
    PROFITABILITY = "profitability"
    QUALITY = "quality"
    LEVERAGE = "leverage"


class S6FormulaInputKind(StrEnum):
    FINANCIAL_FIELD = "financial_field"
    MARKET_FIELD = "market_field"
    DERIVED_METRIC = "derived_metric"


class S6FormulaOperator(StrEnum):
    IDENTITY = "identity"
    RATIO = "ratio"
    DIFFERENCE = "difference"
    SUM = "sum"


class S6MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    UNKNOWN = "unknown"


class S6PeriodAlignmentPolicy(StrEnum):
    SAME_FISCAL_PERIOD = "same_fiscal_period"
    LATEST_PIT_COMPATIBLE = "latest_pit_compatible"
    UNKNOWN = "unknown"


class S6MissingInputPolicy(StrEnum):
    EXCLUDE_OBSERVATION = "exclude_observation"
    FAIL_RUN = "fail_run"
    UNKNOWN = "unknown"


class S6DenominatorPolicy(StrEnum):
    EXCLUDE_OBSERVATION = "exclude_observation"
    FAIL_RUN = "fail_run"
    ALLOW_SIGNED = "allow_signed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class S6FormulaEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    DRAFT_UNVERIFIED = "draft_unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class S6FormulaInputRef:
    position: int
    input_id: str
    kind: S6FormulaInputKind
    semantic_id: str

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("input position cannot be negative")
        for name in ("input_id", "semantic_id"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")


@dataclass(frozen=True)
class S6FormulaSpec:
    metric_id: str
    family: S6MetricFamily
    operator: S6FormulaOperator
    inputs: tuple[S6FormulaInputRef, ...]
    direction: S6MetricDirection
    period_alignment: S6PeriodAlignmentPolicy
    missing_input_policy: S6MissingInputPolicy
    zero_denominator_policy: S6DenominatorPolicy
    negative_denominator_policy: S6DenominatorPolicy
    evidence_status: S6FormulaEvidenceStatus
    documentation_fingerprint: str | None
    rationale_fingerprint: str | None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.metric_id or not self.metric_id.strip():
            raise ValueError("metric_id must be non-empty")
        if self.metric_id != self.metric_id.strip():
            raise ValueError("metric_id must be normalized")
        positions = tuple(item.position for item in self.inputs)
        if positions != tuple(range(len(self.inputs))):
            raise ValueError("formula inputs must use contiguous frozen positions")
        input_ids = tuple(item.input_id for item in self.inputs)
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("formula input IDs must be unique")
        semantic_keys = tuple((item.kind, item.semantic_id) for item in self.inputs)
        if len(semantic_keys) != len(set(semantic_keys)):
            raise ValueError("formula input semantic references must be unique")
        if any(
            item.kind is S6FormulaInputKind.DERIVED_METRIC
            and item.semantic_id == self.metric_id
            for item in self.inputs
        ):
            raise ValueError("formula cannot reference itself")
        _validate_operator_shape(self.operator, len(self.inputs))
        _validate_denominator_policy_shape(self)
        for name in ("documentation_fingerprint", "rationale_fingerprint"):
            value = getattr(self, name)
            if value is not None and (
                not value.strip() or value != value.strip()
            ):
                raise ValueError(f"{name} must be normalized when present")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_spec_payload(self)),
        )


@dataclass(frozen=True)
class S6FormulaAdmissionRow:
    metric_id: str
    admitted: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.metric_id:
            raise ValueError("metric_id must be non-empty")
        if self.admitted != (not self.reasons):
            raise ValueError("admission state must match reasons")
        if self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("admission reasons must be sorted and unique")


@dataclass(frozen=True)
class S6FormulaSpecCatalog:
    schema: str
    specs: tuple[S6FormulaSpec, ...]
    admission_rows: tuple[S6FormulaAdmissionRow, ...]
    admitted_count: int
    blocked_count: int
    concrete_candidate_formulas_frozen: bool = False
    numeric_values_included: bool = False
    metric_computation_authority: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.specs != tuple(sorted(self.specs, key=lambda item: item.metric_id)):
            raise ValueError("formula specs must be in deterministic metric order")
        metric_ids = tuple(item.metric_id for item in self.specs)
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("duplicate formula metric identity")
        _validate_acyclic_dependencies(self.specs)
        expected_rows = tuple(_admission_row(item) for item in self.specs)
        if self.admission_rows != expected_rows:
            raise ValueError("formula admission rows do not match specs")
        expected_admitted = sum(item.admitted for item in self.admission_rows)
        if (
            self.admitted_count != expected_admitted
            or self.blocked_count != len(self.specs) - expected_admitted
        ):
            raise ValueError("formula catalog counts do not match admission rows")
        if (
            self.concrete_candidate_formulas_frozen
            or self.numeric_values_included
            or self.metric_computation_authority
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("catalog cannot acquire formula, numeric, or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_catalog_payload(self)),
        )


def build_s6_formula_spec_catalog(
    specs: Iterable[S6FormulaSpec],
) -> S6FormulaSpecCatalog:
    """Inventory formula specifications without choosing a concrete S6 formula."""

    ordered = tuple(sorted(specs, key=lambda item: item.metric_id))
    rows = tuple(_admission_row(item) for item in ordered)
    admitted = sum(item.admitted for item in rows)
    return S6FormulaSpecCatalog(
        schema=_SCHEMA,
        specs=ordered,
        admission_rows=rows,
        admitted_count=admitted,
        blocked_count=len(ordered) - admitted,
    )


def _admission_row(spec: S6FormulaSpec) -> S6FormulaAdmissionRow:
    reasons: list[str] = []
    if spec.evidence_status is not S6FormulaEvidenceStatus.VERIFIED:
        reasons.append("formula_evidence_not_verified")
    if spec.documentation_fingerprint is None:
        reasons.append("documentation_evidence_missing")
    if spec.rationale_fingerprint is None:
        reasons.append("economic_rationale_evidence_missing")
    if spec.direction is S6MetricDirection.UNKNOWN:
        reasons.append("metric_direction_unknown")
    if spec.period_alignment is S6PeriodAlignmentPolicy.UNKNOWN:
        reasons.append("period_alignment_unknown")
    if spec.missing_input_policy is S6MissingInputPolicy.UNKNOWN:
        reasons.append("missing_input_policy_unknown")
    if spec.operator is S6FormulaOperator.RATIO:
        if spec.zero_denominator_policy is S6DenominatorPolicy.UNKNOWN:
            reasons.append("zero_denominator_policy_unknown")
        if spec.negative_denominator_policy is S6DenominatorPolicy.UNKNOWN:
            reasons.append("negative_denominator_policy_unknown")
    return S6FormulaAdmissionRow(
        metric_id=spec.metric_id,
        admitted=not reasons,
        reasons=tuple(sorted(reasons)),
    )


def _validate_operator_shape(operator: S6FormulaOperator, input_count: int) -> None:
    if operator is S6FormulaOperator.IDENTITY and input_count != 1:
        raise ValueError("identity formulas require exactly one input")
    if operator in {S6FormulaOperator.RATIO, S6FormulaOperator.DIFFERENCE}:
        if input_count != 2:
            raise ValueError(f"{operator.value} formulas require exactly two inputs")
    if operator is S6FormulaOperator.SUM and input_count < 2:
        raise ValueError("sum formulas require at least two inputs")


def _validate_denominator_policy_shape(spec: S6FormulaSpec) -> None:
    policies = (
        spec.zero_denominator_policy,
        spec.negative_denominator_policy,
    )
    if spec.operator is S6FormulaOperator.RATIO:
        if S6DenominatorPolicy.NOT_APPLICABLE in policies:
            raise ValueError("ratio formulas require explicit denominator policies")
        if spec.zero_denominator_policy is S6DenominatorPolicy.ALLOW_SIGNED:
            raise ValueError("zero denominator cannot allow signed evaluation")
    elif policies != (
        S6DenominatorPolicy.NOT_APPLICABLE,
        S6DenominatorPolicy.NOT_APPLICABLE,
    ):
        raise ValueError("non-ratio formulas require not-applicable denominator policies")


def _validate_acyclic_dependencies(specs: tuple[S6FormulaSpec, ...]) -> None:
    graph = {
        spec.metric_id: tuple(
            item.semantic_id
            for item in spec.inputs
            if item.kind is S6FormulaInputKind.DERIVED_METRIC
            and item.semantic_id in {candidate.metric_id for candidate in specs}
        )
        for spec in specs
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(metric_id: str) -> None:
        if metric_id in visiting:
            raise ValueError("circular derived-metric dependency")
        if metric_id in visited:
            return
        visiting.add(metric_id)
        for dependency in graph[metric_id]:
            visit(dependency)
        visiting.remove(metric_id)
        visited.add(metric_id)

    for metric_id in graph:
        visit(metric_id)


def _spec_payload(item: S6FormulaSpec) -> dict[str, object]:
    return {
        "metric_id": item.metric_id,
        "family": item.family.value,
        "operator": item.operator.value,
        "inputs": [
            {
                "position": value.position,
                "input_id": value.input_id,
                "kind": value.kind.value,
                "semantic_id": value.semantic_id,
            }
            for value in item.inputs
        ],
        "direction": item.direction.value,
        "period_alignment": item.period_alignment.value,
        "missing_input_policy": item.missing_input_policy.value,
        "zero_denominator_policy": item.zero_denominator_policy.value,
        "negative_denominator_policy": item.negative_denominator_policy.value,
        "evidence_status": item.evidence_status.value,
        "documentation_fingerprint": item.documentation_fingerprint,
        "rationale_fingerprint": item.rationale_fingerprint,
    }


def _catalog_payload(item: S6FormulaSpecCatalog) -> dict[str, object]:
    return {
        "schema": item.schema,
        "spec_fingerprints": [spec.fingerprint for spec in item.specs],
        "admission_rows": [
            {
                "metric_id": row.metric_id,
                "admitted": row.admitted,
                "reasons": list(row.reasons),
            }
            for row in item.admission_rows
        ],
        "admitted_count": item.admitted_count,
        "blocked_count": item.blocked_count,
        "concrete_candidate_formulas_frozen": (
            item.concrete_candidate_formulas_frozen
        ),
        "numeric_values_included": item.numeric_values_included,
        "metric_computation_authority": item.metric_computation_authority,
        "performance_claim": item.performance_claim,
        "broker_order_authority": item.broker_order_authority,
    }
