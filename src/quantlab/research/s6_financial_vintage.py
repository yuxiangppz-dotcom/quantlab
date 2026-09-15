"""Provider-neutral point-in-time financial-vintage inventory contract for S6."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint

_SCHEMA = "quantlab_s6_financial_vintage_inventory_v1"


class S6FinancialStatement(StrEnum):
    """Supported financial statement families."""

    BALANCE_SHEET = "balance_sheet"
    INCOME_STATEMENT = "income_statement"
    CASH_FLOW_STATEMENT = "cash_flow_statement"


class S6FiscalReportKind(StrEnum):
    """A-share fiscal report periods used by the S6 contract."""

    Q1 = "q1"
    H1 = "h1"
    Q3 = "q3"
    FY = "fy"


class S6FinancialVintageStatus(StrEnum):
    """Evidence strength of a provider's historical financial vintage."""

    ORIGINAL_VINTAGE_VERIFIED = "original_vintage_verified"
    REVISION_CHAIN_VERIFIED = "revision_chain_verified"
    LATEST_ONLY_UNVERIFIED = "latest_only_unverified"
    UNKNOWN = "unknown"


class S6FinancialSelectionVerdict(StrEnum):
    """Fail-closed result of a source-time PIT lookup."""

    ADMISSIBLE = "admissible"
    NOT_YET_AVAILABLE = "not_yet_available"
    UNVERIFIED_VINTAGE = "unverified_vintage"
    MISSING = "missing"


_VERIFIED_STATUSES = frozenset(
    {
        S6FinancialVintageStatus.ORIGINAL_VINTAGE_VERIFIED,
        S6FinancialVintageStatus.REVISION_CHAIN_VERIFIED,
    }
)
_PERIOD_ENDS = {
    S6FiscalReportKind.Q1: (3, 31),
    S6FiscalReportKind.H1: (6, 30),
    S6FiscalReportKind.Q3: (9, 30),
    S6FiscalReportKind.FY: (12, 31),
}


@dataclass(frozen=True)
class S6FinancialVintageRecord:
    """Immutable metadata for one provider/source financial-statement vintage."""

    instrument_id: str
    fiscal_period_end: date
    report_kind: S6FiscalReportKind
    statement: S6FinancialStatement
    provider_id: str
    source_id: str
    revision_id: str
    announced_at: datetime
    available_at: datetime
    retrieved_at: datetime
    raw_field_ids: tuple[str, ...]
    content_fingerprint: str
    status: S6FinancialVintageStatus
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "instrument_id",
            "provider_id",
            "source_id",
            "revision_id",
            "content_fingerprint",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        expected_month_day = _PERIOD_ENDS[self.report_kind]
        if (
            self.fiscal_period_end.month,
            self.fiscal_period_end.day,
        ) != expected_month_day:
            raise ValueError("fiscal_period_end does not align with report_kind")
        for name in ("announced_at", "available_at", "retrieved_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            if value.utcoffset() != UTC.utcoffset(value):
                raise ValueError(f"{name} must be normalized to UTC")
        if self.announced_at.date() < self.fiscal_period_end:
            raise ValueError("announced_at cannot precede fiscal_period_end")
        if self.available_at < self.announced_at:
            raise ValueError("available_at cannot precede announced_at")
        if self.retrieved_at < self.available_at:
            raise ValueError("retrieved_at cannot precede available_at")
        if not self.raw_field_ids:
            raise ValueError("raw_field_ids must be non-empty")
        if any(not item or not item.strip() for item in self.raw_field_ids):
            raise ValueError("raw_field_ids must contain non-empty identifiers")
        if self.raw_field_ids != tuple(sorted(set(self.raw_field_ids))):
            raise ValueError("raw_field_ids must be sorted and unique")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_record_payload(self)),
        )

    @property
    def identity(self) -> tuple[str, date, S6FinancialStatement, str, str, str]:
        return (
            self.instrument_id,
            self.fiscal_period_end,
            self.statement,
            self.provider_id,
            self.source_id,
            self.revision_id,
        )

    @property
    def release_identity(
        self,
    ) -> tuple[str, date, S6FinancialStatement, str, str, datetime]:
        return (
            self.instrument_id,
            self.fiscal_period_end,
            self.statement,
            self.provider_id,
            self.source_id,
            self.available_at,
        )


@dataclass(frozen=True)
class S6FinancialVintageStatusCount:
    status: S6FinancialVintageStatus
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("status count cannot be negative")


