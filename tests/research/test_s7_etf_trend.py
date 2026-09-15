from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from quantlab.research.s7_etf_trend import (
    S7Config,
    S7EvidenceStatus,
    S7State,
    S7TrendObservation,
    S7UniverseMember,
    build_s7_decision,
    build_s7_universe,
)

_AS_OF = datetime(2024, 7, 1, 8, tzinfo=UTC)


def _member(
    instrument_id: str,
    exposure_id: str,
    *,
    evidence_available_at: datetime = datetime(2020, 1, 2, tzinfo=UTC),
    membership_status: S7EvidenceStatus = S7EvidenceStatus.VERIFIED,
    trading_history_status: S7EvidenceStatus = S7EvidenceStatus.VERIFIED,
    corporate_action_status: S7EvidenceStatus = S7EvidenceStatus.VERIFIED,
    effective_from: date = date(2020, 1, 1),
    effective_to: date | None = None,
) -> S7UniverseMember:
    return S7UniverseMember(
        instrument_id=instrument_id,
        exposure_id=exposure_id,
        effective_from=effective_from,
        effective_to=effective_to,
        evidence_available_at=evidence_available_at,
        membership_status=membership_status,
        trading_history_status=trading_history_status,
        corporate_action_status=corporate_action_status,
        evidence_fingerprint="verified-evidence",
    )


def _universe():
    return build_s7_universe(
        (
            _member("ETF_D", "industry_d"),
            _member("ETF_B", "industry_b"),
            _member("ETF_A", "industry_a"),
            _member("ETF_C", "industry_c"),
        )
    )


def _observation(
    universe,
    instrument_id: str,
    total_return_120: float | None,
    *,
    as_of: datetime = _AS_OF,
    feature_available_at: datetime = _AS_OF,
    feature_evidence_fingerprint: str | None = "feature-evidence",
    history_complete: bool | None = True,
    adjusted_price_evidence_verified: bool | None = True,
    research_eligible: bool | None = True,
) -> S7TrendObservation:
    return S7TrendObservation(
        instrument_id=instrument_id,
        as_of=as_of,
        feature_available_at=feature_available_at,
        feature_evidence_fingerprint=feature_evidence_fingerprint,
        universe_fingerprint=universe.fingerprint,
        total_return_120=total_return_120,
        history_complete=history_complete,
        adjusted_price_evidence_verified=adjusted_price_evidence_verified,
        research_eligible=research_eligible,
    )


def test_universe_is_order_invariant_and_rejects_duplicate_exposure() -> None:
    members = (
        _member("ETF_B", "industry_b"),
        _member("ETF_A", "industry_a"),
    )
    first = build_s7_universe(members)
    second = build_s7_universe(reversed(members))

    assert first == second
    assert first.fingerprint == second.fingerprint
    assert tuple(row.instrument_id for row in first.members) == ("ETF_A", "ETF_B")
    assert first.fixed_membership is True
    assert first.real_strategy_universe_admitted is False

    with pytest.raises(ValueError, match="duplicate S7 universe instrument"):
        build_s7_universe((members[0], members[0]))
    with pytest.raises(ValueError, match="duplicate S7 universe exposure"):
        build_s7_universe(
            (
                _member("ETF_A", "same_industry"),
                _member("ETF_B", "same_industry"),
            )
        )


def test_positive_trend_ranks_top_three_and_keeps_residual_cash() -> None:
    universe = _universe()
    observations = (
        _observation(universe, "ETF_A", 0.10),
        _observation(universe, "ETF_B", 0.30),
        _observation(universe, "ETF_C", 0.20),
        _observation(universe, "ETF_D", 0.05),
    )

    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=reversed(observations),
    )

    assert decision.selected_ids == ("ETF_B", "ETF_C", "ETF_A")
    assert [row.target_weight for row in decision.target.positions] == [0.30] * 3
    assert decision.target.cash_weight == pytest.approx(0.10)
    ranks = {
        row.instrument_id: row.relative_strength_rank for row in decision.results
    }
    assert ranks == {
        "ETF_A": 3,
        "ETF_B": 1,
        "ETF_C": 2,
        "ETF_D": 4,
    }
    assert decision.dynamic_n is True
    assert decision.holding_period_lock is False
    assert decision.research_only is True
    assert decision.performance_claim is False
    assert decision.broker_order_authority is False
    assert decision.fingerprint


def test_absolute_trend_gate_uses_dynamic_n_and_cash() -> None:
    universe = _universe()
    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=(
            _observation(universe, "ETF_A", 0.20),
            _observation(universe, "ETF_B", 0.0),
            _observation(universe, "ETF_C", -0.10),
            _observation(universe, "ETF_D", 0.10, research_eligible=False),
        ),
    )

    assert decision.selected_ids == ("ETF_A",)
    assert decision.target.cash_weight == pytest.approx(0.70)
    by_id = {row.instrument_id: row for row in decision.results}
    assert by_id["ETF_B"].state is S7State.INELIGIBLE
    assert by_id["ETF_B"].reasons == ("absolute_trend_not_positive",)
    assert by_id["ETF_C"].state is S7State.INELIGIBLE
    assert by_id["ETF_D"].reasons == ("research_ineligible",)


