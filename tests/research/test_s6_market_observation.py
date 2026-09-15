from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from quantlab.research.s6_market_input_admission import (
    S6MarketCorporateActionBasis,
    S6MarketFieldDefinition,
    S6MarketFieldEvidenceStatus,
    S6MarketInputKind,
    S6MarketObservationTiming,
    S6MarketPriceBasis,
    S6MarketShareScope,
)
from quantlab.research.s6_market_observation import (
    S6MarketObservationSelectionVerdict,
    S6MarketObservationStatus,
    build_s6_market_observation_inventory,
    make_s6_market_observation_record,
    select_s6_market_observation,
)

_TRADE_DATE = date(2024, 6, 3)
_OBSERVED = datetime(2024, 6, 3, 7, tzinfo=UTC)
_AVAILABLE = datetime(2024, 6, 3, 7, 5, tzinfo=UTC)
_RETRIEVED = datetime(2024, 6, 4, tzinfo=UTC)


def _definition(
    *,
    raw_field_id: str = "total_mv",
    price_basis: S6MarketPriceBasis = S6MarketPriceBasis.RAW_UNADJUSTED,
) -> S6MarketFieldDefinition:
    return S6MarketFieldDefinition(
        provider_id="synthetic-provider",
        source_id="synthetic-source",
        raw_field_id=raw_field_id,
        semantic_field_id="market_cap",
        input_kind=S6MarketInputKind.MARKET_CAP,
        price_basis=price_basis,
        share_scope=S6MarketShareScope.TOTAL_OUTSTANDING,
        currency="CNY",
        observation_timing=S6MarketObservationTiming.EXCHANGE_SESSION_CLOSE,
        corporate_action_basis=(
            S6MarketCorporateActionBasis.AS_OBSERVED_ON_TRADE_DATE
        ),
        evidence_status=S6MarketFieldEvidenceStatus.VERIFIED,
        documentation_fingerprint="market-documentation",
    )


def _record(
    *,
    definition: S6MarketFieldDefinition | None = None,
    revision_id: str = "original",
    available_at: datetime = _AVAILABLE,
    retrieved_at: datetime = _RETRIEVED,
    content_fingerprint: str = "content-original",
    status: S6MarketObservationStatus = (
        S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED
    ),
):
    return make_s6_market_observation_record(
        definition=definition or _definition(),
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        revision_id=revision_id,
        observed_at=_OBSERVED,
        available_at=available_at,
        retrieved_at=retrieved_at,
        content_fingerprint=content_fingerprint,
        status=status,
    )


def test_record_normalizes_aware_times_and_definition_identity() -> None:
    china = timezone(timedelta(hours=8))
    definition = _definition()
    record = make_s6_market_observation_record(
        definition=definition,
        instrument_id=" 000001.SZ ",
        trade_date=_TRADE_DATE,
        revision_id=" original ",
        observed_at=datetime(2024, 6, 3, 15, tzinfo=china),
        available_at=datetime(2024, 6, 3, 15, 5, tzinfo=china),
        retrieved_at=datetime(2024, 6, 4, 8, tzinfo=china),
        content_fingerprint=" content ",
        status=S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED,
    )

    assert record.instrument_id == "000001.SZ"
    assert record.provider_id == definition.provider_id
    assert record.raw_field_id == definition.raw_field_id
    assert record.definition_fingerprint == definition.fingerprint
    assert record.observed_at == _OBSERVED
    assert record.available_at == _AVAILABLE
    assert record.retrieved_at == _RETRIEVED
    assert record.content_fingerprint == "content"
    assert record.fingerprint


def test_inventory_is_deterministic_and_retains_all_evidence_statuses() -> None:
    records = (
        _record(),
        _record(
            revision_id="correction",
            available_at=datetime(2024, 6, 5, tzinfo=UTC),
            retrieved_at=datetime(2024, 6, 6, tzinfo=UTC),
            content_fingerprint="content-correction",
            status=S6MarketObservationStatus.CORRECTION_CHAIN_VERIFIED,
        ),
        _record(
            revision_id="latest-only",
            available_at=datetime(2024, 6, 7, tzinfo=UTC),
            retrieved_at=datetime(2024, 6, 8, tzinfo=UTC),
            content_fingerprint="content-latest",
            status=S6MarketObservationStatus.LATEST_ONLY_UNVERIFIED,
        ),
        _record(
            revision_id="unknown",
            available_at=datetime(2024, 6, 9, tzinfo=UTC),
            retrieved_at=datetime(2024, 6, 10, tzinfo=UTC),
            content_fingerprint="content-unknown",
            status=S6MarketObservationStatus.UNKNOWN,
        ),
    )

    first = build_s6_market_observation_inventory(records)
    second = build_s6_market_observation_inventory(reversed(records))

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert {item.status: item.count for item in first.status_counts} == {
        S6MarketObservationStatus.ORIGINAL_OBSERVATION_VERIFIED: 1,
        S6MarketObservationStatus.CORRECTION_CHAIN_VERIFIED: 1,
        S6MarketObservationStatus.LATEST_ONLY_UNVERIFIED: 1,
        S6MarketObservationStatus.UNKNOWN: 1,
    }
    assert first.source_time_pit_only is True
    assert first.historical_local_knowledge_proven is False
    assert first.numeric_values_included is False
    assert first.formula_computation_authority is False
    assert first.performance_claim is False
    assert first.portfolio_or_risk_authority is False
    assert first.broker_order_authority is False


