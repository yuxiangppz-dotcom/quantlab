from __future__ import annotations

import math
import statistics
from dataclasses import replace
from datetime import date, timedelta

import pytest

from quantlab.research.s5_materialization import (
    S5EligibilityEvidence,
    S5MembershipEvidence,
    S5ResearchSeries,
    S5SectorSeriesInput,
    S5SeriesPoint,
    S5StockSeriesInput,
    materialize_s5_inputs,
)

START = date(2025, 1, 1)
SESSIONS = tuple(START + timedelta(days=index) for index in range(272))
AS_OF = SESSIONS[-1]


def _wave_prices(base: float, drift: float, period: float) -> list[float]:
    return [base + drift * index + 2.0 * math.sin(index / period) for index in range(272)]


BENCHMARK_VALUES = _wave_prices(100.0, 0.03, 9.0)
SECTOR_VALUES = _wave_prices(80.0, 0.025, 7.0)
STOCK_VALUES = _wave_prices(50.0, 0.04, 5.0)
VOLUME_VALUES = [1000.0 + (index % 7) * 75.0 for index in range(272)]


def _series(
    identity: str,
    values: list[float],
    *,
    source: str,
    sessions: tuple[date, ...] = SESSIONS,
) -> S5ResearchSeries:
    return S5ResearchSeries(
        series_id=identity,
        source_id=source,
        points=tuple(
            S5SeriesPoint(trade_date=trade_date, value=value)
            for trade_date, value in zip(sessions, values, strict=True)
        ),
    )


def _membership(
    sector_id: str = "POWER",
    *,
    source: str = "membership_v1",
    verified: bool = True,
    start: date = SESSIONS[0],
    end: date | None = None,
) -> S5MembershipEvidence:
    return S5MembershipEvidence(
        instrument_id="000001.SZ",
        sector_id=sector_id,
        effective_from=start,
        effective_to=end,
        source_id=source,
        pit_verified=verified,
    )


def _eligibility(
    *,
    as_of: date = AS_OF,
    eligible: bool | None = True,
    verified: bool = True,
    source: str = "universe_v1",
) -> S5EligibilityEvidence:
    return S5EligibilityEvidence(
        instrument_id="000001.SZ",
        as_of=as_of,
        eligible=eligible,
        source_id=source,
        pit_verified=verified,
    )


def _sector_input(
    values: list[float] | None = None,
    *,
    source: str = "sector_close_v1",
) -> S5SectorSeriesInput:
    return S5SectorSeriesInput(
        sector_id="POWER",
        closes=_series("POWER", values or SECTOR_VALUES, source=source),
    )


def _stock_input(
    *,
    close_values: list[float] | None = None,
    volume_values: list[float] | None = None,
    memberships: tuple[S5MembershipEvidence, ...] | None = None,
    eligibility: S5EligibilityEvidence | None | object = ...,
) -> S5StockSeriesInput:
    eligibility_value = _eligibility() if eligibility is ... else eligibility
    return S5StockSeriesInput(
        instrument_id="000001.SZ",
        sector_id="POWER",
        closes=_series("000001.SZ", close_values or STOCK_VALUES, source="stock_close_v1"),
        volumes=_series(
            "000001.SZ",
            volume_values or VOLUME_VALUES,
            source="stock_volume_v1",
        ),
        membership_evidence=memberships if memberships is not None else (_membership(),),
        eligibility_evidence=eligibility_value,  # type: ignore[arg-type]
    )


def _materialize(
    *,
    sessions: tuple[date, ...] = SESSIONS,
    benchmark: S5ResearchSeries | None = None,
    sectors: tuple[S5SectorSeriesInput, ...] | None = None,
    stocks: tuple[S5StockSeriesInput, ...] | None = None,
):
    return materialize_s5_inputs(
        as_of=sessions[-1],
        sessions=sessions,
        benchmark_closes=benchmark
        or _series("000985.SH", BENCHMARK_VALUES, source="benchmark_v1"),
        sectors=sectors if sectors is not None else (_sector_input(),),
        stocks=stocks if stocks is not None else (_stock_input(),),
    )


