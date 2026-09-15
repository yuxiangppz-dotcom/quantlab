"""Integrate S6 financial and market PIT gates before formula materialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_formula_financial_readiness import (
    S6FormulaFinancialReadinessAudit,
)
from quantlab.research.s6_formula_input_binding import S6FormulaInputBindingAudit
from quantlab.research.s6_formula_market_readiness import (
    S6FormulaMarketReadinessAudit,
)
from quantlab.research.s6_formula_spec import S6FormulaInputKind


@dataclass(frozen=True)
class S6FormulaReadinessPreflight:
    metric_id: str
    binding_audit_fingerprint: str
    financial_readiness_fingerprint: str
    market_readiness_fingerprint: str
    instrument_id: str
    fiscal_period_end: date
    trade_date: date
    as_of: datetime
    binding_overall_admissible: bool
    binding_input_count: int
    financial_input_count: int
    market_input_count: int
    derived_input_count: int
    child_input_coverage_complete: bool
    all_direct_inputs_ready: bool
    derived_inputs_expanded: bool
    metadata_preflight_ready: bool
    numeric_values_included: bool = False
    formula_computation_ready: bool = False
    performance_claim: bool = False
    portfolio_or_risk_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "metric_id",
            "binding_audit_fingerprint",
            "financial_readiness_fingerprint",
            "market_readiness_fingerprint",
            "instrument_id",
        ):
            value = getattr(self, name)
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        _require_utc(self.as_of, "as_of")
        counts = (
            self.binding_input_count,
            self.financial_input_count,
            self.market_input_count,
            self.derived_input_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("input counts cannot be negative")
        expected_coverage = self.binding_input_count == sum(counts[1:])
        if self.child_input_coverage_complete != expected_coverage:
            raise ValueError("child_input_coverage_complete does not match counts")
        if self.derived_inputs_expanded:
            raise ValueError("preflight cannot claim derived inputs were expanded")
        expected_ready = (
            self.binding_overall_admissible
            and self.child_input_coverage_complete
            and bool(self.financial_input_count + self.market_input_count)
            and self.all_direct_inputs_ready
            and not self.derived_input_count
        )
        if self.metadata_preflight_ready != expected_ready:
            raise ValueError("metadata readiness contradicts input state")
        if (
            self.numeric_values_included
            or self.formula_computation_ready
            or self.performance_claim
            or self.portfolio_or_risk_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "readiness preflight cannot acquire value, performance, risk, "
                "account, or execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_preflight_payload(self)),
        )


def build_s6_formula_readiness_preflight(
    *,
    binding_audit: S6FormulaInputBindingAudit,
    financial_readiness: S6FormulaFinancialReadinessAudit,
    market_readiness: S6FormulaMarketReadinessAudit,
) -> S6FormulaReadinessPreflight:
    """Require exact, co-temporal child audits for every direct formula input."""

    if financial_readiness.binding_audit_fingerprint != binding_audit.fingerprint:
        raise ValueError("financial readiness is not bound to the exact binding audit")
    if market_readiness.binding_audit_fingerprint != binding_audit.fingerprint:
        raise ValueError("market readiness is not bound to the exact binding audit")
    if (
        financial_readiness.metric_id != binding_audit.metric_id
        or market_readiness.metric_id != binding_audit.metric_id
    ):
        raise ValueError("child readiness metric identity does not match binding audit")
    if financial_readiness.instrument_id != market_readiness.instrument_id:
        raise ValueError("child readiness instrument targets do not match")
    if financial_readiness.as_of != market_readiness.as_of:
        raise ValueError("child readiness as-of targets do not match")

    financial_inputs = tuple(
        row
        for row in binding_audit.rows
        if row.kind is S6FormulaInputKind.FINANCIAL_FIELD
    )
    market_inputs = tuple(
        row
        for row in binding_audit.rows
        if row.kind is S6FormulaInputKind.MARKET_FIELD
    )
    derived_inputs = tuple(
        row
        for row in binding_audit.rows
        if row.kind is S6FormulaInputKind.DERIVED_METRIC
    )
    financial_coverage = _row_identities(financial_readiness.rows) == (
        _row_identities(financial_inputs)
    )
    market_coverage = _row_identities(market_readiness.rows) == (
        _row_identities(market_inputs)
    )
    coverage = financial_coverage and market_coverage
    direct_count = len(financial_inputs) + len(market_inputs)
    financial_ready = (
        not financial_inputs
        or financial_readiness.all_direct_financial_inputs_ready
    )
    market_ready = not market_inputs or market_readiness.all_direct_market_inputs_ready
    all_direct_ready = bool(direct_count) and financial_ready and market_ready
    metadata_ready = (
        binding_audit.overall_admissible
        and coverage
        and all_direct_ready
        and not derived_inputs
    )
    return S6FormulaReadinessPreflight(
        metric_id=binding_audit.metric_id,
        binding_audit_fingerprint=binding_audit.fingerprint,
        financial_readiness_fingerprint=financial_readiness.fingerprint,
        market_readiness_fingerprint=market_readiness.fingerprint,
        instrument_id=financial_readiness.instrument_id,
        fiscal_period_end=financial_readiness.fiscal_period_end,
        trade_date=market_readiness.trade_date,
        as_of=financial_readiness.as_of,
        binding_overall_admissible=binding_audit.overall_admissible,
        binding_input_count=len(binding_audit.rows),
        financial_input_count=len(financial_inputs),
        market_input_count=len(market_inputs),
        derived_input_count=len(derived_inputs),
        child_input_coverage_complete=coverage,
        all_direct_inputs_ready=all_direct_ready,
        derived_inputs_expanded=False,
        metadata_preflight_ready=metadata_ready,
    )


def _row_identities(rows: tuple[object, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple((row.position, row.input_id, row.semantic_id) for row in rows)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be normalized to UTC")


def _preflight_payload(item: S6FormulaReadinessPreflight) -> dict[str, object]:
    return {
        "metric_id": item.metric_id,
        "binding_audit_fingerprint": item.binding_audit_fingerprint,
        "financial_readiness_fingerprint": item.financial_readiness_fingerprint,
        "market_readiness_fingerprint": item.market_readiness_fingerprint,
        "instrument_id": item.instrument_id,
        "fiscal_period_end": item.fiscal_period_end.isoformat(),
        "trade_date": item.trade_date.isoformat(),
        "as_of": item.as_of.isoformat(),
        "binding_overall_admissible": item.binding_overall_admissible,
        "binding_input_count": item.binding_input_count,
        "financial_input_count": item.financial_input_count,
        "market_input_count": item.market_input_count,
        "derived_input_count": item.derived_input_count,
        "child_input_coverage_complete": item.child_input_coverage_complete,
        "all_direct_inputs_ready": item.all_direct_inputs_ready,
        "derived_inputs_expanded": item.derived_inputs_expanded,
        "metadata_preflight_ready": item.metadata_preflight_ready,
        "numeric_values_included": item.numeric_values_included,
        "formula_computation_ready": item.formula_computation_ready,
        "performance_claim": item.performance_claim,
        "portfolio_or_risk_authority": item.portfolio_or_risk_authority,
        "account_mutation_authority": item.account_mutation_authority,
        "broker_order_authority": item.broker_order_authority,
    }
