from __future__ import annotations

from datetime import date, timedelta

import pytest

from quantlab.research.s5_sector_bottom import (
    S5Config,
    S5SectorObservation,
    S5State,
    S5StockObservation,
    build_s5_decision,
    evaluate_s5_sector,
    evaluate_s5_stock,
)

AS_OF = date(2026, 9, 14)


def _sector(**overrides: object) -> S5SectorObservation:
    values: dict[str, object] = {
        "sector_id": "POWER",
        "as_of": AS_OF,
        "drawdown_from_120d_high": -0.35,
        "new_low_rate_20": 0.05,
        "volatility_percentile_252": 0.25,
        "relative_return_20": 0.03,
        "history_complete": True,
    }
    values.update(overrides)
    return S5SectorObservation(**values)  # type: ignore[arg-type]


def _stock(instrument_id: str = "000001.SZ", **overrides: object) -> S5StockObservation:
    values: dict[str, object] = {
        "instrument_id": instrument_id,
        "sector_id": "POWER",
        "as_of": AS_OF,
        "membership_verified": True,
        "relative_return_20_vs_sector": 0.04,
        "close_to_ma20_ratio": 1.03,
        "ma20_slope_5": 0.01,
        "up_down_volume_ratio_20": 1.20,
        "research_eligible": True,
    }
    values.update(overrides)
    return S5StockObservation(**values)  # type: ignore[arg-type]


def test_sector_bottom_pattern_is_eligible() -> None:
    result = evaluate_s5_sector(_sector())

    assert result.state is S5State.ELIGIBLE
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("drawdown_from_120d_high", -0.20, "drawdown_not_deep_enough"),
        ("new_low_rate_20", 0.20, "still_making_too_many_new_lows"),
        ("volatility_percentile_252", 0.60, "volatility_not_compressed"),
        ("relative_return_20", -0.01, "sector_relative_strength_not_positive"),
    ],
)
def test_sector_continuation_patterns_are_ineligible(
    field: str,
    value: float,
    reason: str,
) -> None:
    result = evaluate_s5_sector(_sector(**{field: value}))

    assert result.state is S5State.INELIGIBLE
    assert reason in result.reasons


def test_incomplete_sector_history_is_unknown_not_safe_or_ineligible() -> None:
    result = evaluate_s5_sector(
        _sector(history_complete=False, volatility_percentile_252=None)
    )

    assert result.state is S5State.UNKNOWN
    assert result.reasons == (
        "sector_history_incomplete",
        "missing_volatility_percentile_252",
    )


def test_unknown_sector_membership_blocks_stock() -> None:
    sector_result = evaluate_s5_sector(_sector())
    result = evaluate_s5_stock(_stock(membership_verified=None), sector_result)

    assert result.state is S5State.UNKNOWN
    assert result.reasons == ("sector_membership_unknown",)


def test_known_membership_rejection_is_ineligible() -> None:
    sector_result = evaluate_s5_sector(_sector())
    result = evaluate_s5_stock(_stock(membership_verified=False), sector_result)

    assert result.state is S5State.INELIGIBLE
    assert result.reasons == ("sector_membership_not_verified",)


def test_stock_in_ineligible_sector_cannot_pass() -> None:
    sector_result = evaluate_s5_sector(_sector(relative_return_20=-0.01))
    result = evaluate_s5_stock(_stock(), sector_result)

    assert result.state is S5State.INELIGIBLE
    assert result.reasons == ("sector_ineligible",)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "relative_return_20_vs_sector",
            0.0,
            "stock_relative_strength_not_positive",
        ),
        ("close_to_ma20_ratio", 1.0, "stock_not_above_ma20"),
        ("ma20_slope_5", 0.0, "ma20_slope_not_positive"),
        ("up_down_volume_ratio_20", 0.99, "up_down_volume_ratio_too_low"),
        ("research_eligible", False, "research_ineligible"),
    ],
)
def test_each_stock_gate_fails_independently(
    field: str,
    value: object,
    reason: str,
) -> None:
    result = evaluate_s5_stock(_stock(**{field: value}), evaluate_s5_sector(_sector()))

    assert result.state is S5State.INELIGIBLE
    assert reason in result.reasons


def test_missing_stock_feature_is_unknown() -> None:
    result = evaluate_s5_stock(
        _stock(ma20_slope_5=None),
        evaluate_s5_sector(_sector()),
    )

    assert result.state is S5State.UNKNOWN
    assert result.reasons == ("missing_ma20_slope_5",)


