from __future__ import annotations

from dataclasses import replace

import pytest

from quantlab.research.s6_market_input_admission import (
    S6MarketCorporateActionBasis,
    S6MarketFieldDefinition,
    S6MarketFieldEvidenceStatus,
    S6MarketInputKind,
    S6MarketObservationTiming,
    S6MarketPriceBasis,
    S6MarketShareScope,
    build_s6_market_input_catalog,
)


def _definition(
    raw_field_id: str,
    *,
    semantic_field_id: str | None = None,
    input_kind: S6MarketInputKind = S6MarketInputKind.MARKET_CAP,
    price_basis: S6MarketPriceBasis = S6MarketPriceBasis.RAW_UNADJUSTED,
    share_scope: S6MarketShareScope = S6MarketShareScope.TOTAL_OUTSTANDING,
    currency: str | None = "CNY",
    observation_timing: S6MarketObservationTiming = (
        S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE
    ),
    corporate_action_basis: S6MarketCorporateActionBasis = (
        S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE
    ),
    evidence_status: S6MarketFieldEvidenceStatus = (
        S6MarketFieldEvidenceStatus.VERIFIED
    ),
) -> S6MarketFieldDefinition:
    return S6MarketFieldDefinition(
        provider_id="synthetic-provider",
        source_id="synthetic-source",
        raw_field_id=raw_field_id,
        semantic_field_id=semantic_field_id or raw_field_id,
        input_kind=input_kind,
        price_basis=price_basis,
        share_scope=share_scope,
        currency=currency,
        observation_timing=observation_timing,
        corporate_action_basis=corporate_action_basis,
        evidence_status=evidence_status,
        documentation_fingerprint=(
            "documentation"
            if evidence_status is not S6MarketFieldEvidenceStatus.UNKNOWN
            else None
        ),
    )


def _price(
    raw_field_id: str = "close",
    *,
    price_basis: S6MarketPriceBasis = S6MarketPriceBasis.RAW_UNADJUSTED,
) -> S6MarketFieldDefinition:
    return _definition(
        raw_field_id,
        input_kind=S6MarketInputKind.CLOSE_PRICE,
        price_basis=price_basis,
        share_scope=S6MarketShareScope.NOT_APPLICABLE,
    )


def _shares(
    raw_field_id: str = "total_share_count",
    *,
    share_scope: S6MarketShareScope = S6MarketShareScope.TOTAL_OUTSTANDING,
) -> S6MarketFieldDefinition:
    return _definition(
        raw_field_id,
        input_kind=S6MarketInputKind.SHARE_COUNT,
        price_basis=S6MarketPriceBasis.NOT_APPLICABLE,
        share_scope=share_scope,
        currency=None,
    )


def test_catalog_is_deterministic_and_retains_all_evidence_levels() -> None:
    definitions = (
        _definition("total_mv"),
        _price(),
        replace(
            _shares(),
            evidence_status=S6MarketFieldEvidenceStatus.UNKNOWN,
            documentation_fingerprint=None,
        ),
        replace(
            _definition("circ_mv"),
            evidence_status=(
                S6MarketFieldEvidenceStatus.PROVIDER_DOCUMENTED_UNVERIFIED
            ),
        ),
    )

    first = build_s6_market_input_catalog(definitions)
    second = build_s6_market_input_catalog(reversed(definitions))

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.verified_count == 2
    assert first.unverified_count == 1
    assert first.unknown_count == 1
    assert first.numeric_values_included is False
    assert first.formula_bindings_included is False
    assert first.performance_claim is False
    assert first.broker_order_authority is False


def test_verified_close_market_cap_and_share_count_are_admissible() -> None:
    definitions = (_price(), _definition("total_mv"), _shares())

    assert all(item.semantically_admissible for item in definitions)
    assert len({item.fingerprint for item in definitions}) == 3


def test_adjusted_price_is_retained_but_not_admissible_for_valuation() -> None:
    adjusted = _price(
        raw_field_id="adj_close",
        price_basis=S6MarketPriceBasis.CUMULATIVE_ADJUSTED,
    )

    assert adjusted.semantically_admissible is False
    catalog = build_s6_market_input_catalog((adjusted,))
    assert catalog.verified_count == 1


@pytest.mark.parametrize(
    ("timing", "action_basis"),
    [
        (
            S6MarketObservationTiming.UNKNOWN,
            S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE,
        ),
        (
            S6MarketObservationTiming.INTRADAY_SNAPSHOT,
            S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE,
        ),
        (
            S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE,
            S6MarketCorporateActionBasis.UNKNOWN,
        ),
        (
            S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE,
            S6MarketCorporateActionBasis.RETROACTIVELY_RESTATED,
        ),
    ],
)
def test_non_daily_or_non_pit_semantics_fail_closed(
    timing: S6MarketObservationTiming,
    action_basis: S6MarketCorporateActionBasis,
) -> None:
    definition = _definition(
        "total_mv",
        observation_timing=timing,
        corporate_action_basis=action_basis,
    )

    assert definition.semantically_admissible is False


def test_share_scope_is_explicit_and_part_of_the_fingerprint() -> None:
    total = _definition("provider_mv", share_scope=S6MarketShareScope.TOTAL_OUTSTANDING)
    circulating = replace(total, share_scope=S6MarketShareScope.CIRCULATING)
    unknown = replace(total, share_scope=S6MarketShareScope.UNKNOWN)

    assert total.fingerprint != circulating.fingerprint
    assert total.semantically_admissible is True
    assert circulating.semantically_admissible is True
    assert unknown.semantically_admissible is False


def test_input_kind_shape_and_currency_rules_fail_closed() -> None:
    with pytest.raises(ValueError, match="cannot declare share scope"):
        replace(_price(), share_scope=S6MarketShareScope.TOTAL_OUTSTANDING)
    with pytest.raises(ValueError, match="cannot declare a price basis"):
        replace(_shares(), price_basis=S6MarketPriceBasis.RAW_UNADJUSTED)
    with pytest.raises(ValueError, match="require a share scope"):
        replace(_shares(), share_scope=S6MarketShareScope.NOT_APPLICABLE)
    with pytest.raises(ValueError, match="cannot declare currency"):
        replace(_shares(), currency="CNY")
    with pytest.raises(ValueError, match="uppercase ISO-like"):
        replace(_price(), currency="cny")


def test_verified_definition_requires_documentation_evidence() -> None:
    with pytest.raises(ValueError, match="require documentation evidence"):
        replace(_definition("total_mv"), documentation_fingerprint=None)


def test_direct_catalog_construction_cannot_bypass_identity_or_counts() -> None:
    first = _price()
    second = _definition("total_mv")
    catalog = build_s6_market_input_catalog((first, second))

    with pytest.raises(ValueError, match="deterministic identity order"):
        replace(catalog, definitions=tuple(reversed(catalog.definitions)))
    with pytest.raises(ValueError, match="duplicate market field identity"):
        replace(catalog, definitions=(first, first), verified_count=2)
    with pytest.raises(ValueError, match="counts do not match"):
        replace(catalog, verified_count=0, unknown_count=2)