def test_materializes_frozen_s5_formula_contract() -> None:
    result = _materialize()
    sector = result.sector_observations[0]
    stock = result.stock_observations[0]

    expected_drawdown = SECTOR_VALUES[-1] / max(SECTOR_VALUES[-120:]) - 1.0
    expected_sector_return = SECTOR_VALUES[-1] / SECTOR_VALUES[-21] - 1.0
    expected_benchmark_return = BENCHMARK_VALUES[-1] / BENCHMARK_VALUES[-21] - 1.0
    expected_stock_return = STOCK_VALUES[-1] / STOCK_VALUES[-21] - 1.0
    expected_ma20 = statistics.fmean(STOCK_VALUES[-20:])
    expected_old_ma20 = statistics.fmean(STOCK_VALUES[-25:-5])

    endpoint_returns = [
        STOCK_VALUES[index] / STOCK_VALUES[index - 1] - 1.0
        for index in range(len(STOCK_VALUES) - 20, len(STOCK_VALUES))
    ]
    endpoint_volumes = VOLUME_VALUES[-20:]
    positive = [
        volume
        for daily_return, volume in zip(endpoint_returns, endpoint_volumes, strict=True)
        if daily_return > 0.0
    ]
    negative = [
        volume
        for daily_return, volume in zip(endpoint_returns, endpoint_volumes, strict=True)
        if daily_return < 0.0
    ]

    assert sector.drawdown_from_120d_high == pytest.approx(expected_drawdown)
    assert sector.relative_return_20 == pytest.approx(
        expected_sector_return - expected_benchmark_return
    )
    assert sector.new_low_rate_20 is not None
    assert sector.volatility_percentile_252 is not None
    assert sector.history_complete is True

    assert stock.membership_verified is True
    assert stock.research_eligible is True
    assert stock.relative_return_20_vs_sector == pytest.approx(
        expected_stock_return - expected_sector_return
    )
    assert stock.close_to_ma20_ratio == pytest.approx(STOCK_VALUES[-1] / expected_ma20)
    assert stock.ma20_slope_5 == pytest.approx(expected_ma20 / expected_old_ma20 - 1.0)
    assert stock.up_down_volume_ratio_20 == pytest.approx(
        statistics.fmean(positive) / statistics.fmean(negative)
    )
    assert len(result.fingerprint) == 64
    assert result.performance_claim is False
    assert result.broker_order_authority is False


def test_new_low_rate_counts_only_last_20_endpoints_and_includes_ties() -> None:
    flat = [100.0] * 272
    result = _materialize(
        benchmark=_series("000985.SH", flat, source="benchmark_flat"),
        sectors=(_sector_input(flat, source="sector_flat"),),
        stocks=(),
    )

    sector = result.sector_observations[0]
    assert sector.new_low_rate_20 == 1.0
    assert sector.volatility_percentile_252 == 1.0
    assert sector.relative_return_20 == 0.0
    assert sector.history_complete is True


def test_exact_272_close_boundary_is_required_for_volatility_percentile() -> None:
    short_sessions = SESSIONS[1:]
    sector_values = SECTOR_VALUES[1:]
    benchmark_values = BENCHMARK_VALUES[1:]
    result = _materialize(
        sessions=short_sessions,
        benchmark=_series(
            "000985.SH", benchmark_values, source="benchmark_short", sessions=short_sessions
        ),
        sectors=(
            S5SectorSeriesInput(
                "POWER",
                _series("POWER", sector_values, source="sector_short", sessions=short_sessions),
            ),
        ),
        stocks=(),
    )

    sector = result.sector_observations[0]
    assert sector.volatility_percentile_252 is None
    assert sector.history_complete is False
    assert any(
        issue.field == "volatility_percentile_252"
        and issue.reason == "insufficient_history"
        for issue in result.issues
    )


def test_missing_or_nonpositive_required_price_stays_unknown() -> None:
    missing_points = tuple(
        S5SeriesPoint(day, value)
        for day, value in zip(SESSIONS, SECTOR_VALUES, strict=True)
        if day != AS_OF
    )
    missing_sector = S5SectorSeriesInput(
        "POWER",
        S5ResearchSeries("POWER", "sector_missing", missing_points),
    )
    result = _materialize(sectors=(missing_sector,), stocks=())
    sector = result.sector_observations[0]
    assert sector.drawdown_from_120d_high is None
    assert sector.relative_return_20 is None
    assert sector.history_complete is False

    zero_values = list(SECTOR_VALUES)
    zero_values[-1] = 0.0
    zero_result = _materialize(
        sectors=(_sector_input(zero_values, source="sector_zero"),),
        stocks=(),
    )
    zero_sector = zero_result.sector_observations[0]
    assert zero_sector.drawdown_from_120d_high is None
    assert zero_sector.new_low_rate_20 is None
    assert zero_sector.volatility_percentile_252 is None


def test_one_sided_stock_return_window_keeps_volume_ratio_unknown() -> None:
    strictly_rising = [50.0 + index for index in range(272)]
    result = _materialize(stocks=(_stock_input(close_values=strictly_rising),))

    stock = result.stock_observations[0]
    assert stock.up_down_volume_ratio_20 is None
    assert any(
        issue.field == "up_down_volume_ratio_20"
        and issue.reason == "one_sided_return_window"
        for issue in result.issues
    )


def test_membership_is_true_false_or_unknown_from_pit_evidence() -> None:
    verified = _materialize(stocks=(_stock_input(memberships=(_membership(),)),))
    assert verified.stock_observations[0].membership_verified is True

    other_sector = _materialize(
        stocks=(_stock_input(memberships=(_membership("ENERGY"),)),)
    )
    assert other_sector.stock_observations[0].membership_verified is False

    unverified = _materialize(
        stocks=(_stock_input(memberships=(_membership(verified=False),)),)
    )
    assert unverified.stock_observations[0].membership_verified is None

    conflicting = _materialize(
        stocks=(
            _stock_input(
                memberships=(_membership("POWER"), _membership("ENERGY", source="other"))
            ),
        )
    )
    assert conflicting.stock_observations[0].membership_verified is None

    absent = _materialize(stocks=(_stock_input(memberships=()),))
    assert absent.stock_observations[0].membership_verified is None