@pytest.mark.parametrize(
    "observation",
    [
        lambda: _sector(drawdown_from_120d_high=float("nan")),
        lambda: _sector(relative_return_20=float("inf")),
        lambda: _sector(new_low_rate_20=1.1),
        lambda: _stock(close_to_ma20_ratio=0.0),
        lambda: _stock(up_down_volume_ratio_20=-0.1),
        lambda: _stock(ma20_slope_5=float("nan")),
    ],
)
def test_invalid_numeric_inputs_fail_closed(observation: object) -> None:
    with pytest.raises(ValueError):
        observation()  # type: ignore[operator]


@pytest.mark.parametrize("count", [0, 1, 5, 20, 25])
def test_dynamic_n_and_residual_cash(count: int) -> None:
    stocks = tuple(
        _stock(
            f"{index:06d}.SZ",
            relative_return_20_vs_sector=0.001 + index / 10_000,
        )
        for index in range(1, count + 1)
    )

    decision = build_s5_decision(as_of=AS_OF, sectors=(_sector(),), stocks=stocks)
    selected = min(count, 20)

    assert len(decision.selected_ids) == selected
    assert len(decision.target.positions) == selected
    assert all(position.target_weight == 0.04 for position in decision.target.positions)
    assert decision.target.cash_weight == pytest.approx(1.0 - selected * 0.04)
    assert decision.target.gross_exposure == pytest.approx(selected * 0.04)


def test_no_eligible_stock_is_explicit_all_cash() -> None:
    decision = build_s5_decision(
        as_of=AS_OF,
        sectors=(_sector(relative_return_20=-0.01),),
        stocks=(_stock(),),
    )

    assert decision.selected_ids == ()
    assert decision.target.positions == ()
    assert decision.target.cash_weight == 1.0
    assert decision.research_only is True
    assert decision.performance_claim is False
    assert decision.broker_order_authority is False


def test_deterministic_ranking_uses_relative_strength_volume_then_id() -> None:
    stocks = (
        _stock("000003.SZ", relative_return_20_vs_sector=0.05, up_down_volume_ratio_20=1.3),
        _stock("000002.SZ", relative_return_20_vs_sector=0.05, up_down_volume_ratio_20=1.3),
        _stock("000001.SZ", relative_return_20_vs_sector=0.05, up_down_volume_ratio_20=1.5),
        _stock("000004.SZ", relative_return_20_vs_sector=0.04, up_down_volume_ratio_20=3.0),
    )

    first = build_s5_decision(as_of=AS_OF, sectors=(_sector(),), stocks=stocks)
    second = build_s5_decision(
        as_of=AS_OF,
        sectors=(_sector(),),
        stocks=tuple(reversed(stocks)),
    )

    expected = ("000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ")
    assert first.selected_ids == expected
    assert second.selected_ids == expected
    assert first == second


def test_unobserved_sector_returns_unknown_stock_result() -> None:
    decision = build_s5_decision(
        as_of=AS_OF,
        sectors=(),
        stocks=(_stock(sector_id="UNKNOWN_SECTOR"),),
    )

    assert decision.stock_results[0].state is S5State.UNKNOWN
    assert decision.stock_results[0].reasons == ("sector_observation_missing",)
    assert decision.target.cash_weight == 1.0


def test_mismatched_observation_date_is_rejected() -> None:
    with pytest.raises(ValueError, match="sector observation as_of"):
        build_s5_decision(
            as_of=AS_OF,
            sectors=(_sector(as_of=AS_OF + timedelta(days=1)),),
            stocks=(),
        )

    with pytest.raises(ValueError, match="stock observation as_of"):
        build_s5_decision(
            as_of=AS_OF,
            sectors=(_sector(),),
            stocks=(_stock(as_of=AS_OF + timedelta(days=1)),),
        )


def test_duplicates_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate sector_id"):
        build_s5_decision(as_of=AS_OF, sectors=(_sector(), _sector()), stocks=())

    with pytest.raises(ValueError, match="duplicate instrument_id"):
        build_s5_decision(
            as_of=AS_OF,
            sectors=(_sector(),),
            stocks=(_stock(), _stock()),
        )


def test_config_cannot_over_allocate_or_accept_nonfinite_thresholds() -> None:
    with pytest.raises(ValueError, match="cannot exceed 1"):
        S5Config(max_names=30, weight_per_name=0.04)
    with pytest.raises(ValueError, match="must be finite"):
        S5Config(min_ma20_slope_5=float("inf"))


def test_build_does_not_mutate_caller_observations() -> None:
    sectors = (_sector(),)
    stocks = (_stock(),)
    sectors_before = tuple(sectors)
    stocks_before = tuple(stocks)

    build_s5_decision(as_of=AS_OF, sectors=sectors, stocks=stocks)

    assert sectors == sectors_before
    assert stocks == stocks_before
