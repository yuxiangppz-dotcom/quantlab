"""Provider-neutral semantic admission for S6 daily market inputs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from quantlab.data.models import canonical_payload_fingerprint

_SCHEMA = "quantlab_s6_market_input_catalog_v1"


class S6MarketInputKind(StrEnum):
    CLOSE_PRICE = "close_price"
    SHARE_COUNT = "share_count"
    MARKET_CAP = "market_cap"


class S6MarketPriceBasis(StrEnum):
    RAW_UNADJUSTED = "raw_unadjusted"
    CUMULATIVE_ADJUSTED = "cumulative_adjusted"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class S6MarketShareScope(StrEnum):
    TOTAL_OUTSTANDING = "total_outstanding"
    CIRCULATING = "circulating"
    FREE_FLOAT = "free_float"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class S6MarketObservationTiming(StrEnum):
    EXCHANGE_SESSION_CLOSE = "exchange_session_close"
    INTRADAY_SNAPSHOT = "intraday_snapshot"
    UNKNOWN = "unknown"


class S6MarketCorporateActionBasis(StrEnum):
    AS_OBSERVED_ON_TRADE_DATE = "as_observed_on_trade_date"
    RETROACTIVELY_RESTATED = "retroactively_restated"
    UNKNOWN = "unknown"


class S6MarketFieldEvidenceStatus(StrEnum):
    VERIFIED = "verified"
    PROVIDER_DOCUMENTED_UNVERIFIED = "provider_documented_unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class S6MarketFieldDefinition:
    provider_id: str
    source_id: str
    raw_field_id: str
    semantic_field_id: str
    input_kind: S6MarketInputKind
    price_basis: S6MarketPriceBasis
    share_scope: S6MarketShareScope
    currency: str | None
    observation_timing: S6MarketObservationTiming
    corporate_action_basis: S6MarketCorporateActionBasis
    evidence_status: S6MarketFieldEvidenceStatus
    documentation_fingerprint: str | None
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("provider_id", "source_id", "raw_field_id", "semantic_field_id"):
            value = getattr(self, name)
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")
            if value != value.strip():
                raise ValueError(f"{name} must be normalized")
        _validate_kind_shape(self)
        _validate_currency(self)
        if self.documentation_fingerprint is not None and (
            not self.documentation_fingerprint.strip()
            or self.documentation_fingerprint
            != self.documentation_fingerprint.strip()
        ):
            raise ValueError(
                "documentation_fingerprint must be normalized when present"
            )
        if (
            self.evidence_status is S6MarketFieldEvidenceStatus.VERIFIED
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
        """Return whether this definition is safe for daily PIT valuation binding."""

        return (
            self.evidence_status is S6MarketFieldEvidenceStatus.VERIFIED
            and self.observation_timing
            is S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE
            and self.corporate_action_basis
            is S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE
            and (
                self.input_kind is S6MarketInputKind.SHARE_COUNT
                or self.price_basis is S6MarketPriceBasis.RAW_UNADJUSTED
            )
            and (
                self.input_kind is S6MarketInputKind.CLOSE_PRICE
                or self.share_scope
                not in {
                    S6MarketShareScope.UNKNOWN,
                    S6MarketShareScope.NOT_APPLICABLE,
                }
            )
        )


@dataclass(frozen=True)
class S6MarketInputCatalog:
    schema: str
    definitions: tuple[S6MarketFieldDefinition, ...]
    verified_count: int
    unverified_count: int
    unknown_count: int
    numeric_values_included: bool = False
    formula_bindings_included: bool = False
    performance_claim: bool = False
    broker_order_authority: bool = False
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError(f"schema must equal {_SCHEMA}")
        if self.definitions != tuple(
            sorted(self.definitions, key=lambda item: item.identity)
        ):
            raise ValueError("definitions must be in deterministic identity order")
        identities = tuple(item.identity for item in self.definitions)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate market field identity")
        expected = (
            sum(
                item.evidence_status is S6MarketFieldEvidenceStatus.VERIFIED
                for item in self.definitions
            ),
            sum(
                item.evidence_status
                is S6MarketFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
                for item in self.definitions
            ),
            sum(
                item.evidence_status is S6MarketFieldEvidenceStatus.UNKNOWN
                for item in self.definitions
            ),
        )
        if (self.verified_count, self.unverified_count, self.unknown_count) != expected:
            raise ValueError("catalog evidence counts do not match definitions")
        if (
            self.numeric_values_included
            or self.formula_bindings_included
            or self.performance_claim
            or self.broker_order_authority
        ):
            raise ValueError(
                "catalog cannot acquire numeric, formula, performance, or execution authority"
            )
        object.__setattr__(
            self,
            "fingerprint",
            canonical_payload_fingerprint(_catalog_payload(self)),
        )


def build_s6_market_input_catalog(
    definitions: Iterable[S6MarketFieldDefinition],
) -> S6MarketInputCatalog:
    ordered = tuple(sorted(definitions, key=lambda item: item.identity))
    return S6MarketInputCatalog(
        schema=_SCHEMA,
        definitions=ordered,
        verified_count=sum(
            item.evidence_status is S6MarketFieldEvidenceStatus.VERIFIED
            for item in ordered
        ),
        unverified_count=sum(
            item.evidence_status
            is S6MarketFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
            for item in ordered
        ),
        unknown_count=sum(
            item.evidence_status is S6MarketFieldEvidenceStatus.UNKNOWN
            for item in ordered
        ),
    )


def _validate_kind_shape(item: S6MarketFieldDefinition) -> None:
    if item.input_kind is S6MarketInputKind.CLOSE_PRICE:
        if item.price_basis is S6MarketPriceBasis.NOT_APPLICABLE:
            raise ValueError("close-price fields require a price basis")
        if item.share_scope is not S6MarketShareScope.NOT_APPLICABLE:
            raise ValueError("close-price fields cannot declare share scope")
        return
    if item.input_kind is S6MarketInputKind.SHARE_COUNT:
        if item.price_basis is not S6MarketPriceBasis.NOT_APPLICABLE:
            raise ValueError("share-count fields cannot declare a price basis")
        if item.share_scope is S6MarketShareScope.NOT_APPLICABLE:
            raise ValueError("share-count fields require a share scope")
        return
    if item.price_basis is S6MarketPriceBasis.NOT_APPLICABLE:
        raise ValueError("market-cap fields require a price basis")
    if item.share_scope is S6MarketShareScope.NOT_APPLICABLE:
        raise ValueError("market-cap fields require a share scope")


def _validate_currency(item: S6MarketFieldDefinition) -> None:
    if item.input_kind is S6MarketInputKind.SHARE_COUNT:
        if item.currency is not None:
            raise ValueError("share-count fields cannot declare currency")
        return
    if (
        item.currency is None
        or len(item.currency) != 3
        or not item.currency.isascii()
        or not item.currency.isalpha()
        or not item.currency.isupper()
    ):
        raise ValueError(
            "close-price and market-cap fields require an uppercase ISO-like currency"
        )


def _definition_payload(item: S6MarketFieldDefinition) -> dict[str, object]:
    return {
        "provider_id": item.provider_id,
        "source_id": item.source_id,
        "raw_field_id": item.raw_field_id,
        "semantic_field_id": item.semantic_field_id,
        "input_kind": item.input_kind.value,
        "price_basis": item.price_basis.value,
        "share_scope": item.share_scope.value,
        "currency": item.currency,
        "observation_timing": item.observation_timing.value,
        "corporate_action_basis": item.corporate_action_basis.value,
        "evidence_status": item.evidence_status.value,
        "documentation_fingerprint": item.documentation_fingerprint,
    }


def _catalog_payload(item: S6MarketInputCatalog) -> dict[str, object]:
    return {
        "schema": item.schema,
        "definition_fingerprints": [
            definition.fingerprint for definition in item.definitions
        ],
        "verified_count": item.verified_count,
        "unverified_count": item.unverified_count,
        "unknown_count": item.unknown_count,
        "numeric_values_included": item.numeric_values_included,
        "formula_bindings_included": item.formula_bindings_included,
        "performance_claim": item.performance_claim,
        "broker_order_authority": item.broker_order_authority,
    }