def test_verified_correction_is_selected_only_after_its_availability() -> None:
    definition = _definition()
    original = _record(definition=definition)
    correction = _record(
        definition=definition,
        revision_id="correction",
        available_at=datetime(2024, 6, 5, tzinfo=UTC),
        retrieved_at=datetime(2024, 6, 6, tzinfo=UTC),
        content_fingerprint="content-correction",
        status=S6MarketObservationStatus.CORRECTION_CHAIN_VERIFIED,
    )

    before = select_s6_market_observation(
        (correction, original),
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )
    after = select_s6_market_observation(
        (original, correction),
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 5, tzinfo=UTC),
    )

    assert before.verdict is S6MarketObservationSelectionVerdict.ADMISSIBLE
    assert before.selected is original
    assert after.verdict is S6MarketObservationSelectionVerdict.ADMISSIBLE
    assert after.selected is correction
    assert after.historical_local_knowledge_proven is False
    assert after.numeric_value_authority is False
    assert after.formula_computation_authority is False
    assert after.performance_claim is False
    assert after.portfolio_or_risk_authority is False
    assert after.broker_order_authority is False


@pytest.mark.parametrize(
    "status",
    [
        S6MarketObservationStatus.LATEST_ONLY_UNVERIFIED,
        S6MarketObservationStatus.UNKNOWN,
    ],
)
def test_weak_visible_observation_is_never_selected(
    status: S6MarketObservationStatus,
) -> None:
    definition = _definition()
    result = select_s6_market_observation(
        (_record(definition=definition, status=status),),
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )

    assert result.verdict is (
        S6MarketObservationSelectionVerdict.UNVERIFIED_OBSERVATION
    )
    assert result.selected is None


def test_future_matching_observation_is_not_yet_available() -> None:
    definition = _definition()
    result = select_s6_market_observation(
        (_record(definition=definition),),
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 3, 7, 4, tzinfo=UTC),
    )

    assert result.verdict is S6MarketObservationSelectionVerdict.NOT_YET_AVAILABLE
    assert result.selected is None


def test_inadmissible_definition_is_rejected_before_selection() -> None:
    adjusted = _definition(price_basis=S6MarketPriceBasis.CUMULATIVE_ADJUSTED)
    result = select_s6_market_observation(
        (_record(definition=adjusted),),
        definition=adjusted,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )

    assert result.verdict is (
        S6MarketObservationSelectionVerdict.DEFINITION_NOT_ADMISSIBLE
    )
    assert result.selected is None


def test_different_definition_fingerprint_is_an_explicit_mismatch() -> None:
    requested = _definition()
    other = _definition(raw_field_id="provider_total_mv")
    mismatched = replace(
        _record(definition=requested),
        definition_fingerprint=other.fingerprint,
    )

    result = select_s6_market_observation(
        (mismatched,),
        definition=requested,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )

    assert result.verdict is S6MarketObservationSelectionVerdict.DEFINITION_MISMATCH
    assert result.selected is None


def test_missing_target_is_explicit() -> None:
    definition = _definition()
    result = select_s6_market_observation(
        (_record(definition=definition),),
        definition=definition,
        instrument_id="600000.SH",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )

    assert result.verdict is S6MarketObservationSelectionVerdict.MISSING
    assert result.selected is None


def test_duplicate_identity_and_time_conflict_are_rejected() -> None:
    original = _record()
    with pytest.raises(ValueError, match="duplicate market observation identity"):
        build_s6_market_observation_inventory((original, original))

    conflict = _record(
        revision_id="conflict",
        content_fingerprint="different-content",
    )
    with pytest.raises(ValueError, match="share one availability time"):
        build_s6_market_observation_inventory((original, conflict))


def test_timestamp_backdating_and_naive_as_of_fail_closed() -> None:
    with pytest.raises(ValueError, match="available_at cannot precede observed_at"):
        _record(available_at=datetime(2024, 6, 3, 6, 59, tzinfo=UTC))
    with pytest.raises(ValueError, match="retrieved_at cannot precede available_at"):
        _record(retrieved_at=datetime(2024, 6, 3, 7, 4, tzinfo=UTC))
    with pytest.raises(ValueError, match="timezone-aware"):
        select_s6_market_observation(
            (_record(),),
            definition=_definition(),
            instrument_id="000001.SZ",
            trade_date=_TRADE_DATE,
            as_of=datetime(2024, 6, 4),
        )


def test_direct_inventory_construction_cannot_bypass_invariants() -> None:
    original = _record()
    correction = _record(
        revision_id="correction",
        available_at=datetime(2024, 6, 5, tzinfo=UTC),
        retrieved_at=datetime(2024, 6, 6, tzinfo=UTC),
        content_fingerprint="content-correction",
        status=S6MarketObservationStatus.CORRECTION_CHAIN_VERIFIED,
    )
    inventory = build_s6_market_observation_inventory((original, correction))

    with pytest.raises(ValueError, match="deterministic frozen order"):
        replace(inventory, records=tuple(reversed(inventory.records)))
    bad_counts = (
        replace(inventory.status_counts[0], count=0),
        replace(inventory.status_counts[1], count=2),
        *inventory.status_counts[2:],
    )
    with pytest.raises(ValueError, match="status_counts do not match"):
        replace(inventory, status_counts=bad_counts)
    with pytest.raises(ValueError, match="earliest_available_at"):
        replace(
            inventory,
            earliest_available_at=datetime(2024, 6, 4, tzinfo=UTC),
        )


def test_direct_selection_cannot_embed_an_ineligible_record() -> None:
    definition = _definition()
    result = select_s6_market_observation(
        (_record(definition=definition),),
        definition=definition,
        instrument_id="000001.SZ",
        trade_date=_TRADE_DATE,
        as_of=datetime(2024, 6, 4, tzinfo=UTC),
    )
    weak = _record(
        definition=definition,
        status=S6MarketObservationStatus.UNKNOWN,
    )

    with pytest.raises(ValueError, match="frozen PIT target"):
        replace(result, selected=weak)
