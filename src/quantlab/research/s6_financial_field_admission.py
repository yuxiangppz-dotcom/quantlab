"""Provider-neutral semantic admission for raw S6 financial fields."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_financial_vintage import (
    S6FinancialStatement,
    S6FinancialVintageRecord,
    S6FinancialVintageStatus,
)

_SCHEMA = "quantlab_s6_financial_field_catalog_v1"
_VERIFIED_VINTAGES = frozenset(
    {
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
        S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED,
    }
)


class S6FinancialValueNature(StrEnum):
    POINT_IN_TIME = "point_in_time"
    PERIOD_FLOW = "period_flow"


class S6FinancialPeriodBasis(StrEnum):
    INSTANT = "instant"
    YEAR_TO_DATE = "year_to_date"
    SINGLE_PERIOD = "single_period"


class S6FinancialConsolidationScope(StrEnum):
    CONSOLIDATED = "consolidated"
    PARENT_ONLY = "parent_only"
    UNKNOWN = "unknown"


class S6FinancialUnitKind(StrEnum):
    CURRENCY = "currency"
    SHARES = "shares"
    RATIO = "ratio"
    COUNT = "count"


class S6FinancialSignConvention(StrEnum):
    AS_REPORTED_DOCUMENTED = "as_reported_documented"
    POSITIVE_MAGNITUDE = "positive_magnitude"
    UNKNOWN = "unknown"


class S6FinancialFieldEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    PROVIDER_DOCUMENTED_UNVERIFIED = "provider_documented_unverified"
    UNKNOWN = "unknown"


class S6FinancialFieldAdmissionVerdict(StrEnum):
    ADMISSIBLE = "admissible"
    UNVERIFIED_DEFINITION = "unverified_definition"
    MISSING_DEFINITION = "missing_definition"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True)
class S6FinancialFieldDefinition:
    provider_id: str
    source_id: str
    raw_field_id: str
    semantic_field_id: str
    statement: S6FinancialStatement
    value_nature: S6FinancialValueNature
    period_basis: S6FinancialPeriodBasis
    consolidation_scope: S6FinancialConsolidationScope
    unit_kind: S6FinancialUnitKind
    currency: str | None
    sign_convention: S6FinancialSignConvention
    evidence_status: S6FinancialFieldEvidenceStatus
    documentation_fingerprint: str | None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("provider_id", "source_id", "raw_field_id", "semantic_field_id"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        if self.value_nature is S6FinancialValueNature.POINT_IN_TIME:
            if self.period_basis is not S6FinancialPeriodBasis.INSTANT:
                raise ValueError("point-in-time fields require instant period basis")
        elif self.period_basis is S6FinancialPeriodBasis.INSTANT:
            raise ValueError("period-flow fields cannot use instant period basis")
        if self.unit_kind is S6FinancialUnitKind.CURRENCY:
            if (
                self.currency is None
                or len(self.currency) != 3
                or not self.currency.isascii()
                or not self.currency.isalpha()
                or not self.currency.isupper()
            ):
                raise ValueError("currency fields require an uppercase ISO-like code")
        elif self.currency is not None:
            raise ValueError("non-currency fields cannot declare currency")
        if self.documentation_fingerprint is not None and (
            not self.documentation_fingerprint.strip()
            or self.documentation_fingerprint != self.documentation_fingerprint.strip()
        ):
            raise ValueError("documentation_fingerprint must be normalized when present")
        if (
            self.evidence_status is S6FinancialFieldEvidenceStatus.VERIFIED
            and self.documentation_fingerprint is None
        ):
            raise ValueError("verified definitions require documentation evidence")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_definition_payload(self)),
        )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.provider_id, self.source_id, self.raw_field_id)

    @property
    def semantically_admissible(self) -> bool:
        return (
            self.evidence_status is S6FinancialFieldEvidenceStatus.VERIFIED
            and self.consolidation_scope
            is not S6FinancialConsolidationScope.UNKNOWN
            and self.sign_convention is not S6FinancialSignConvention.UNKNOWN
        )


@dataclass(frozen=True)
class S6FinancialFieldCatalog:
    schema: str
    definitions: tuple[S6FinancialFieldDefinition, ...]
    verified_count: int
    unverified_count: int
    unknown_count: int
    numeric_values_included: bool = False
    factor_formulas_included: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.definitions != tuple(sorted(self.definitions, key=lambda item: item.identity)):
            raise ValueError("definitions must be in deterministic identity order")
        identities = tuple(item.identity for item in self.definitions)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate financial field identity")
        expected = (
            sum(
                item.evidence_status is S6FinancialFieldEvidenceStatus.VERIFIED
                for item in self.definitions
            ),
            sum(
                item.evidence_status
                is S6FinancialFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
                for item in self.definitions
            ),
            sum(
                item.evidence_status is S6FinancialFieldEvidenceStatus.UNKNOWN
                for item in self.definitions
            ),
        )
        if (self.verified_count, self.unverified_count, self.unknown_count) != expected:
            raise ValueError("catalog evidence counts do not match definitions")
        if (
            self.numeric_values_included
            or self.factor_formulas_included
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("catalog cannot acquire numeric, factor, or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_catalog_payload(self)),
        )


@dataclass(frozen=True)
class S6FinancialFieldAdmissionRow:
    raw_field_id: str
    verdict: S6FinancialFieldAdmissionVerdict
    definition_fingerprint: str | None
    reason: str
    admissible: bool

    def __post_init__(self) -> None:
        if not self.raw_field_id or not self.reason:
            raise ValueError("admission row identity and reason must be non-empty")
        if self.admissible != (
            self.verdict is S6FinancialFieldAdmissionVerdict.ADMISSIBLE
        ):
            raise ValueError("row admissibility must match verdict")
        if self.admissible != (self.definition_fingerprint is not None):
            raise ValueError("only admitted rows may bind a definition fingerprint")


@dataclass(frozen=True)
class S6FinancialFieldAdmissionAudit:
    record_fingerprint: str
    catalog_fingerprint: str
    rows: tuple[S6FinancialFieldAdmissionRow, ...]
    vintage_verified: bool
    all_fields_admissible: bool
    overall_admissible: bool
    source_time_pit_only: bool = True
    numeric_values_included: bool = False
    factor_computation_authority: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if tuple(item.raw_field_id for item in self.rows) != tuple(
            sorted(item.raw_field_id for item in self.rows)
        ):
            raise ValueError("admission rows must be in raw-field order")
        if self.all_fields_admissible != all(item.admissible for item in self.rows):
            raise ValueError("all_fields_admissible does not match rows")
        if self.overall_admissible != (
            self.vintage_verified and self.all_fields_admissible
        ):
            raise ValueError("overall_admissible does not match evidence")
        if (
            not self.source_time_pit_only
            or self.numeric_values_included
            or self.factor_computation_authority
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("audit cannot acquire numeric, factor, or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_audit_payload(self)),
        )


def build_s6_financial_field_catalog(
    definitions: Iterable[S6FinancialFieldDefinition],
) -> S6FinancialFieldCatalog:
    ordered = tuple(sorted(definitions, key=lambda item: item.identity))
    return S6FinancialFieldCatalog(
        schema=_SCHEMA,
        definitions=ordered,
        verified_count=sum(
            item.evidence_status is S6FinancialFieldEvidenceStatus.VERIFIED
            for item in ordered
        ),
        unverified_count=sum(
            item.evidence_status
            is S6FinancialFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
            for item in ordered
        ),
        unknown_count=sum(
            item.evidence_status is S6FinancialFieldEvidenceStatus.UNKNOWN
            for item in ordered
        ),
    )


def audit_s6_financial_field_admission(
    *,
    record: S6FinancialVintageRecord,
    catalog: S6FinancialFieldCatalog,
) -> S6FinancialFieldAdmissionAudit:
    exact = {item.identity: item for item in catalog.definitions}
    raw_elsewhere = {
        item.raw_field_id
        for item in catalog.definitions
        if (item.provider_id, item.source_id)
        != (record.provider_id, record.source_id)
    }
    rows = tuple(
        _audit_field(
            record=record,
            raw_field_id=raw_field_id,
            exact=exact,
            raw_elsewhere=raw_elsewhere,
        )
        for raw_field_id in record.raw_field_ids
    )
    vintage_verified = record.status in _VERIFIED_VINTAGES
    all_fields_admissible = all(item.admissible for item in rows)
    return S6FinancialFieldAdmissionAudit(
        record_fingerprint=record.fingerprint,
        catalog_fingerprint=catalog.fingerprint,
        rows=rows,
        vintage_verified=vintage_verified,
        all_fields_admissible=all_fields_admissible,
        overall_admissible=vintage_verified and all_fields_admissible,
    )


def _audit_field(
    *,
    record: S6FinancialVintageRecord,
    raw_field_id: str,
    exact: dict[tuple[str, str, str], S6FinancialFieldDefinition],
    raw_elsewhere: set[str],
) -> S6FinancialFieldAdmissionRow:
    definition = exact.get((record.provider_id, record.source_id, raw_field_id))
    if definition is None:
        if raw_field_id in raw_elsewhere:
            verdict = S6FinancialFieldAdmissionVerdict.IDENTITY_MISMATCH
            reason = "raw_field_definition_exists_only_for_another_provider_or_source"
        else:
            verdict = S6FinancialFieldAdmissionVerdict.MISSING_DEFINITION
            reason = "raw_field_definition_is_missing"
        return S6FinancialFieldAdmissionRow(
            raw_field_id=raw_field_id,
            verdict=verdict,
            definition_fingerprint=None,
            reason=reason,
            admissible=False,
        )
    if definition.statement is not record.statement:
        return S6FinancialFieldAdmissionRow(
            raw_field_id=raw_field_id,
            verdict=S6FinancialFieldAdmissionVerdict.IDENTITY_MISMATCH,
            definition_fingerprint=None,
            reason="field_statement_does_not_match_vintage_statement",
            admissible=False,
        )
    if not definition.semantically_admissible:
        return S6FinancialFieldAdmissionRow(
            raw_field_id=raw_field_id,
            verdict=S6FinancialFieldAdmissionVerdict.UNVERIFIED_DEFINITION,
            definition_fingerprint=None,
            reason="field_semantics_are_unverified_or_unknown",
            admissible=False,
        )
    return S6FinancialFieldAdmissionRow(
        raw_field_id=raw_field_id,
        verdict=S6FinancialFieldAdmissionVerdict.ADMISSIBLE,
        definition_fingerprint=definition.fingerprint,
        reason="verified_complete_field_semantics",
        admissible=True,
    )


def _definition_payload(item: S6FinancialFieldDefinition) -> dict[str, object]:
    return {
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "raw_field_id": item.raw_field_id,
        "semantic_field_id": item.semantic_field_id,
        "statement": item.statement.value,
        "value_nature": item.value_nature.value,
        "period_basis": item.period_basis.value,
        "consolidation_scope": item.consolidation_scope.value,
        "unit_kind": item.unit_kind.value,
        "currency": item.currency,
        "sign_convention": item.sign_convention.value,
        "evidence_status": item.evidence_status.value,
        "documentation_fingerprint": item.documentation_fingerprint,
    }


def _catalog_payload(item: S6FinancialFieldCatalog) -> dict[str, object]:
    return {
        "schema": item.schema,
        "definition_fingerprints": [
            definition.fingerprint for definition in item.definitions
        ],
        "verified_count": item.verified_count,
        "unverified_count": item.unverified_count,
        "unknown_count": item.unknown_count,
        "numeric_values_included": item.numeric_values_included,
        "factor_formulas_included": item.factor_formulas_included,
        "performance_claim": item.performance_claim,
        "broker_order_authority": item.broker_order_authority,
    }


def _audit_payload(item: S6FinancialFieldAdmissionAudit) -> dict[str, object]:
    return {
        "record_fingerprint": item.record_fingerprint,
        "catalog_fingerprint": item.catalog_fingerprint,
        "rows": [
            {
                "raw_field_id": row.raw_field_id,
                "verdict": row.verdict.value,
                "definition_fingerprint": row.definition_fingerprint,
                "reason": row.reason,
                "admissible": row.admissible,
            }
            for row in item.rows
        ],
        "vintage_verified": item.vintage_verified,
        "all_fields_admissible": item.all_fields_admissible,
        "overall_admissible": item.overall_admissible,
        "source_time_pit_only": item.source_time_pit_only,
        "numeric_values_included": item.numeric_values_included,
        "factor_computation_authority": item.factor_computation_authority,
        "performance_claim": item.performance_claim,
        "broker_order_authority": item.broker_order_authority,
    }
