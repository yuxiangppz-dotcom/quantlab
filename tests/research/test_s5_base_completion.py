from dataclasses import replace
from datetime import date

import pytest

from quantlab.research.s5_base_completion import (
    S5BaseConfig,
    S5BaseObservation,
    S5BaseState,
    evaluate_s5_base,
)


def _ready(**changes: object) -> S5BaseObservation:
    base = S5BaseObservation(
        entity_id="000001.SZ",
        as_of=date(2026, 9, 14),
        prior_drawdown_from_120d_high=-0.30,
        recent_return_10=-0.01,
        max_drawdown_10=0.04,
        new_low_count_10=0,
        new_low_count_20=0,
        max_new_low_undercut_10=0.0,
        volatility_ratio_10_to_prior_20=0.80,
        recovery_from_10d_low=0.06,
        prior_low_reclaimed=True,
        reclaim_sessions=None,
        close_location_20=0.65,
        close_to_ma20_ratio=1.01,
        ma20_slope_5=0.002,
        breakout_distance_20=-0.01,
        volume_ratio_5_to_20=1.0,
        history_complete=True,
    )
    return replace(base, **changes)


def test_no_new_low_can_be_ready() -> None:
    result = evaluate_s5_base(_ready())
    assert result.state is S5BaseState.BASE_READY
    assert result.reasons == (
        "price_breakout_not_confirmed",
        "breakout_volume_not_confirmed",
    )


def test_one_shallow_new_low_with_reclaim_is_not_failure() -> None:
    result = evaluate_s5_base(
        _ready(
            new_low_count_10=1,
            new_low_count_20=2,
            max_new_low_undercut_10=0.012,
            reclaim_sessions=2,
        )
    )
    assert result.state is S5BaseState.BASE_READY


def test_several_shallow_lows_without_material_decline_keep_building() -> None:
    result = evaluate_s5_base(
        _ready(
            new_low_count_10=4,
            new_low_count_20=5,
            max_new_low_undercut_10=0.015,
            reclaim_sessions=2,
        )
    )
    assert result.state is S5BaseState.BASE_BUILDING
    assert result.reasons == ("new_low_frequency_still_high",)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"max_new_low_undercut_10": 0.031}, "new_low_undercut_too_deep"),
        ({"recent_return_10": -0.061}, "recent_decline_too_fast"),
        ({"max_drawdown_10": 0.081}, "recent_drawdown_too_large"),
        (
            {"volatility_ratio_10_to_prior_20": 1.11},
            "downside_volatility_expanding",
        ),
    ],
)
def test_structural_invalidation_fails(
    changes: dict[str, object], reason: str
) -> None:
    result = evaluate_s5_base(_ready(**changes))
    assert result.state is S5BaseState.FAILED
    assert result.reasons == (reason,)


def test_failure_reason_order_is_deterministic() -> None:
    result = evaluate_s5_base(
        _ready(
            max_new_low_undercut_10=0.10,
            recent_return_10=-0.20,
            max_drawdown_10=0.20,
            volatility_ratio_10_to_prior_20=2.0,
        )
    )
    assert result.reasons == (
        "new_low_undercut_too_deep",
        "recent_decline_too_fast",
        "recent_drawdown_too_large",
        "downside_volatility_expanding",
    )


def test_unreclaimed_shallow_low_keeps_building() -> None:
    result = evaluate_s5_base(
        _ready(
            new_low_count_10=1,
            new_low_count_20=1,
            max_new_low_undercut_10=0.01,
            prior_low_reclaimed=False,
            reclaim_sessions=4,
        )
    )
    assert result.state is S5BaseState.BASE_BUILDING
    assert "prior_low_not_reclaimed" in result.reasons


def test_confirmed_breakout_requires_price_and_volume() -> None:
    result = evaluate_s5_base(
        _ready(breakout_distance_20=0.005, volume_ratio_5_to_20=1.20)
    )
    assert result.state is S5BaseState.BREAKOUT_CONFIRMED
    assert result.reasons == (
        "price_breakout_confirmed",
        "breakout_volume_confirmed",
    )


def test_price_breakout_without_volume_remains_ready() -> None:
    result = evaluate_s5_base(
        _ready(breakout_distance_20=0.005, volume_ratio_5_to_20=1.19)
    )
    assert result.state is S5BaseState.BASE_READY
    assert result.reasons == ("breakout_volume_not_confirmed",)


def test_slow_reclaim_keeps_shallow_new_low_in_base_building() -> None:
    result = evaluate_s5_base(
        _ready(
            new_low_count_10=1,
            new_low_count_20=1,
            max_new_low_undercut_10=0.01,
            reclaim_sessions=4,
        )
    )
    assert result.state is S5BaseState.BASE_BUILDING
    assert result.reasons == ("prior_low_reclaim_too_slow",)


def test_missing_reclaim_timing_for_a_new_low_is_unknown() -> None:
    result = evaluate_s5_base(
        _ready(new_low_count_10=1, new_low_count_20=1, reclaim_sessions=None)
    )
    assert result.state is S5BaseState.UNKNOWN
    assert result.reasons == ("missing_reclaim_sessions",)


def test_missing_or_unverified_history_is_unknown() -> None:
    result = evaluate_s5_base(
        _ready(recent_return_10=None, history_complete=None)
    )
    assert result.state is S5BaseState.UNKNOWN
    assert result.reasons == ("history_unknown", "missing_recent_return_10")


def test_multiple_incomplete_structure_gates_keep_reason_order() -> None:
    result = evaluate_s5_base(
        _ready(
            new_low_count_10=5,
            new_low_count_20=6,
            prior_low_reclaimed=False,
            reclaim_sessions=5,
            recovery_from_10d_low=0.01,
            close_location_20=0.30,
            close_to_ma20_ratio=0.99,
            ma20_slope_5=-0.01,
        )
    )
    assert result.state is S5BaseState.BASE_BUILDING
    assert result.reasons == (
        "new_low_frequency_still_high",
        "new_low_frequency_20_still_high",
        "prior_low_not_reclaimed",
        "recovery_from_low_insufficient",
        "close_location_still_low",
        "short_ma_not_reclaimed",
        "short_ma_still_falling",
    )


def test_result_cannot_claim_performance_or_order_authority() -> None:
    result = evaluate_s5_base(_ready())
    assert result.research_only is True
    assert result.performance_claim is False
    assert result.broker_order_authority is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"new_low_count_10": True},
        {"new_low_count_10": 11},
        {"new_low_count_20": 21},
        {"new_low_count_10": 5, "new_low_count_20": 4},
        {"reclaim_sessions": True},
        {"max_new_low_undercut_10": -0.01},
        {"close_to_ma20_ratio": 0.0},
        {"prior_low_reclaimed": 1},
        {"recent_return_10": float("nan")},
    ],
)
def test_observation_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _ready(**kwargs)


def test_config_rejects_invalid_threshold() -> None:
    with pytest.raises(ValueError, match="max_new_low_count_ready"):
        S5BaseConfig(max_new_low_count_ready=11)
