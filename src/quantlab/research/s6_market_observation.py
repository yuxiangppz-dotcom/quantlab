"""Metadata-only point-in-time observation contract for S6 market inputs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint
from quantlab.research.s6_market_input_admission import S6MarketFieldDefinition

_SCHEMA = "quantlab_s6_market_observation_inventory_v1"


class S6MarketObservationStatus(StrEnum):
    ORIGINAL_OBSERVATION_VERIFIED = "original_observation_verified"
    CORRECTION_CHAIN_VERIFIED = "correction_chain_verified"
    LATEST_ONLY_UNVERIFIED = "latest_only_unverified"
    UNKNOWN = "unknown"


class S6MarketObservationSelectionVerdict(StrEnum):
    ADMISSIBLE = "admissible"
    DEFINITION_NOT_ADMISSIBLE = "definition_not_admissible"
    DEFINITION_MISMATCH = "definition_mismatch"
    NOT_YET_AVAILABLE = "not_yet_available"
    UNVERIFIED_OBSERVATION = "unverified_observation"
    MISSING = "missing"


_VERIFIED_STATUSES = frozenset(
    {
        S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED,
        S6MarketObservationStatus.CORRECTION_CHAIN_VERIFIED,
    }
)


@dataclass(frozen=True)
class S6MarketObservationRecord:
    """Immutable metadata for one market-field observation revision."""

    instrument_id: str
    trade_date: date
    provider_id: str
    source_id: str
    raw_field_id: str
    definition_fingerprint: str
    revision_id: str
    observed_at: datetime
    available_at: datetime
    retrieved_at: datetime
    content_fingerprint: str
    status: S6MarketObservationStatus
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "instrument_id",
            "provider_id",
            "source_id",
            "raw_field_id",
            "definition_fingerprint",
            "revision_id",
            "content_fingerprint",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        for name in ("observed_at", "available_at", "retrieved_at"):
            _require_utc(getattr(self, name), name)
        if self.available_at < self.observed_at:
            raise ValueError("available_at cannot precede observed_at")
        if self.retrieved_at < self.available_at:
            raise ValueError("retrieved_at cannot precede available_at")
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_record_payload(self)),
        )

    @property
    def identity(self) -> tuple[str, date, str, str, str, str]:
        return (
            self.instrument_id,
            self.trade_date,
            self.provider_id,
            self.source_id,
            self.raw_field_id,
            self.revision_id,
        )

    @property
    def release_identity(self) -> tuple[str, date, str, str, str, datetime]:
        return (
            self.instrument_id,
            self.trade_date,
            self.provider_id,
            self.source_id,
            self.raw_field_id,
            self.available_at,
        )

    @property
    def target_identity(self) -> tuple[str, date, str, str, str]:
        return (
            self.instrument_id,
            self.trade_date,
            self.provider_id,
            self.source_id,
            self.raw_field_id,
        )


@dataclass(frozen=True)
class S6MarketObservationStatusCount:
    status: S6MarketObservationStatus
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("status count cannot be negative")


@dataclass(frozen=True)
class S6MarketObservationInventory:
    """Deterministic inventory retaining weak and unknown observations."""

    schema: str
    records: tuple[S6MarketObservationRecord, ...]
    status_counts: tuple[S6MarketObservationStatusCount, ...]
    earliest_available_at: datetime | None
    latest_available_at: datetime | None
    source_time_pit_only: bool = True
    historical_local_knowledge_proven: bool = False
    numeric_values_included: bool = False
    formula_computation_authority: bool = False
    performance_claim: bool = False
    portfolio_or_risk_authority: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if tuple(item.status for item in self.status_counts) != tuple(
            S6MarketObservationStatus
        ):
            raise ValueError("status_counts must cover every status in frozen order")
        if self.records != tuple(sorted(self.records, key=_record_sort_key)):
            raise ValueError("records must be in deterministic frozen order")
        _validate_record_set(self.records)
        expected_counts = tuple(
            sum(record.status is status for record in self.records)
            for status in S6MarketObservationStatus
        )
        if tuple(item.count for item in self.status_counts) != expected_counts:
            raise ValueError("status_counts do not match records")
        availability = tuple(record.available_at for record in self.records)
        if self.earliest_available_at != min(availability, default=None):
            raise ValueError("earliest_available_at does not match records")
        if self.latest_available_at != max(availability, default=None):
            raise ValueError("latest_available_at does not match records")
        if (
            not self.source_time_pit_only
            or self.historical_local_knowledge_proven
            or self.numeric_values_included
            or self.formula_computation_authority
            or self.performance_claim
            or self.portfolio_or_risk_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "inventory cannot acquire value, performance, risk, or execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_inventory_payload(self)),
        )


@dataclass(frozen=True)
class S6MarketObservationSelection:
    """Auditable source-time result for one exact admitted market definition."""

    instrument_id: str
    trade_date: date
    provider_id: str
    source_id: str
    raw_field_id: str
    definition_fingerprint: str
    as_of: datetime
    verdict: S6MarketObservationSelectionVerdict
    selected: S6MarketObservationRecord | None
    reason: str
    source_time_pit_only: bool = True
    historical_local_knowledge_proven: bool = False
    numeric_value_authority: bool = False
    formula_computation_authority: bool = False
    performance_claim: bool = False
    portfolio_or_risk_authority: bool = False
    broker_order_authority: bool = False

    def __post_init__(self) -> None:
        for name in (
            "instrument_id",
            "provider_id",
            "source_id",
            "raw_field_id",
            "definition_fingerprint",
            "reason",
        ):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        _require_utc(self.as_of, "as_of")
        if (
            self.verdict is S6MarketObservationSelectionVerdict.ADMISSIBLE
        ) != (self.selected is not None):
            raise ValueError("only an admissible selection may contain a record")
        if self.selected is not None and (
            self.selected.instrument_id != self.instrument_id
            or self.selected.trade_date != self.trade_date
            or self.selected.provider_id != self.provider_id
            or self.selected.source_id != self.source_id
            or self.selected.raw_field_id != self.raw_field_id
            or self.selected.definition_fingerprint != self.definition_fingerprint
            or self.selected.status not in _VERIFIED_STATUSES
            or self.selected.available_at > self.as_of
        ):
            raise ValueError("selected record does not satisfy the frozen PIT target")
        if (
            not self.source_time_pit_only
            or self.historical_local_knowledge_proven
            or self.numeric_value_authority
            or self.formula_computation_authority
            or self.performance_claim
            or self.portfolio_or_risk_authority
            or self.broker_order_authority
        ):
            raise ValueError(
                "selection cannot acquire value, performance, risk, or execution authority"
            )


def make_s6_market_observation_record(
    *,
    definition: S6MarketFieldDefinition,
    instrument_id: str,
    trade_date: date,
    revision_id: str,
    observed_at: datetime,
    available_at: datetime,
    retrieved_at: datetime,
    content_fingerprint: str,
    status: S6MarketObservationStatus,
) -> S6MarketObservationRecord:
    """Bind one observation revision to an exact market-field definition."""

    return S6MarketObservationRecord(
        instrument_id=instrument_id.strip(),
        trade_date=trade_date,
        provider_id=definition.provider_id,
        source_id=definition.source_id,
        raw_field_id=definition.raw_field_id,
        definition_fingerprint=definition.fingerprint,
        revision_id=revision_id.strip(),
        observed_at=_to_utc(observed_at, "observed_at"),
        available_at=_to_utc(available_at, "available_at"),
        retrieved_at=_to_utc(retrieved_at, "retrieved_at"),
        content_fingerprint=content_fingerprint.strip(),
        status=status,
    )


def build_s6_market_observation_inventory(
    records: Iterable[S6MarketObservationRecord],
) -> S6MarketObservationInventory:
    ordered = tuple(sorted(records, key=_record_sort_key))
    _validate_record_set(ordered)
    availability = tuple(item.available_at for item in ordered)
    counts = tuple(
        S6MarketObservationStatusCount(
            status=status,
            count=sum(item.status is status for item in ordered),
        )
        for status in S6MarketObservationStatus
    )
    return S6MarketObservationInventory(
        schema=_SCHEMA,
        records=ordered,
        status_counts=counts,
        earliest_available_at=min(availability, default=None),
        latest_available_at=max(availability, default=None),
    )


def select_s6_market_observation(
    records: Iterable[S6MarketObservationRecord],
    *,
    definition: S6MarketFieldDefinition,
    instrument_id: str,
    trade_date: date,
    as_of: datetime,
) -> S6MarketObservationSelection:
    """Select the latest verified exact-definition observation visible by as-of."""

    normalized_instrument = instrument_id.strip()
    normalized_as_of = _to_utc(as_of, "as_of")
    inventory = build_s6_market_observation_inventory(records)
    common = dict(
        instrument_id=normalized_instrument,
        trade_date=trade_date,
        provider_id=definition.provider_id,
        source_id=definition.source_id,
        raw_field_id=definition.raw_field_id,
        definition_fingerprint=definition.fingerprint,
        as_of=normalized_as_of,
    )
    if not definition.semantically_admissible:
        return S6MarketObservationSelection(
            **common,
            verdict=S6MarketObservationSelectionVerdict.DEFINITION_NOT_ADMISSIBLE,
            selected=None,
            reason="market_field_definition_is_not_semantically_admissible",
        )

    candidates = tuple(
        item
        for item in inventory.records
        if item.target_identity
        == (
            normalized_instrument,
            trade_date,
            definition.provider_id,
            definition.source_id,
            definition.raw_field_id,
        )
    )
    exact = tuple(
        item
        for item in candidates
        if item.definition_fingerprint == definition.fingerprint
    )
    verified_visible = tuple(
        item
        for item in exact
        if item.status in _VERIFIED_STATUSES and item.available_at <= normalized_as_of
    )
    if verified_visible:
        return S6MarketObservationSelection(
            **common,
            verdict=S6MarketObservationSelectionVerdict.ADMISSIBLE,
            selected=max(verified_visible, key=lambda item: item.available_at),
            reason="latest_verified_exact_definition_observation_available_by_as_of",
        )

    if any(
        item.available_at <= normalized_as_of
        and item.status not in _VERIFIED_STATUSES
        for item in exact
    ):
        verdict = S6MarketObservationSelectionVerdict.UNVERIFIED_OBSERVATION
        reason = "only_unverified_or_unknown_exact_observations_are_visible"
    elif any(item.available_at > normalized_as_of for item in exact):
        verdict = S6MarketObservationSelectionVerdict.NOT_YET_AVAILABLE
        reason = "matching_observation_exists_but_was_not_available_by_as_of"
    elif candidates:
        verdict = S6MarketObservationSelectionVerdict.DEFINITION_MISMATCH
        reason = "observations_exist_only_for_a_different_definition_fingerprint"
    else:
        verdict = S6MarketObservationSelectionVerdict.MISSING
        reason = "no_matching_market_observation"

    return S6MarketObservationSelection(
        **common,
        verdict=verdict,
        selected=None,
        reason=reason,
    )


def _validate_record_set(records: tuple[S6MarketObservationRecord, ...]) -> None:
    identities: set[tuple[object, ...]] = set()
    releases: set[tuple[object, ...]] = set()
    for item in records:
        if item.identity in identities:
            raise ValueError("duplicate market observation identity")
        identities.add(item.identity)
        if item.release_identity in releases:
            raise ValueError("conflicting market observations share one availability time")
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


def _record_sort_key(item: S6MarketObservationRecord) -> tuple[object, ...]:
    return (
        item.instrument_id,
        item.trade_date,
        item.provider_id,
        item.source_id,
        item.raw_field_id,
        item.available_at,
        item.revision_id,
    )


def _record_payload(item: S6MarketObservationRecord) -> dict[str, object]:
    return {
        "instrument_id": item.instrument_id,
        "trade_date": item.trade_date.isoformat(),
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "raw_field_id": item.raw_field_id,
        "definition_fingerprint": item.definition_fingerprint,
        "revision_id": item.revision_id,
        "observed_at": item.observed_at.isoformat(),
        "available_at": item.available_at.isoformat(),
        "retrieved_at": item.retrieved_at.isoformat(),
        "content_fingerprint": item.content_fingerprint,
        "status": item.status.value,
    }


def _inventory_payload(item: S6MarketObservationInventory) -> dict[str, object]:
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
        "formula_computation_authority": item.formula_computation_authority,
        "performance_claim": item.performance_claim,
        "portfolio_or_risk_authority": item.portfolio_or_risk_authority,
        "broker_order_authority": item.broker_order_authority,
    }
