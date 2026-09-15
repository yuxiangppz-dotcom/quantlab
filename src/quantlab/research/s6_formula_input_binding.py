"""Bind S6 formula metadata to admitted semantic input definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_financial_field_admission import S6FinancialFieldCatalog
from quantlab.research.s6_formula_spec import (
    S6FormulaAdmissionRow,
    S6FormulaInputKind,
    S6FormulaInputRef,
    S6FormulaSpec,
    S6FormulaSpecCatalog,
)


class S6FormulaInputBindingVerdict(StrEnum):
    ADMISSIBLE = "admissible"
    FINANCIAL_FIELD_MISSING = "financial_field_missing"
    FINANCIAL_FIELD_UNVERIFIED = "financial_field_unverified"
    FINANCIAL_FIELD_AMBIGUOUS = "financial_field_ambiguous"
    DERIVED_METRIC_MISSING = "derived_metric_missing"
    DERIVED_METRIC_BLOCKED = "derived_metric_blocked"
    MARKET_INPUT_CONTRACT_MISSING = "market_input_contract_missing"


@dataclass(frozen=True)
class S6FormulaBindingContext:
    provider_id: str
    source_id: str

    def __post_init__(self) -> None:
        for name in ("provider_id", "source_id"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")


@dataclass(frozen=True)
class S6FormulaInputBindingRow:
    position: int
    input_id: str
    kind: S6FormulaInputKind
    semantic_id: str
    verdict: S6FormulaInputBindingVerdict
    bound_fingerprint: str | None
    reason: str
    admissible: bool

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("input position cannot be negative")
        if not self.input_id or not self.semantic_id or not self.reason:
            raise ValueError("binding row identity and reason must be non-empty")
        if self.admissible != (
            self.verdict is S6FormulaInputBindingVerdict.ADMISSIBLE
        ):
            raise ValueError("binding admissibility must match verdict")
        if self.admissible != (self.bound_fingerprint is not None):
            raise ValueError("only admitted bindings may carry a fingerprint")


@dataclass(frozen=True)
class S6FormulaInputBindingAudit:
    metric_id: str
    formula_fingerprint: str
    formula_catalog_fingerprint: str
    field_catalog_fingerprint: str
    provider_id: str
    source_id: str
    formula_admitted: bool
    rows: tuple[S6FormulaInputBindingRow, ...]
    all_inputs_admitted: bool
    overall_admissible: bool
    metadata_only: bool = True
    numeric_values_included: bool = False
    metric_computation_authority: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "metric_id",
            "formula_fingerprint",
            "formula_catalog_fingerprint",
            "field_catalog_fingerprint",
            "provider_id",
            "source_id",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if tuple(row.position for row in self.rows) != tuple(range(len(self.rows))):
            raise ValueError("binding rows must retain contiguous formula-input order")
        input_ids = tuple(row.input_id for row in self.rows)
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("binding row input IDs must be unique")
        if self.all_inputs_admitted != all(row.admissible for row in self.rows):
            raise ValueError("all_inputs_admitted does not match rows")
        if self.overall_admissible != (
            self.formula_admitted and self.all_inputs_admitted
        ):
            raise ValueError("overall_admissible does not match formula and inputs")
        if (
            not self.metadata_only
            or self.numeric_values_included
            or self.metric_computation_authority
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("binding audit cannot acquire numeric or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_audit_payload(self)),
        )


def audit_s6_formula_input_bindings(
    *,
    spec: S6FormulaSpec,
    formula_catalog: S6FormulaSpecCatalog,
    field_catalog: S6FinancialFieldCatalog,
    context: S6FormulaBindingContext,
) -> S6FormulaInputBindingAudit:
    """Audit every formula input without reading or calculating numeric values."""

    catalog_specs = {
        item.metric_id: item
        for item in formula_catalog.specs
    }
    bound_spec = catalog_specs.get(spec.metric_id)
    if bound_spec is None or bound_spec.fingerprint != spec.fingerprint:
        raise ValueError("formula spec is not exactly bound to the formula catalog")
    formula_rows = {
        item.metric_id: item
        for item in formula_catalog.admission_rows
    }
    return _audit_spec(
        spec=bound_spec,
        formula_catalog=formula_catalog,
        catalog_specs=catalog_specs,
        formula_rows=formula_rows,
        field_catalog=field_catalog,
        context=context,
        cache={},
    )


def _audit_spec(
    *,
    spec: S6FormulaSpec,
    formula_catalog: S6FormulaSpecCatalog,
    catalog_specs: dict[str, S6FormulaSpec],
    formula_rows: dict[str, S6FormulaAdmissionRow],
    field_catalog: S6FinancialFieldCatalog,
    context: S6FormulaBindingContext,
    cache: dict[str, S6FormulaInputBindingAudit],
) -> S6FormulaInputBindingAudit:
    cached = cache.get(spec.metric_id)
    if cached is not None:
        return cached
    rows = tuple(
        _bind_input(
            input_ref=input_ref,
            formula_catalog=formula_catalog,
            catalog_specs=catalog_specs,
            formula_rows=formula_rows,
            field_catalog=field_catalog,
            context=context,
            cache=cache,
        )
        for input_ref in spec.inputs
    )
    formula_admitted = formula_rows[spec.metric_id].admitted
    all_inputs_admitted = all(item.admissible for item in rows)
    audit = S6FormulaInputBindingAudit(
        metric_id=spec.metric_id,
        formula_fingerprint=spec.fingerprint,
        formula_catalog_fingerprint=formula_catalog.fingerprint,
        field_catalog_fingerprint=field_catalog.fingerprint,
        provider_id=context.provider_id,
        source_id=context.source_id,
        formula_admitted=formula_admitted,
        rows=rows,
        all_inputs_admitted=all_inputs_admitted,
        overall_admissible=formula_admitted and all_inputs_admitted,
    )
    cache[spec.metric_id] = audit
    return audit


def _bind_input(
    *,
    input_ref: S6FormulaInputRef,
    formula_catalog: S6FormulaSpecCatalog,
    catalog_specs: dict[str, S6FormulaSpec],
    formula_rows: dict[str, S6FormulaAdmissionRow],
    field_catalog: S6FinancialFieldCatalog,
    context: S6FormulaBindingContext,
    cache: dict[str, S6FormulaInputBindingAudit],
) -> S6FormulaInputBindingRow:
    position = input_ref.position
    input_id = input_ref.input_id
    kind = input_ref.kind
    semantic_id = input_ref.semantic_id

    if kind is S6FormulaInputKind.MARKET_FIELD:
        return _blocked_row(
            position,
            input_id,
            kind,
            semantic_id,
            S6FormulaInputBindingVerdict.MARKET_INPUT_CONTRACT_MISSING,
            "market_input_contract_has_not_been_frozen",
        )
    if kind is S6FormulaInputKind.DERIVED_METRIC:
        dependency = catalog_specs.get(semantic_id)
        if dependency is None:
            return _blocked_row(
                position,
                input_id,
                kind,
                semantic_id,
                S6FormulaInputBindingVerdict.DERIVED_METRIC_MISSING,
                "derived_metric_is_missing_from_formula_catalog",
            )
        dependency_row = formula_rows[semantic_id]
        if not dependency_row.admitted:
            return _blocked_row(
                position,
                input_id,
                kind,
                semantic_id,
                S6FormulaInputBindingVerdict.DERIVED_METRIC_BLOCKED,
                "derived_metric_formula_is_not_admitted",
            )
        dependency_audit = _audit_spec(
            spec=dependency,
            formula_catalog=formula_catalog,
            catalog_specs=catalog_specs,
            formula_rows=formula_rows,
            field_catalog=field_catalog,
            context=context,
            cache=cache,
        )
        if not dependency_audit.overall_admissible:
            return _blocked_row(
                position,
                input_id,
                kind,
                semantic_id,
                S6FormulaInputBindingVerdict.DERIVED_METRIC_BLOCKED,
                "derived_metric_inputs_are_not_fully_admitted",
            )
        return _admitted_row(
            position,
            input_id,
            kind,
            semantic_id,
            dependency_audit.fingerprint,
            "derived_metric_formula_and_inputs_are_admitted",
        )

    candidates = tuple(
        item
        for item in field_catalog.definitions
        if item.provider_id == context.provider_id
        and item.source_id == context.source_id
        and item.semantic_field_id == semantic_id
    )
    if not candidates:
        return _blocked_row(
            position,
            input_id,
            kind,
            semantic_id,
            S6FormulaInputBindingVerdict.FINANCIAL_FIELD_MISSING,
            "financial_semantic_field_is_missing_for_provider_source",
        )
    admitted = tuple(item for item in candidates if item.semantically_admissible)
    if not admitted:
        return _blocked_row(
            position,
            input_id,
            kind,
            semantic_id,
            S6FormulaInputBindingVerdict.FINANCIAL_FIELD_UNVERIFIED,
            "financial_semantic_field_has_no_admitted_definition",
        )
    if len(admitted) > 1:
        return _blocked_row(
            position,
            input_id,
            kind,
            semantic_id,
            S6FormulaInputBindingVerdict.FINANCIAL_FIELD_AMBIGUOUS,
            "multiple_admitted_raw_fields_map_to_one_semantic_field",
        )
    return _admitted_row(
        position,
        input_id,
        kind,
        semantic_id,
        admitted[0].fingerprint,
        "exactly_one_admitted_financial_field_definition",
    )


def _blocked_row(
    position: int,
    input_id: str,
    kind: S6FormulaInputKind,
    semantic_id: str,
    verdict: S6FormulaInputBindingVerdict,
    reason: str,
) -> S6FormulaInputBindingRow:
    return S6FormulaInputBindingRow(
        position=position,
        input_id=input_id,
        kind=kind,
        semantic_id=semantic_id,
        verdict=verdict,
        bound_fingerprint=None,
        reason=reason,
        admissible=False,
    )


def _admitted_row(
    position: int,
    input_id: str,
    kind: S6FormulaInputKind,
    semantic_id: str,
    fingerprint: str,
    reason: str,
) -> S6FormulaInputBindingRow:
    return S6FormulaInputBindingRow(
        position=position,
        input_id=input_id,
        kind=kind,
        semantic_id=semantic_id,
        verdict=S6FormulaInputBindingVerdict.ADMISSIBLE,
        bound_fingerprint=fingerprint,
        reason=reason,
        admissible=True,
    )


def _audit_payload(item: S6FormulaInputBindingAudit) -> dict[str, object]:
    return {
        "metric_id": item.metric_id,
        "formula_fingerprint": item.formula_fingerprint,
        "formula_catalog_fingerprint": item.formula_catalog_fingerprint,
        "field_catalog_fingerprint": item.field_catalog_fingerprint,
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "formula_admitted": item.formula_admitted,
        "rows": [
            {
                "position": row.position,
                "input_id": row.input_id,
                "kind": row.kind.value,
                "semantic_id": row.semantic_id,
                "verdict": row.verdict.value,
                "bound_fingerprint": row.bound_fingerprint,
                "reason": row.reason,
                "admissible": row.admissible,
            }
            for row in item.rows
        ],
        "all_inputs_admitted": item.all_inputs_admitted,
        "overall_admissible": item.overall_admissible,
        "metadata_only": item.metadata_only,
        "numeric_values_included": item.numeric_values_included,
        "metric_computation_authority": item.metric_computation_authority,
        "performance_claim": item.performance_claim,
        "broker_order_authority": item.broker_order_authority,
    }