def test_equal_relative_strength_uses_instrument_id_tie_break() -> None:
    universe = _universe()
    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=(
            _observation(universe, "ETF_A", 0.20),
            _observation(universe, "ETF_B", 0.20),
            _observation(universe, "ETF_C", 0.20),
            _observation(universe, "ETF_D", 0.20),
        ),
    )

    assert decision.selected_ids == ("ETF_A", "ETF_B", "ETF_C")


@pytest.mark.parametrize(
    ("member_kwargs", "observation_kwargs", "expected_reason"),
    [
        (
            {"evidence_available_at": _AS_OF + timedelta(days=1)},
            {},
            "membership_evidence_not_yet_available",
        ),
        (
            {"corporate_action_status": S7EvidenceStatus.UNVERIFIED},
            {},
            "corporate_action_status_unverified",
        ),
        ({}, {"history_complete": False}, "history_complete_not_verified"),
        (
            {},
            {"adjusted_price_evidence_verified": None},
            "adjusted_price_evidence_verified_unknown",
        ),
        (
            {},
            {"feature_available_at": _AS_OF + timedelta(minutes=1)},
            "feature_not_yet_available",
        ),
    ],
)
def test_unknown_or_future_evidence_never_becomes_a_signal(
    member_kwargs, observation_kwargs, expected_reason
) -> None:
    member = _member("ETF_A", "industry_a", **member_kwargs)
    universe = build_s7_universe((member,))
    observation = _observation(universe, "ETF_A", 0.20, **observation_kwargs)

    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=(observation,),
    )

    result = decision.results[0]
    assert result.state is S7State.UNKNOWN
    assert expected_reason in result.reasons
    assert decision.selected_ids == ()
    assert decision.target.cash_weight == 1.0


def test_missing_observation_and_inactive_member_are_distinct() -> None:
    universe = build_s7_universe(
        (
            _member("ETF_A", "industry_a"),
            _member(
                "ETF_OLD",
                "industry_old",
                effective_to=date(2023, 12, 31),
            ),
        )
    )

    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=(),
    )

    by_id = {row.instrument_id: row for row in decision.results}
    assert by_id["ETF_A"].state is S7State.UNKNOWN
    assert by_id["ETF_A"].reasons == ("observation_missing",)
    assert by_id["ETF_OLD"].state is S7State.INELIGIBLE
    assert by_id["ETF_OLD"].reasons == ("outside_verified_effective_interval",)


def test_observations_must_match_time_universe_and_membership() -> None:
    universe = _universe()
    observation = _observation(universe, "ETF_A", 0.20)

    with pytest.raises(ValueError, match="observation as_of"):
        build_s7_decision(
            as_of=_AS_OF,
            universe=universe,
            observations=(replace(observation, as_of=_AS_OF - timedelta(days=1)),),
        )
    with pytest.raises(ValueError, match="exact S7 universe"):
        build_s7_decision(
            as_of=_AS_OF,
            universe=universe,
            observations=(replace(observation, universe_fingerprint="other"),),
        )
    with pytest.raises(ValueError, match="outside frozen universe"):
        build_s7_decision(
            as_of=_AS_OF,
            universe=universe,
            observations=(replace(observation, instrument_id="ETF_X"),),
        )
    with pytest.raises(ValueError, match="duplicate S7 observation"):
        build_s7_decision(
            as_of=_AS_OF,
            universe=universe,
            observations=(observation, observation),
        )


def test_timezone_normalization_and_frozen_rule_parameters() -> None:
    china = timezone(timedelta(hours=8))
    universe = _universe()
    local_as_of = datetime(2024, 7, 1, 16, tzinfo=china)
    observation = _observation(universe, "ETF_A", 0.20)

    decision = build_s7_decision(
        as_of=local_as_of,
        universe=universe,
        observations=(observation,),
    )
    assert decision.as_of == _AS_OF

    with pytest.raises(ValueError, match="frozen at 120"):
        S7Config(lookback_sessions=60)
    with pytest.raises(ValueError, match="frozen at three"):
        S7Config(max_positions=5)
    with pytest.raises(ValueError, match="require evidence fingerprint"):
        _observation(
            universe,
            "ETF_A",
            0.20,
            feature_evidence_fingerprint=None,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_s7_decision(
            as_of=datetime(2024, 7, 1, 8),
            universe=universe,
            observations=(),
        )


def test_decision_cannot_acquire_holding_lock_or_execution_authority() -> None:
    universe = _universe()
    decision = build_s7_decision(
        as_of=_AS_OF,
        universe=universe,
        observations=(_observation(universe, "ETF_A", 0.20),),
    )

    with pytest.raises(ValueError, match="cannot acquire"):
        replace(decision, holding_period_lock=True)
    with pytest.raises(ValueError, match="cannot acquire"):
        replace(decision, broker_order_authority=True)
    with pytest.raises(ValueError, match="top-three"):
        replace(
            decision,
            results=tuple(
                replace(row, selected=False) if row.instrument_id == "ETF_A" else row
                for row in decision.results
            ),
        )