def test_membership_effective_interval_switch_is_respected() -> None:
    old = _membership(end=AS_OF - timedelta(days=1), source="old")
    current = _membership("ENERGY", start=AS_OF, source="current")
    result = _materialize(stocks=(_stock_input(memberships=(old, current)),))

    assert result.stock_observations[0].membership_verified is False


def test_eligibility_requires_exact_date_and_pit_verification() -> None:
    wrong_date = _materialize(
        stocks=(_stock_input(eligibility=_eligibility(as_of=AS_OF - timedelta(days=1))),)
    )
    assert wrong_date.stock_observations[0].research_eligible is None

    unverified = _materialize(
        stocks=(_stock_input(eligibility=_eligibility(verified=False)),)
    )
    assert unverified.stock_observations[0].research_eligible is None

    explicit_false = _materialize(
        stocks=(_stock_input(eligibility=_eligibility(eligible=False)),)
    )
    assert explicit_false.stock_observations[0].research_eligible is False


def test_future_points_and_future_membership_cannot_change_prior_result_or_fingerprint() -> None:
    base = _materialize()
    future_day = AS_OF + timedelta(days=10)

    def with_future(series: S5ResearchSeries, value: float) -> S5ResearchSeries:
        return replace(
            series,
            points=series.points + (S5SeriesPoint(future_day, value),),
        )

    benchmark = with_future(
        _series("000985.SH", BENCHMARK_VALUES, source="benchmark_v1"),
        999999.0,
    )
    sector_series = with_future(
        _series("POWER", SECTOR_VALUES, source="sector_close_v1"),
        -1.0,
    )
    stock = _stock_input(
        memberships=(
            _membership(),
            _membership("FUTURE", start=future_day, source="future_membership"),
        )
    )
    stock = replace(
        stock,
        closes=with_future(stock.closes, 0.0),
        volumes=with_future(stock.volumes, -100.0),
    )
    changed = _materialize(
        benchmark=benchmark,
        sectors=(S5SectorSeriesInput("POWER", sector_series),),
        stocks=(stock,),
    )

    assert changed.sector_observations == base.sector_observations
    assert changed.stock_observations == base.stock_observations
    assert changed.issues == base.issues
    assert changed.fingerprint == base.fingerprint


def test_historical_source_or_membership_drift_changes_fingerprint() -> None:
    base = _materialize()
    changed_source = _materialize(
        sectors=(_sector_input(source="sector_close_revised"),)
    )
    changed_membership = _materialize(
        stocks=(
            _stock_input(
                memberships=(_membership(source="membership_revised"),),
            ),
        )
    )

    assert changed_source.fingerprint != base.fingerprint
    assert changed_membership.fingerprint != base.fingerprint


def test_point_and_input_order_do_not_change_materialization() -> None:
    benchmark = _series("000985.SH", BENCHMARK_VALUES, source="benchmark_v1")
    sector = _sector_input()
    stock = _stock_input()
    reversed_result = _materialize(
        benchmark=replace(benchmark, points=tuple(reversed(benchmark.points))),
        sectors=(replace(sector, closes=replace(sector.closes, points=tuple(reversed(sector.closes.points)))),),
        stocks=(
            replace(
                stock,
                closes=replace(stock.closes, points=tuple(reversed(stock.closes.points))),
                volumes=replace(stock.volumes, points=tuple(reversed(stock.volumes.points))),
                membership_evidence=tuple(reversed(stock.membership_evidence)),
            ),
        ),
    )

    assert reversed_result == _materialize()


def test_duplicate_consumed_series_date_is_rejected() -> None:
    sector = _sector_input()
    duplicate = replace(
        sector.closes,
        points=sector.closes.points + (S5SeriesPoint(AS_OF, SECTOR_VALUES[-1]),),
    )
    with pytest.raises(ValueError, match="duplicate series date"):
        _materialize(sectors=(S5SectorSeriesInput("POWER", duplicate),), stocks=())


def test_calendar_must_be_strict_and_end_at_as_of() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        materialize_s5_inputs(
            as_of=AS_OF,
            sessions=(AS_OF, AS_OF),
            benchmark_closes=_series("000985.SH", BENCHMARK_VALUES, source="benchmark_v1"),
            sectors=(),
            stocks=(),
        )

    with pytest.raises(ValueError, match="end exactly at as_of"):
        materialize_s5_inputs(
            as_of=AS_OF,
            sessions=SESSIONS[:-1],
            benchmark_closes=_series("000985.SH", BENCHMARK_VALUES, source="benchmark_v1"),
            sectors=(),
            stocks=(),
        )
