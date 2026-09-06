"""Synthetic-data tests for benchmark alignment and attribution (v0)."""

import importlib.util
import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quantlab.backtest.benchmark import (
    align_returns,
    compare_benchmark,
    cross_sectional_equal_weight_return_diagnostic,
    index_benchmark_coverage,
    returns_from_closes,
    strategy_daily_returns,
)
from quantlab.backtest.models import DailyBacktestRecord

START = date(2024, 1, 1)


def _load_research_runner():
    path = Path(__file__).parents[2] / "scripts" / "run_research_backtest.py"
    spec = importlib.util.spec_from_file_location("research_backtest_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(trade_date, ret_net, ret_gross=None) -> DailyBacktestRecord:
    """A minimal synthetic record; nav values are consistent with returns."""
    return DailyBacktestRecord(
        trade_date=trade_date,
        nav_gross=1.0 + ret_gross if ret_gross is not None else 1.0 + ret_net,
        nav_net=1.0 + ret_net,
        daily_return_gross=ret_gross if ret_gross is not None else ret_net,
        daily_return_net=ret_net,
        gross_exposure=0.98,
        net_exposure=0.98,
        cash_weight=0.02,
        turnover=0.2,
        traded_notional_ratio=0.2,
        transaction_cost=0.0002,
        holdings_count=180,
        gross_book_gross_exposure=1.0,
        gross_book_net_exposure=1.0,
        gross_book_cash_weight=0.0,
        gross_book_turnover=0.2,
        gross_book_traded_notional_ratio=0.2,
        gross_book_holdings_count=180,
    )


def _records(returns) -> list[DailyBacktestRecord]:
    # records[0] is the establishment period and is excluded by convention
    return [_record(START, 0.0)] + [
        _record(START + timedelta(days=i + 1), ret) for i, ret in enumerate(returns)
    ]


def _benchmark_dates(n):
    return {START + timedelta(days=i + 1) for i in range(n)}


# ---------------------------------------------------------------- strategy --


def test_strategy_daily_returns_skips_first_record() -> None:
    records = _records([0.01, 0.02])
    series = strategy_daily_returns(records)
    assert set(series) == {
        START + timedelta(days=1),
        START + timedelta(days=2),
    }
    assert series[START + timedelta(days=1)] == 0.01


def test_strategy_daily_returns_selects_book() -> None:
    records = [_record(START, 0.0), _record(START, 0.01, ret_gross=0.02)]
    assert strategy_daily_returns(records, book="gross")[START] == 0.02
    assert strategy_daily_returns(records, book="net")[START] == 0.01
    with pytest.raises(ValueError):
        strategy_daily_returns(records, book="fx")


# ------------------------------------------------------------------ closes --


def test_returns_from_closes_basic() -> None:
    closes = [
        (date(2024, 1, 1), 100.0),
        (date(2024, 1, 2), 101.0),
        (date(2024, 1, 3), 99.0),
    ]
    returns = returns_from_closes(closes)
    assert math.isclose(returns[date(2024, 1, 2)], 0.01)
    assert math.isclose(returns[date(2024, 1, 3)], 99.0 / 101.0 - 1)
    assert date(2024, 1, 1) not in returns


def test_returns_from_closes_rejects_duplicate_dates() -> None:
    closes = [(date(2024, 1, 1), 100.0), (date(2024, 1, 1), 101.0)]
    with pytest.raises(ValueError, match="Duplicate"):
        returns_from_closes(closes)


def test_returns_from_closes_rejects_nonpositive_close() -> None:
    closes = [(date(2024, 1, 1), 100.0), (date(2024, 1, 2), 0.0)]
    with pytest.raises(ValueError, match="Invalid close"):
        returns_from_closes(closes)


# ------------------------------------------------------- equal-weight (PIT) --


def _universe_frame(rows) -> pd.DataFrame:
    # rows: (instrument_id, date, adj_close)
    return pd.DataFrame(rows, columns=["instrument_id", "trade_date", "adj_close"])


def test_equal_weight_returns_basic_mean() -> None:
    day0, day1, day2 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
    frame = _universe_frame(
        [
            ("A", day0, 100.0), ("A", day1, 110.0), ("A", day2, 99.0),
            ("B", day0, 50.0), ("B", day1, 55.0), ("B", day2, 60.5),
        ]
    )
    returns = cross_sectional_equal_weight_return_diagnostic(frame)
    # day1: A +10%, B +10% -> 10%; day2: A -10%, B +10% -> 0%
    assert math.isclose(returns[day1], 0.10, rel_tol=1e-12)
    assert math.isclose(returns[day2], 0.0, abs_tol=1e-12)
    assert day0 not in returns  # no prior close -> no fabricated first return


def test_equal_weight_pit_delisting_stops_contribution() -> None:
    day0, day1, day2 = date(2024, 1, 1), date(2024, 1, 2), date(2024, 1, 3)
    # "B" is delisted after day1: it has no row from day2 onward (point-in-time
    # window already applied); it must contribute nothing on day2.
    frame = _universe_frame(
        [
            ("A", day0, 100.0), ("A", day1, 110.0), ("A", day2, 110.0),
            ("B", day0, 50.0), ("B", day1, 55.0),
        ]
    )
    returns = cross_sectional_equal_weight_return_diagnostic(frame)
    assert math.isclose(returns[day1], 0.10)  # both contribute on day1
    assert math.isclose(returns[day2], 0.0)  # A flat; B does not drag or boost
    # explicitly: day2 mean is over {A} only, not {A, B-with-filled-price}
    contrib_day2 = frame[frame["trade_date"] == day2]["instrument_id"].tolist()
    assert contrib_day2 == ["A"]


def test_equal_weight_no_fill_across_suspension() -> None:
    day0, day2 = date(2024, 1, 1), date(2024, 1, 3)
    # B's rows skip day1 entirely (suspended): no synthetic day1 value
    frame = _universe_frame(
        [
            ("A", day0, 100.0), ("A", day2, 100.0),
            ("B", day0, 50.0), ("B", day2, 52.5),
        ]
    )
    returns = cross_sectional_equal_weight_return_diagnostic(frame)
    assert date(2024, 1, 2) not in returns
    # the two-session return of B (5%) is averaged with A's (0%) on day2
    assert math.isclose(returns[day2], 0.025)


def test_equal_weight_rejects_duplicate_and_missing_columns() -> None:
    day0 = date(2024, 1, 1)
    frame = _universe_frame(
        [("A", day0, 100.0), ("A", day0, 101.0)]
    )
    with pytest.raises(ValueError, match="duplicate"):
        cross_sectional_equal_weight_return_diagnostic(frame)
    with pytest.raises(ValueError, match="missing columns"):
        cross_sectional_equal_weight_return_diagnostic(
            pd.DataFrame([{"instrument_id": "A"}])
        )


# ------------------------------------------------------------------ align --


def test_align_drops_missing_benchmark_dates_without_fill() -> None:
    strategy = {
        date(2024, 1, 2): 0.01,
        date(2024, 1, 3): 0.02,
        date(2024, 1, 4): -0.01,
    }
    # benchmark has no value for 1/3: that date must be dropped, never filled
    benchmark = {
        date(2024, 1, 2): 0.005,
        date(2024, 1, 4): -0.005,
        date(2024, 1, 5): 0.003,  # benchmark-only date is ignored
    }
    aligned = align_returns(strategy, benchmark)
    assert [d for d, _, _ in aligned] == [date(2024, 1, 2), date(2024, 1, 4)]
    assert aligned[0] == (date(2024, 1, 2), 0.01, 0.005)
    assert aligned[1] == (date(2024, 1, 4), -0.01, -0.005)


# ---------------------------------------------------------------- compare --


def _known_parameter_inputs():
    """s = 0.001 + 1.5 * b exactly -> OLS slope/intercept are exact."""
    b = [0.01, -0.02, 0.03, 0.05, -0.01, 0.04, 0.02, -0.03]
    s = [0.001 + 1.5 * value for value in b]
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    records = _records(s)
    active = [s_value - b_value for s_value, b_value in zip(s, b, strict=True)]
    mean_active = sum(active) / len(active)
    var_active = sum((v - mean_active) ** 2 for v in active) / len(active)
    te_annual = math.sqrt(var_active) * math.sqrt(252)
    ir_annual = mean_active / math.sqrt(var_active) * math.sqrt(252)
    return records, benchmark, te_annual, ir_annual


def test_compare_known_parameters() -> None:
    records, benchmark, te_annual, ir_annual = _known_parameter_inputs()
    stats = compare_benchmark(records, "test_bm", benchmark, annualization=252)
    assert stats.n_obs == 8
    assert stats.beta == pytest.approx(1.5, abs=1e-12)
    assert stats.alpha_daily == pytest.approx(0.001, abs=1e-12)
    assert stats.alpha_annualized == pytest.approx(0.252, abs=1e-12)
    assert stats.correlation == pytest.approx(1.0, abs=1e-12)
    assert stats.tracking_error == pytest.approx(te_annual, rel=1e-12)
    assert stats.information_ratio == pytest.approx(ir_annual, rel=1e-12)


def test_compare_cumulative_and_active_cagr() -> None:
    b = [0.01, -0.02, 0.03, 0.05, -0.01, 0.04, 0.02, -0.03]
    s = [0.001 + 1.5 * value for value in b]
    records = _records(s)
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(records, "test_bm", benchmark)
    cum_strategy = math.prod(1 + value for value in s)
    cum_benchmark = math.prod(1 + value for value in b)
    assert stats.strategy_total_return == pytest.approx(cum_strategy - 1, rel=1e-12)
    assert stats.benchmark_total_return == pytest.approx(cum_benchmark - 1, rel=1e-12)
    assert stats.cumulative_active_return == pytest.approx(
        cum_strategy / cum_benchmark - 1, rel=1e-12
    )
    assert stats.active_cagr == pytest.approx(
        (cum_strategy / cum_benchmark) ** (252 / 8) - 1, rel=1e-12
    )
    assert stats.strategy_cagr == pytest.approx(
        cum_strategy ** (252 / 8) - 1, rel=1e-12
    )
    assert stats.benchmark_cagr == pytest.approx(
        cum_benchmark ** (252 / 8) - 1, rel=1e-12
    )
    assert stats.first_date == START + timedelta(days=1)
    assert stats.last_date == START + timedelta(days=8)


def test_compare_drops_dates_missing_from_benchmark() -> None:
    # strategy has 4 return dates, benchmark misses one -> n_obs = 3
    s = [0.01, 0.02, -0.01, 0.005]
    b = [0.005, 0.01, 0.0, 0.0]
    benchmark = {
        START + timedelta(days=1): b[0],
        # day 2 missing
        START + timedelta(days=3): b[2],
        START + timedelta(days=4): b[3],
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    assert stats.n_obs == 3
    # exact OLS on the aligned subset: s = b + [0.005, -0.01, 0.005]
    # slope/intercept recomputed independently below
    s_aligned = [s[0], s[2], s[3]]
    b_aligned = [b[0], b[2], b[3]]
    mean_b = sum(b_aligned) / 3
    mean_s = sum(s_aligned) / 3
    cov = sum(
        (x - mean_b) * (y - mean_s) for x, y in zip(b_aligned, s_aligned, strict=True)
    ) / 3
    var = sum((x - mean_b) ** 2 for x in b_aligned) / 3
    assert stats.beta == pytest.approx(cov / var, rel=1e-12)
    assert stats.alpha_daily == pytest.approx(mean_s - (cov / var) * mean_b, rel=1e-12)


def test_compare_insufficient_observations() -> None:
    empty = compare_benchmark(_records([]), "test_bm", _benchmark_dates(0))
    assert empty.n_obs == 0
    assert math.isnan(empty.beta)
    assert math.isnan(empty.tracking_error)

    single = compare_benchmark(
        _records([0.01]), "test_bm", {START + timedelta(days=1): 0.005}
    )
    assert single.n_obs == 1
    assert single.strategy_total_return == pytest.approx(0.01)
    assert single.benchmark_total_return == pytest.approx(0.005)
    assert math.isnan(single.beta)
    assert math.isnan(single.information_ratio)
    assert math.isnan(single.correlation)


def test_compare_flat_active_series_gives_zero_te_undefined_ir() -> None:
    s = [0.0, 0.0, 0.0]
    b = [0.0, 0.0, 0.0]  # active is exactly constant zero
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    assert stats.tracking_error == pytest.approx(0.0, abs=1e-15)
    assert math.isnan(stats.information_ratio)  # 0/0 undefined; not silently 0


def test_compare_to_dict_is_json_ready() -> None:
    s = [0.01, 0.02]
    b = [0.005, 0.015]
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    payload = stats.to_dict()
    assert payload["benchmark"] == "test_bm"
    assert payload["book"] == "net"
    assert payload["n_obs"] == 2


def test_active_max_drawdown_known_value() -> None:
    # active = [0.1, -0.2]: active NAV 1.0 -> 1.1 -> 0.88, so the active
    # drawdown is 0.88/1.1 - 1 = -0.2 exactly, regardless of the benchmark
    b = [0.01, -0.01]
    a = [0.1, -0.2]
    s = [b_value + a_value for b_value, a_value in zip(b, a, strict=True)]
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    # formal active MDD uses the RELATIVE wealth path:
    #   r1 = 1.11 / 1.01, r2 = (1.11 * 0.79) / (1.01 * 0.99)
    #   dd = r2 / r1 - 1  (documented formula, not re-running the impl)
    expected = ((1.11 * 0.79) / (1.01 * 0.99)) / (1.11 / 1.01) - 1.0
    assert stats.active_max_drawdown == pytest.approx(expected, rel=1e-12)
    # arithmetic path prod(1 + s - b) is a separate diagnostic
    assert stats.arithmetic_active_nav_diagnostic == pytest.approx(0.88, rel=1e-12)
    assert stats.relative_active_nav_final == pytest.approx(
        (1.11 * 0.79) / (1.01 * 0.99), rel=1e-12
    )


def test_active_max_drawdown_zero_when_active_never_dips() -> None:
    b = [0.01, -0.01, 0.02]
    s = [value + 0.001 for value in b]  # strictly positive active returns
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    assert stats.active_max_drawdown == 0.0


def test_active_mdd_relative_and_arithmetic_paths_diverge_materially() -> None:
    # strategy flat, benchmark -50% then +150% then flat: large moves, NOT a
    # near-zero benchmark degenerate case. Relative wealth goes 2.0 -> 0.8 ->
    # 0.8, so the formal active MDD is -0.6, consistent with the -20%
    # cumulative active return. The arithmetic path prod(1 + s - b) goes
    # 1.5 -> -0.75 -> -0.75: it is not even a valid wealth process (negative
    # NAV), which is exactly why prod(1 + s - b) may only ever be a named
    # diagnostic, never the formal active NAV/MDD.
    b = [-0.5, 1.5, 0.0]
    s = [0.0, 0.0, 0.0]
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    stats = compare_benchmark(_records(s), "test_bm", benchmark)
    assert stats.active_max_drawdown == pytest.approx(-0.6, rel=1e-12)
    assert stats.relative_active_nav_final == pytest.approx(0.8, rel=1e-12)
    assert stats.cumulative_active_return == pytest.approx(-0.2, rel=1e-12)
    # consistency: cumulative active return is exactly final relative NAV - 1
    assert stats.cumulative_active_return == pytest.approx(
        stats.relative_active_nav_final - 1.0, rel=1e-12
    )
    assert stats.arithmetic_active_nav_diagnostic == pytest.approx(-0.75, rel=1e-12)


# ---------------------------------------------------------------- coverage --


def test_index_benchmark_coverage_complete_series() -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    closes = {day: 100.0 + i for i, day in enumerate(sessions)}
    coverage = index_benchmark_coverage("000300.SH", closes, sessions)
    assert coverage.complete is True
    assert coverage.expected_sessions == 3
    assert coverage.available_sessions == 3
    assert coverage.missing_sessions == ()


def test_index_benchmark_coverage_reports_missing_and_invalid_closes() -> None:
    sessions = [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    closes = {sessions[0]: 100.0, sessions[2]: float("nan")}
    coverage = index_benchmark_coverage("000300.SH", closes, sessions)
    assert coverage.complete is False
    assert coverage.available_sessions == 1
    assert coverage.missing_sessions == (sessions[1], sessions[2])


def test_attribution_fail_hard_on_incomplete_n_obs() -> None:
    # a missing benchmark session must raise, never silently shrink the
    # observation set of a formal attribution
    runner = _load_research_runner()
    s = [0.01, 0.02, -0.01]
    b = [0.005, 0.01, 0.0]
    benchmark = {
        START + timedelta(days=i + 1): value for i, value in enumerate(b)
    }
    run = SimpleNamespace(records=_records(s))
    # full series: passes
    result = runner._attribution_or_fail(
        run, "000300.SH", benchmark, annualization=252
    )
    assert result["n_obs"] == 3
    # one benchmark session missing: n_obs drops to 2 -> hard failure
    partial = {k: v for k, v in benchmark.items() if k != START + timedelta(days=2)}
    with pytest.raises(RuntimeError, match="n_obs"):
        runner._attribution_or_fail(
            run, "000300.SH", partial, annualization=252
        )