@dataclass(frozen=True)
class S6FinancialVintageInventory:
    """Deterministic inventory that retains weak and unknown evidence."""

    schema: str
    records: tuple[S6FinancialVintageRecord, ...]
    status_counts: tuple[S6FinancialVintageStatusCount, ...]
    earliest_available_at: datetime | None
    latest_available_at: datetime | None
    source_time_pit_only: bool = True
    historical_local_knowledge_proven: bool = False
    numeric_values_included: bool = False
    factor_scores_included: bool = False
    performance_claim: bool = False
    promotion_authority: bool = False
    account_mutation_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if tuple(item.status for item in self.status_counts) != tuple(
            S6FinancialVintageStatus
        ):
            raise ValueError("status_counts must cover every status in frozen order")
        if sum(item.count for item in self.status_counts) != len(self.records):
            raise ValueError("status_counts do not match records")
        if (
            not self.source_time_pit_only
            or self.historical_local_knowledge_proven
            or self.numeric_values_included
            or self.factor_scores_included
            or self.performance_claim
            or self.promotion_authority
            or self.account_mutation_authority
            or self.broker_order_authority
        ):
            raise ValueError("inventory cannot acquire value, performance, or execution authority")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_inventory_payload(self)),
        )


@dataclass(frozen=True)
class S6FinancialVintageSelection:
    """Auditable source-time result; never proof of historical local knowledge."""

    instrument_id: str
    fiscal_period_end: date
    statement: S6FinancialStatement
    as_of: datetime
    verdict: S6FinancialSelectionVerdict
    selected: S6FinancialVintageRecord | None
    reason: str
    source_time_pit_only: bool = True
    historical_local_knowledge_proven: bool = False
    numeric_value_authority: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False

    def __post_init__(self) -> None:
        if not self.instrument_id or not self.instrument_id.strip():
            raise ValueError("instrument_id must be non-empty")
        if not self.reason or not self.reason.strip():
            raise ValueError("reason must be non-empty")
        _require_utc(self.as_of, "as_of")
        if (self.verdict is S6FinancialSelectionVerdict.ADMISSIBLE) != (
            self.selected is not None
        ):
            raise ValueError("only an admissible selection may contain a record")
        if (
            not self.source_time_pit_only
            or self.historical_local_knowledge_proven
            or self.numeric_value_authority
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError("selection cannot acquire local-knowledge or execution authority")


def make_s6_financial_vintage_record(
    *,
    instrument_id: str,
    fiscal_period_end: date,
    report_kind: S6FiscalReportKind,
    statement: S6FinancialStatement,
    provider_id: str,
    source_id: str,
    revision_id: str,
    announced_at: datetime,
    available_at: datetime,
    retrieved_at: datetime,
    raw_field_ids: Iterable[str],
    content_fingerprint: str,
    status: S6FinancialVintageStatus,
) -> S6FinancialVintageRecord:
    """Normalize one record without weakening its evidence status."""

    fields = tuple(sorted(item.strip() for item in raw_field_ids))
    if len(fields) != len(set(fields)):
        raise ValueError("raw_field_ids must be unique")
    return S6FinancialVintageRecord(
        instrument_id=instrument_id.strip(),
        fiscal_period_end=fiscal_period_end,
        report_kind=report_kind,
        statement=statement,
        provider_id=provider_id.strip(),
        source_id=source_id.strip(),
        revision_id=revision_id.strip(),
        announced_at=_to_utc(announced_at, "announced_at"),
        available_at=_to_utc(available_at, "available_at"),
        retrieved_at=_to_utc(retrieved_at, "retrieved_at"),
        raw_field_ids=fields,
        content_fingerprint=content_fingerprint.strip(),
        status=status,
    )


def build_s6_financial_vintage_inventory(
    records: Iterable[S6FinancialVintageRecord],
) -> S6FinancialVintageInventory:
    """Build a stable inventory and reject ambiguous vintage identities."""

    ordered = tuple(sorted(records, key=_record_sort_key))
    _validate_record_set(ordered)
    availability = tuple(item.available_at for item in ordered)
    counts = tuple(
        S6FinancialVintageStatusCount(
            status=status,
            count=sum(item.status is status for item in ordered),
        )
        for status in S6FinancialVintageStatus
    )
    return S6FinancialVintageInventory(
        schema=_SCHEMA,
        records=ordered,
        status_counts=counts,
        earliest_available_at=min(availability, default=None),
        latest_available_at=max(availability, default=None),
    )


def select_s6_financial_vintage(
    records: Iterable[S6FinancialVintageRecord],
    *,
    instrument_id: str,
    fiscal_period_end: date,
    statement: S6FinancialStatement,
    as_of: datetime,
) -> S6FinancialVintageSelection:
    """Select the latest verified vintage published by the as-of time."""

    normalized_as_of = _to_utc(as_of, "as_of")
    inventory = build_s6_financial_vintage_inventory(records)
    candidates = tuple(
        item
        for item in inventory.records
        if item.instrument_id == instrument_id.strip()
        and item.fiscal_period_end == fiscal_period_end
        and item.statement is statement
    )
    verified_visible = tuple(
        item
        for item in candidates
        if item.status in _VERIFIED_STATUSES and item.available_at <= normalized_as_of
    )
    if verified_visible:
        return S6FinancialVintageSelection(
            instrument_id=instrument_id.strip(),
            fiscal_period_end=fiscal_period_end,
            statement=statement,
            as_of=normalized_as_of,
            verdict=S6FinancialSelectionVerdict.ADMISSIBLE,
            selected=max(verified_visible, key=lambda item: item.available_at),
            reason="latest_verified_source_vintage_available_by_as_of",
        )

    weak_visible = any(
        item.available_at <= normalized_as_of and item.status not in _VERIFIED_STATUSES
        for item in candidates
    )
    if weak_visible:
        verdict = S6FinancialSelectionVerdict.UNVERIFIED_VINTAGE
        reason = "only_latest_only_unverified_or_unknown_vintages_are_visible"
    elif any(
        item.status in _VERIFIED_STATUSES and item.available_at > normalized_as_of
        for item in candidates
    ):
        verdict = S6FinancialSelectionVerdict.NOT_YET_AVAILABLE
        reason = "verified_vintage_exists_but_was_not_available_by_as_of"
    else:
        verdict = S6FinancialSelectionVerdict.MISSING
        reason = "no_matching_financial_vintage"
    return S6FinancialVintageSelection(
        instrument_id=instrument_id.strip(),
        fiscal_period_end=fiscal_period_end,
        statement=statement,
        as_of=normalized_as_of,
        verdict=verdict,
        selected=None,
        reason=reason,
    )


def _validate_record_set(records: tuple[S6FinancialVintageRecord, ...]) -> None:
    identities: set[tuple[object, ...]] = set()
    releases: set[tuple[object, ...]] = set()
    for item in records:
        if item.identity in identities:
            raise ValueError("duplicate financial vintage identity")
        identities.add(item.identity)
        if item.release_identity in releases:
            raise ValueError("conflicting financial vintages share one availability time")
        releases.add(item.release_identity)


def _to_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{name} must be normalized to UTC")


def _record_sort_key(item: S6FinancialVintageRecord) -> tuple[object, ...]:
    return (
        item.instrument_id,
        item.fiscal_period_end,
        item.statement.value,
        item.provider_id,
        item.source_id,
        item.available_at,
        item.revision_id,
    )


def _record_payload(item: S6FinancialVintageRecord) -> dict[str, object]:
    return {
        "instrument_id": item.instrument_id,
        "fiscal_period_end": item.fiscal_period_end.isoformat(),
        "report_kind": item.report_kind.value,
        "statement": item.statement.value,
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "revision_id": item.revision_id,
        "announced_at": item.announced_at.isoformat(),
        "available_at": item.available_at.isoformat(),
        "retrieved_at": item.retrieved_at.isoformat(),
        "raw_field_ids": list(item.raw_field_ids),
        "content_fingerprint": item.content_fingerprint,
        "status": item.status.value,
    }


def _inventory_payload(item: S6FinancialVintageInventory) -> dict[str, object]:
    return {
        "schema": item.schema,
        "record_fingerprints": [record.fingerprint for record in item.records],
        "status_counts": [
            {"status": count.status.value, "count": count.count}
            for count in item.status_counts
        ],
        "earliest_available_at": (
            item.earliest_available_at.isoformat()
            if item.earliest_available_at is not None
            else None
        ),
        "latest_available_at": (
            item.latest_available_at.isoformat()
            if item.latest_available_at is not None
            else None
        ),
        "source_time_pit_only": item.source_time_pit_only,
        "historical_local_knowledge_proven": item.historical_local_knowledge_proven,
        "numeric_values_included": item.numeric_values_included,
        "factor_scores_included": item.factor_scores_included,
        "performance_claim": item.performance_claim,
        "promotion_authority": item.promotion_authority,
        "account_mutation_authority": item.account_mutation_authority,
        "broker_order_authority": item.broker_order_authority,
    }
