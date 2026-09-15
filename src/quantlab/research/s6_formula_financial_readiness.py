"""Audit direct S6 financial formula inputs for point-in-time readiness."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_financial_field_admission import (
    S6FinancialFieldCatalog,
    S6FinancialFieldDefinition,
    audit_s6_financial_field_admission,
)
from quantlab.research.s6_financial_vintage import (
    S6FinancialSelectionVerdict,
    S6FinancialStatement,
    S6FinancialVintageRecord,
    build_s6_financial_vintage_inventory,
    select_s6_financial_vintage,
)
from quantlab.research.s6_formula_input_binding import (
    S6FormulaInputBindingAudit,
    S6FormulaInputBindingRow,
    S6FormulaInputBindingVerdict,
)
from quantlab.research.s6_formula_spec import S6FormulaInputKind


class S6FormulaFinancialReadinessVerdict(StrEnum):
    READY = "ready"
    BINDING_BLOCKED = "binding_blocked"
    DEFINITION_FINGERPRINT_MISSING = "definition_fingerprint_missing"
    NOT_YET_AVAILABLE = "not_yet_available"
    UNVERIFIED_VINTAGE = "unverified_vintage"
    MISSING_VINTAGE = "missing_vintage"
    FIELD_MISSING_FROM_VINTAGE = "field_missing_from_vintage"
    FIELD_ADMISSION_BLOCKED = "field_admission_blocked"


_SELECTION_TO_READINESS = {
    S6FinancialSelectionVerdict.NOT_YET_AVAILABLE: (
        S6FormulaFinancialReadinessVerdict.NOT_YET_AVAILABLE
    ),
    S6FinancialSelectionVerdict.UNVERIFIED_VINTAGE: (
        S6FormulaFinancialReadinessVerdict.UNVERIFIED_VINTAGE
    ),
    S6FinancialSelectionVerdict.MISSING: (
        S6FormulaFinancialReadinessVerdict.MISSING_VINTAGE
    ),
}


@dataclass(frozen=True)
class S6FormulaFinancialReadinessRow:
    position: int
    input_id: str
    semantic_id: str
    statement: S6FinancialStatement | None
    binding_verdict: S6FormulaInputBindingVerdict
    readiness_verdict: S6FormulaFinancialReadinessVerdict
    definition_fingerprint: str | None
    selection_verdict: S6FinancialSelectionVerdict | None
    selected_record_fingerprint: str | None
    field_admission_fingerprint: str | None
    reason: str
    ready: bool

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("input position cannot be negative")
        for name in ("input_id", "semantic_id", "reason"):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        binding_admitted = (
            self.binding_verdict is S6FormulaInputBindingVerdict.ADMISSIBLE
        )
        if binding_admitted == (
            self.readiness_verdict
            is S6FormulaFinancialReadinessVerdict.BINDING_BLOCKED
        ):
            raise ValueError("binding verdict and readiness verdict are inconsistent")
        if binding_admitted != (self.definition_fingerprint is not None):
            raise ValueError(
                "only admitted bindings may carry a definition fingerprint"
            )
        if self.ready != (
            self.readiness_verdict is S6FormulaFinancialReadinessVerdict.READY
        ):
            raise ValueError("row readiness must match readiness verdict")
        if self.ready and not (
            self.selection_verdict is S6FinancialSelectionVerdict.ADMISSIBLE
            and self.selected_record_fingerprint is not None
            and self.field_admission_fingerprint is not None
        ):
            raise ValueError("ready rows require selected vintage and field admission")
        if self.selection_verdict in _SELECTION_TO_READINESS and (
            self.readiness_verdict
            is not _SELECTION_TO_READINESS[self.selection_verdict]
        ):
            raise ValueError("readiness must preserve failed vintage selection")
        if self.selected_record_fingerprint is not None and (
            self.selection_verdict is not S6FinancialSelectionVerdict.ADMISSIBLE
        ):
            raise ValueError("only an admitted selection may identify a vintage")


@dataclass(frozen=True)
class S6FormulaFinancialReadinessAudit:
    metric_id: str
    binding_audit_fingerprint: str
    field_catalog_fingerprint: str
    vintage_inventory_fingerprint: str
    provider_id: str
    source_id: str
    instrument_id: str
    fiscal_period_end: date
    as_of: datetime
    binding_overall_admissible: bool
    rows: tuple[S6FormulaFinancialReadinessRow, ...]
    financial_input_count: int
    ready_count: int
    all_direct_financial_inputs_ready: bool
    financial_gate_ready: bool
    direct_financial_inputs_only: bool = True
    market_observation_readiness_included: bool = False
    derived_metric_inputs_expanded: bool = False
    formula_computation_ready: bool = False
    numeric_values_included: bool = False
    performance_claim: bool = False
    portfolio_or_risk_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "metric_id",
            "binding_audit_fingerprint",
            "field_catalog_fingerprint",
            "vintage_inventory_fingerprint",
            "provider_id",
            "source_id",
            "instrument_id",
        ):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        _require_utc(self.as_of, "as_of")
        positions = tuple(row.position for row in self.rows)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("rows must retain unique increasing formula positions")
        if self.financial_input_count != len(self.rows):
            raise ValueError("financial_input_count does not match rows")
        if self.ready_count != sum(row.ready for row in self.rows):
            raise ValueError("ready_count does not match rows")
        expected_all = bool(self.rows) and all(row.ready for row in self.rows)
        if self.all_direct_financial_inputs_ready != expected_all:
            raise ValueError("all_direct_financial_inputs_ready does not match rows")
        expected_gate = self.binding_overall_admissible and expected_all
        if self.financial_gate_ready != expected_gate:
            raise ValueError("financial_gate_ready does not match binding and rows")
        if (
            not self.direct_financial_inputs_only
            or self.market_observation_readiness_included
            or self.derived_metric_inputs_expanded
            or self.formula_computation_ready
            or self.numeric_values_included
            or self.performance_claim
            or self.portfolio_or_risk_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "financial readiness cannot acquire formula, value, risk, or "
                "execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_audit_payload(self)),
        )


def audit_s6_formula_financial_readiness(
    *,
    binding_audit: S6FormulaInputBindingAudit,
    field_catalog: S6FinancialFieldCatalog,
    vintage_records: Iterable[S6FinancialVintageRecord],
    instrument_id: str,
    fiscal_period_end: date,
    as_of: datetime,
) -> S6FormulaFinancialReadinessAudit:
    """Audit direct financial bindings against one explicit PIT period target."""

    if binding_audit.field_catalog_fingerprint != field_catalog.fingerprint:
        raise ValueError("binding audit is not bound to the exact field catalog")
    normalized_instrument = instrument_id.strip()
    if not normalized_instrument:
        raise ValueError("instrument_id must be non-empty")
    normalized_as_of = _to_utc(as_of, "as_of")
    inventory = build_s6_financial_vintage_inventory(vintage_records)
    definitions = {
        item.fingerprint: item
        for item in field_catalog.definitions
    }
    scoped_records = tuple(
        item
        for item in inventory.records
        if item.provider_id == binding_audit.provider_id
        and item.source_id == binding_audit.source_id
    )
    rows = tuple(
        _audit_financial_row(
            binding_row=row,
            definitions=definitions,
            field_catalog=field_catalog,
            vintage_records=scoped_records,
            instrument_id=normalized_instrument,
            fiscal_period_end=fiscal_period_end,
            as_of=normalized_as_of,
        )
        for row in binding_audit.rows
        if row.kind is S6FormulaInputKind.FINANCIAL_FIELD
    )
    all_ready = bool(rows) and all(row.ready for row in rows)
    return S6FormulaFinancialReadinessAudit(
        metric_id=binding_audit.metric_id,
        binding_audit_fingerprint=binding_audit.fingerprint,
        field_catalog_fingerprint=field_catalog.fingerprint,
        vintage_inventory_fingerprint=inventory.fingerprint,
        provider_id=binding_audit.provider_id,
        source_id=binding_audit.source_id,
        instrument_id=normalized_instrument,
        fiscal_period_end=fiscal_period_end,
        as_of=normalized_as_of,
        binding_overall_admissible=binding_audit.overall_admissible,
        rows=rows,
        financial_input_count=len(rows),
        ready_count=sum(row.ready for row in rows),
        all_direct_financial_inputs_ready=all_ready,
        financial_gate_ready=binding_audit.overall_admissible and all_ready,
    )


def _audit_financial_row(
    *,
    binding_row: S6FormulaInputBindingRow,
    definitions: dict[str, S6FinancialFieldDefinition],
    field_catalog: S6FinancialFieldCatalog,
    vintage_records: tuple[S6FinancialVintageRecord, ...],
    instrument_id: str,
    fiscal_period_end: date,
    as_of: datetime,
) -> S6FormulaFinancialReadinessRow:
    if not binding_row.admissible:
        return _row(
            binding_row=binding_row,
            verdict=S6FormulaFinancialReadinessVerdict.BINDING_BLOCKED,
            reason=f"formula_binding_blocked:{binding_row.reason}",
        )

    definition_fingerprint = binding_row.bound_fingerprint
    if definition_fingerprint is None:
        raise ValueError(
            "admitted financial binding must carry a definition fingerprint"
        )
    definition = definitions.get(definition_fingerprint)
    if definition is None:
        return _row(
            binding_row=binding_row,
            verdict=(
                S6FormulaFinancialReadinessVerdict.DEFINITION_FINGERPRINT_MISSING
            ),
            reason="bound_definition_fingerprint_missing_from_field_catalog",
            definition_fingerprint=definition_fingerprint,
        )

    selection = select_s6_financial_vintage(
        vintage_records,
        instrument_id=instrument_id,
        fiscal_period_end=fiscal_period_end,
        statement=definition.statement,
        as_of=as_of,
    )
    if selection.selected is None:
        return _row(
            binding_row=binding_row,
            verdict=_SELECTION_TO_READINESS[selection.verdict],
            reason=selection.reason,
            definition_fingerprint=definition_fingerprint,
            statement=definition.statement,
            selection_verdict=selection.verdict,
        )

    record = selection.selected
    if definition.raw_field_id not in record.raw_field_ids:
        return _row(
            binding_row=binding_row,
            verdict=S6FormulaFinancialReadinessVerdict.FIELD_MISSING_FROM_VINTAGE,
            reason="bound_raw_field_is_absent_from_selected_vintage",
            definition_fingerprint=definition_fingerprint,
            statement=definition.statement,
            selection_verdict=selection.verdict,
            selected_record_fingerprint=record.fingerprint,
        )

    field_audit = audit_s6_financial_field_admission(
        record=record,
        catalog=field_catalog,
    )
    field_row = next(
        row
        for row in field_audit.rows
        if row.raw_field_id == definition.raw_field_id
    )
    if (
        not field_row.admissible
        or field_row.definition_fingerprint != definition_fingerprint
    ):
        return _row(
            binding_row=binding_row,
            verdict=S6FormulaFinancialReadinessVerdict.FIELD_ADMISSION_BLOCKED,
            reason=f"selected_field_blocked:{field_row.reason}",
            definition_fingerprint=definition_fingerprint,
            statement=definition.statement,
            selection_verdict=selection.verdict,
            selected_record_fingerprint=record.fingerprint,
            field_admission_fingerprint=field_audit.fingerprint,
        )
    return _row(
        binding_row=binding_row,
        verdict=S6FormulaFinancialReadinessVerdict.READY,
        reason="verified_field_present_in_latest_visible_verified_vintage",
        definition_fingerprint=definition_fingerprint,
        statement=definition.statement,
        selection_verdict=selection.verdict,
        selected_record_fingerprint=record.fingerprint,
        field_admission_fingerprint=field_audit.fingerprint,
    )


def _row(
    *,
    binding_row: S6FormulaInputBindingRow,
    verdict: S6FormulaFinancialReadinessVerdict,
    reason: str,
    definition_fingerprint: str | None = None,
    statement: S6FinancialStatement | None = None,
    selection_verdict: S6FinancialSelectionVerdict | None = None,
    selected_record_fingerprint: str | None = None,
    field_admission_fingerprint: str | None = None,
) -> S6FormulaFinancialReadinessRow:
    return S6FormulaFinancialReadinessRow(
        position=binding_row.position,
        input_id=binding_row.input_id,
        semantic_id=binding_row.semantic_id,
        statement=statement,
        binding_verdict=binding_row.verdict,
        readiness_verdict=verdict,
        definition_fingerprint=definition_fingerprint,
        selection_verdict=selection_verdict,
        selected_record_fingerprint=selected_record_fingerprint,
        field_admission_fingerprint=field_admission_fingerprint,
        reason=reason,
        ready=verdict is S6FormulaFinancialReadinessVerdict.READY,
    )


def _to_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be normalized to UTC")


def _audit_payload(item: S6FormulaFinancialReadinessAudit) -> dict[str, object]:
    return {
        "metric_id": item.metric_id,
        "binding_audit_fingerprint": item.binding_audit_fingerprint,
        "field_catalog_fingerprint": item.field_catalog_fingerprint,
        "vintage_inventory_fingerprint": item.vintage_inventory_fingerprint,
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "instrument_id": item.instrument_id,
        "fiscal_period_end": item.fiscal_period_end.isoformat(),
        "as_of": item.as_of.isoformat(),
        "binding_overall_admissible": item.binding_overall_admissible,
        "rows": [
            {
                "position": row.position,
                "input_id": row.input_id,
                "semantic_id": row.semantic_id,
                "statement": row.statement.value if row.statement is not None else None,
                "binding_verdict": row.binding_verdict.value,
                "readiness_verdict": row.readiness_verdict.value,
                "definition_fingerprint": row.definition_fingerprint,
                "selection_verdict": (
                    row.selection_verdict.value
                    if row.selection_verdict is not None
                    else None
                ),
                "selected_record_fingerprint": row.selected_record_fingerprint,
                "field_admission_fingerprint": row.field_admission_fingerprint,
                "reason": row.reason,
                "ready": row.ready,
            }
            for row in item.rows
        ],
        "financial_input_count": item.financial_input_count,
        "ready_count": item.ready_count,
        "all_direct_financial_inputs_ready": (
            item.all_direct_financial_inputs_ready
        ),
        "financial_gate_ready": item.financial_gate_ready,
        "direct_financial_inputs_only": item.direct_financial_inputs_only,
        "market_observation_readiness_included": (
            item.market_observation_readiness_included
        ),
        "derived_metric_inputs_expanded": item.derived_metric_inputs_expanded,
        "formula_computation_ready": item.formula_computation_ready,
        "numeric_values_included": item.numeric_values_included,
        "performance_claim": item.performance_claim,
        "portfolio_or_risk_authority": item.portfolio_or_risk_authority,
        "broker_order_authority": item.broker_order_authority,
    }
