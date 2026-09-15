"""Audit direct S6 formula market inputs for point-in-time readiness."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_formula_input_binding import (
    S6FormulaInputBindingAudit,
    S6FormulaInputBindingRow,
    S6FormulaInputBindingVerdict,
)
from quantlab.research.s6_formula_spec import S6FormulaInputKind
from quantlab.research.s6_market_input_admission import (
    S6MarketFieldDefinition,
    S6MarketInputCatalog,
)
from quantlab.research.s6_market_observation import (
    S6MarketObservationRecord,
    S6MarketObservationSelectionVerdict,
    build_s6_market_observation_inventory,
    select_s6_market_observation,
)


class S6FormulaMarketReadinessVerdict(StrEnum):
    READY = "ready"
    BINDING_BLOCKED = "binding_blocked"
    DEFINITION_FINGERPRINT_MISSING = "definition_fingerprint_missing"
    DEFINITION_NOT_ADMISSIBLE = "definition_not_admissible"
    DEFINITION_MISMATCH = "definition_mismatch"
    NOT_YET_AVAILABLE = "not_yet_available"
    UNVERIFIED_OBSERVATION = "unverified_observation"
    MISSING = "missing"


_SELECTION_TO_READINESS = {
    S6MarketObservationSelectionVerdict.ADMISSIBLE: (
        S6FormulaMarketReadinessVerdict.READY
    ),
    S6MarketObservationSelectionVerdict.DEFINITION_NOT_ADMISSIBLE: (
        S6FormulaMarketReadinessVerdict.DEFINITION_NOT_ADMISSIBLE
    ),
    S6MarketObservationSelectionVerdict.DEFINITION_MISMATCH: (
        S6FormulaMarketReadinessVerdict.DEFINITION_MISMATCH
    ),
    S6MarketObservationSelectionVerdict.NOT_YET_AVAILABLE: (
        S6FormulaMarketReadinessVerdict.NOT_YET_AVAILABLE
    ),
    S6MarketObservationSelectionVerdict.UNVERIFIED_OBSERVATION: (
        S6FormulaMarketReadinessVerdict.UNVERIFIED_OBSERVATION
    ),
    S6MarketObservationSelectionVerdict.MISSING: (
        S6FormulaMarketReadinessVerdict.MISSING
    ),
}


@dataclass(frozen=True)
class S6FormulaMarketReadinessRow:
    position: int
    input_id: str
    semantic_id: str
    binding_verdict: S6FormulaInputBindingVerdict
    readiness_verdict: S6FormulaMarketReadinessVerdict
    definition_fingerprint: str | None
    observation_verdict: S6MarketObservationSelectionVerdict | None
    selected_record_fingerprint: str | None
    reason: str
    ready: bool

    def __post_init__(self) -> None:
        if self.position < 0:
            raise ValueError("input position cannot be negative")
        for name in ("input_id", "semantic_id", "reason"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        if self.ready != (
            self.readiness_verdict is S6FormulaMarketReadinessVerdict.READY
        ):
            raise ValueError("row readiness must match readiness verdict")
        binding_admitted = (
            self.binding_verdict is S6FormulaInputBindingVerdict.ADMISSIBLE
        )
        if binding_admitted == (
            self.readiness_verdict is S6FormulaMarketReadinessVerdict.BINDING_BLOCKED
        ):
            raise ValueError("binding verdict and readiness verdict are inconsistent")
        if binding_admitted != (self.definition_fingerprint is not None):
            raise ValueError("only admitted bindings may carry a definition fingerprint")
        if self.ready != (
            self.observation_verdict
            is S6MarketObservationSelectionVerdict.ADMISSIBLE
            and self.selected_record_fingerprint is not None
        ):
            raise ValueError("ready rows require one admitted selected observation")
        if (
            self.observation_verdict is not None
            and self.readiness_verdict
            is not _SELECTION_TO_READINESS[self.observation_verdict]
        ):
            raise ValueError(
                "readiness verdict must preserve the observation selection verdict"
            )
        if (
            self.observation_verdict is None
            and self.readiness_verdict
            not in {
                S6FormulaMarketReadinessVerdict.BINDING_BLOCKED,
                S6FormulaMarketReadinessVerdict.DEFINITION_FINGERPRINT_MISSING,
            }
        ):
            raise ValueError("observation verdict is required after definition lookup")


@dataclass(frozen=True)
class S6FormulaMarketReadinessAudit:
    metric_id: str
    binding_audit_fingerprint: str
    market_catalog_fingerprint: str
    observation_inventory_fingerprint: str
    instrument_id: str
    trade_date: date
    as_of: datetime
    binding_overall_admissible: bool
    rows: tuple[S6FormulaMarketReadinessRow, ...]
    market_input_count: int
    ready_count: int
    all_direct_market_inputs_ready: bool
    market_gate_ready: bool
    direct_market_inputs_only: bool = True
    financial_observation_readiness_included: bool = False
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
            "market_catalog_fingerprint",
            "observation_inventory_fingerprint",
            "instrument_id",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        _require_utc(self.as_of, "as_of")
        positions = tuple(row.position for row in self.rows)
        if positions != tuple(sorted(set(positions))):
            raise ValueError("rows must retain unique increasing formula positions")
        input_ids = tuple(row.input_id for row in self.rows)
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("market readiness row input IDs must be unique")
        if self.market_input_count != len(self.rows):
            raise ValueError("market_input_count does not match rows")
        if self.ready_count != sum(row.ready for row in self.rows):
            raise ValueError("ready_count does not match rows")
        expected_all_ready = bool(self.rows) and all(row.ready for row in self.rows)
        if self.all_direct_market_inputs_ready != expected_all_ready:
            raise ValueError("all_direct_market_inputs_ready does not match rows")
        expected_gate = self.binding_overall_admissible and expected_all_ready
        if self.market_gate_ready != expected_gate:
            raise ValueError("market_gate_ready does not match binding and rows")
        if (
            not self.direct_market_inputs_only
            or self.financial_observation_readiness_included
            or self.derived_metric_inputs_expanded
            or self.formula_computation_ready
            or self.numeric_values_included
            or self.performance_claim
            or self.portfolio_or_risk_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "market readiness cannot acquire formula, value, risk, or execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_audit_payload(self)),
        )


def audit_s6_formula_market_readiness(
    *,
    binding_audit: S6FormulaInputBindingAudit,
    market_catalog: S6MarketInputCatalog,
    observation_records: Iterable[S6MarketObservationRecord],
    instrument_id: str,
    trade_date: date,
    as_of: datetime,
) -> S6FormulaMarketReadinessAudit:
    """Audit direct market bindings against observations visible by as-of."""

    if binding_audit.market_catalog_fingerprint != market_catalog.fingerprint:
        raise ValueError("binding audit is not bound to the exact market catalog")
    normalized_instrument = instrument_id.strip()
    if not normalized_instrument:
        raise ValueError("instrument_id must be non-empty")
    normalized_as_of = _to_utc(as_of, "as_of")
    inventory = build_s6_market_observation_inventory(observation_records)
    definitions = {
        item.fingerprint: item
        for item in market_catalog.definitions
    }
    rows = tuple(
        _audit_market_row(
            binding_row=row,
            definitions=definitions,
            observation_records=inventory.records,
            instrument_id=normalized_instrument,
            trade_date=trade_date,
            as_of=normalized_as_of,
        )
        for row in binding_audit.rows
        if row.kind is S6FormulaInputKind.MARKET_FIELD
    )
    all_ready = bool(rows) and all(row.ready for row in rows)
    return S6FormulaMarketReadinessAudit(
        metric_id=binding_audit.metric_id,
        binding_audit_fingerprint=binding_audit.fingerprint,
        market_catalog_fingerprint=market_catalog.fingerprint,
        observation_inventory_fingerprint=inventory.fingerprint,
        instrument_id=normalized_instrument,
        trade_date=trade_date,
        as_of=normalized_as_of,
        binding_overall_admissible=binding_audit.overall_admissible,
        rows=rows,
        market_input_count=len(rows),
        ready_count=sum(row.ready for row in rows),
        all_direct_market_inputs_ready=all_ready,
        market_gate_ready=binding_audit.overall_admissible and all_ready,
    )


def _audit_market_row(
    *,
    binding_row: S6FormulaInputBindingRow,
    definitions: dict[str, S6MarketFieldDefinition],
    observation_records: tuple[S6MarketObservationRecord, ...],
    instrument_id: str,
    trade_date: date,
    as_of: datetime,
) -> S6FormulaMarketReadinessRow:
    if not binding_row.admissible:
        return S6FormulaMarketReadinessRow(
            position=binding_row.position,
            input_id=binding_row.input_id,
            semantic_id=binding_row.semantic_id,
            binding_verdict=binding_row.verdict,
            readiness_verdict=S6FormulaMarketReadinessVerdict.BINDING_BLOCKED,
            definition_fingerprint=None,
            observation_verdict=None,
            selected_record_fingerprint=None,
            reason=f"formula_binding_blocked:{binding_row.reason}",
            ready=False,
        )

    definition_fingerprint = binding_row.bound_fingerprint
    if definition_fingerprint is None:
        raise ValueError("admitted market binding must carry a definition fingerprint")
    definition = definitions.get(definition_fingerprint)
    if definition is None:
        return S6FormulaMarketReadinessRow(
            position=binding_row.position,
            input_id=binding_row.input_id,
            semantic_id=binding_row.semantic_id,
            binding_verdict=binding_row.verdict,
            readiness_verdict=(
                S6FormulaMarketReadinessVerdict.DEFINITION_FINGERPRINT_MISSING
            ),
            definition_fingerprint=definition_fingerprint,
            observation_verdict=None,
            selected_record_fingerprint=None,
            reason="bound_definition_fingerprint_missing_from_market_catalog",
            ready=False,
        )

    selection = select_s6_market_observation(
        observation_records,
        definition=definition,
        instrument_id=instrument_id,
        trade_date=trade_date,
        as_of=as_of,
    )
    readiness = _SELECTION_TO_READINESS[selection.verdict]
    return S6FormulaMarketReadinessRow(
        position=binding_row.position,
        input_id=binding_row.input_id,
        semantic_id=binding_row.semantic_id,
        binding_verdict=binding_row.verdict,
        readiness_verdict=readiness,
        definition_fingerprint=definition_fingerprint,
        observation_verdict=selection.verdict,
        selected_record_fingerprint=(
            selection.selected.fingerprint if selection.selected is not None else None
        ),
        reason=selection.reason,
        ready=readiness is S6FormulaMarketReadinessVerdict.READY,
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


def _audit_payload(item: S6FormulaMarketReadinessAudit) -> dict[str, object]:
    return {
        "metric_id": item.metric_id,
        "binding_audit_fingerprint": item.binding_audit_fingerprint,
        "market_catalog_fingerprint": item.market_catalog_fingerprint,
        "observation_inventory_fingerprint": (
            item.observation_inventory_fingerprint
        ),
        "instrument_id": item.instrument_id,
        "trade_date": item.trade_date.isoformat(),
        "as_of": item.as_of.isoformat(),
        "binding_overall_admissible": item.binding_overall_admissible,
        "rows": [
            {
                "position": row.position,
                "input_id": row.input_id,
                "semantic_id": row.semantic_id,
                "binding_verdict": row.binding_verdict.value,
                "readiness_verdict": row.readiness_verdict.value,
                "definition_fingerprint": row.definition_fingerprint,
                "observation_verdict": (
                    row.observation_verdict.value
                    if row.observation_verdict is not None
                    else None
                ),
                "selected_record_fingerprint": row.selected_record_fingerprint,
                "reason": row.reason,
                "ready": row.ready,
            }
            for row in item.rows
        ],
        "market_input_count": item.market_input_count,
        "ready_count": item.ready_count,
        "all_direct_market_inputs_ready": item.all_direct_market_inputs_ready,
        "market_gate_ready": item.market_gate_ready,
        "direct_market_inputs_only": item.direct_market_inputs_only,
        "financial_observation_readiness_included": (
            item.financial_observation_readiness_included
        ),
        "derived_metric_inputs_expanded": item.derived_metric_inputs_expanded,
        "formula_computation_ready": item.formula_computation_ready,
        "numeric_values_included": item.numeric_values_included,
        "performance_claim": item.performance_claim,
        "portfolio_or_risk_authority": item.portfolio_or_risk_authority,
        "broker_order_authority": item.broker_order_authority,
    }
