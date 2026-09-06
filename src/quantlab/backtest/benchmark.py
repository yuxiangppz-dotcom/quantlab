"""Benchmark alignment and active-return attribution (v0.1).

Answers "how much of the strategy is beta and how much is active return" for
one strategy return series against one benchmark return series.

Benchmark layers (kept distinct by the runner):

- ``equal_weight_v1_control``: a real self-financing portfolio run through
  the same engine (see :mod:`quantlab.portfolio.control`) — the PRIMARY
  stock-selection control.
- index benchmarks (000300.SH / 000905.SH / 000852.SH): market attribution
  on the raw price-index close (``index_return_basis = price_index_close``);
  raw index closes are NOT dividend-adjusted and are not claimed to be
  equivalent to adjusted-stock total returns.
- ``cross_sectional_equal_weight_return_diagnostic``: a cross-sectional mean
  of per-instrument consecutive returns. Useful diagnostic; NOT a portfolio.

Conventions (kept identical to :mod:`quantlab.backtest.metrics`):

- Strategy daily returns are taken from ``records[1:]`` (``len(records) - 1``
  return intervals), matching ``compute_metrics``.
- Variance / covariance use the population estimator (ddof=0).
- Risk-free rate is 0.0 (no rf series is introduced; see ``sharpe_rf`` in the
  accounting disclosure).
- Alignment is a strict inner join on actual record dates: a benchmark date
  missing from the strategy records, or a strategy date missing from the
  benchmark, drops that observation. Nothing is forward/backward filled.
- Alpha is a daily OLS regression ``s = alpha + beta * b``; the annualized
  alpha is the daily alpha scaled by the annualization factor (arithmetic),
  reported alongside the daily value.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from quantlab.backtest.models import DailyBacktestRecord


@dataclass(frozen=True)
class BenchmarkComparison:
    """Attribution statistics of one strategy series vs one benchmark."""

    benchmark: str
    book: str  # "net" | "gross"
    n_obs: int
    first_date: date | None
    last_date: date | None
    strategy_total_return: float
    benchmark_total_return: float
    cumulative_active_return: float
    strategy_cagr: float
    benchmark_cagr: float
    active_cagr: float
    tracking_error: float  # annualized, population std of daily active returns
    information_ratio: float  # annualized, rf = 0
    beta: float  # daily OLS slope
    alpha_daily: float  # daily OLS intercept
    alpha_annualized: float  # alpha_daily * annualization (arithmetic)
    correlation: float
    cumulative_strategy_nav: float  # prod(1 + s_t) over aligned dates
    cumulative_benchmark_nav: float  # prod(1 + b_t) over aligned dates
    relative_active_nav_final: float  # strategy NAV / benchmark NAV
    arithmetic_active_nav_diagnostic: float  # prod(1 + s_t - b_t), diagnostic only
    active_max_drawdown: float  # max drawdown of the RELATIVE wealth path

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class IndexBenchmarkCoverage:
    """Per-instrument completeness audit of an index benchmark series.

    A formal attribution is only allowed when ``complete`` is true: every
    expected market session must carry a valid close for the instrument.
    Missing rows are never lumped into later returns by silent alignment.
    """

    benchmark: str
    expected_sessions: int
    available_sessions: int
    missing_sessions: tuple[date, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_sessions

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "expected_sessions": self.expected_sessions,
            "available_sessions": self.available_sessions,
            "missing_sessions": [d.isoformat() for d in self.missing_sessions],
            "complete": self.complete,
        }


def index_benchmark_coverage(
    benchmark: str,
    closes: Mapping[date, float],
    expected_sessions: Sequence[date],
) -> IndexBenchmarkCoverage:
    """Audit close availability per expected session for one index.

    A close counts as available only when it is finite and positive; a
    non-finite or non-positive value is reported as a missing session rather
    than silently entering a return computation.
    """
    missing = tuple(
        day
        for day in sorted(set(expected_sessions))
        if not _valid_close(closes.get(day))
    )
    return IndexBenchmarkCoverage(
        benchmark=benchmark,
        expected_sessions=len(set(expected_sessions)),
        available_sessions=len(set(expected_sessions)) - len(missing),
        missing_sessions=missing,
    )


def _valid_close(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def strategy_daily_returns(
    records: list[DailyBacktestRecord], book: str = "net"
) -> dict[date, float]:
    """Return ``{trade_date: daily_return}`` from ``records[1:]``.

    The first record is the portfolio-establishment period and is excluded,
    matching ``compute_metrics``. ``book`` selects the net (``daily_return_net``)
    or gross (``daily_return_gross``) ledger.
    """
    if book == "net":
        return {r.trade_date: r.daily_return_net for r in records[1:]}
    if book == "gross":
        return {r.trade_date: r.daily_return_gross for r in records[1:]}
    raise ValueError(f"Unknown book: {book!r}")


def returns_from_closes(closes: list[tuple[date, float]]) -> dict[date, float]:
    """Convert a close-price series into per-session simple returns.

    Each return spans the two consecutive available points for the series
    (never filled, never shifted). The first point yields no return.
    """
    ordered = sorted(closes, key=lambda item: item[0])
    returns: dict[date, float] = {}
    for (prev_date, prev_close), (trade_date, close) in zip(
        ordered, ordered[1:], strict=False
    ):
        if prev_date == trade_date:
            raise ValueError(f"Duplicate close date: {trade_date}")
        if trade_date < prev_date:
            raise ValueError("close series must not be unsorted")
        if not math.isfinite(prev_close) or prev_close <= 0:
            raise ValueError(f"Invalid previous close on {prev_date}")
        if not math.isfinite(close) or close <= 0:
            raise ValueError(f"Invalid close on {trade_date}")
        returns[trade_date] = close / prev_close - 1.0
    return returns


def cross_sectional_equal_weight_return_diagnostic(
    frame: pd.DataFrame,
) -> dict[date, float]:
    """Cross-sectional equal-weight daily return DIAGNOSTIC of a universe.

    ``frame`` must have ``instrument_id``, ``trade_date``, ``adj_close`` and be
    already point-in-time filtered (rows restricted to each instrument's
    [list_date, delist_date] window). Each instrument contributes the simple
    return between its own consecutive available rows (a suspension therefore
    accumulates into the next available return; nothing is filled). A delisted
    instrument simply stops contributing after its last available row. The
    daily value is the mean of the contributing instruments' returns.

    This is NOT a self-financing equal-weight portfolio: suspended names
    vanish from the daily denominator and resume with a multi-day lump return,
    and no turnover or cost is ever charged. It is retained only as a
    cross-sectional diagnostic; the formal control portfolio is
    ``quantlab.portfolio.control.build_equal_weight_control_targets`` run
    through the backtest engine.
    """
    if frame.empty:
        return {}
    required = {"instrument_id", "trade_date", "adj_close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"frame missing columns: {sorted(missing)}")
    work = frame[["instrument_id", "trade_date", "adj_close"]].copy()
    work["trade_date"] = work["trade_date"].map(_as_date)
    work = work.sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
    if work.duplicated(subset=["instrument_id", "trade_date"]).any():
        raise ValueError("duplicate instrument_id + trade_date in universe frame")
    work["return_1d"] = work.groupby("instrument_id")["adj_close"].pct_change()
    contributing = work.dropna(subset=["return_1d"])
    daily = contributing.groupby("trade_date")["return_1d"].mean()
    return {day: float(value) for day, value in daily.items()}


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def align_returns(
    strategy: dict[date, float], benchmark: dict[date, float]
) -> list[tuple[date, float, float]]:
    """Strict inner join of two return series on the strategy dates.

    A benchmark-missing date is dropped (never filled); benchmark-only dates
    are ignored.
    """
    aligned: list[tuple[date, float, float]] = []
    for trade_date in sorted(strategy):
        benchmark_return = benchmark.get(trade_date)
        if benchmark_return is None:
            continue
        aligned.append((trade_date, strategy[trade_date], benchmark_return))
    return aligned


def _mean(values: list[float]) -> float:
    return math.fsum(values) / len(values)


def _centered(values: list[float], mean: float) -> list[float]:
    return [value - mean for value in values]


def compare_benchmark(
    records: list[DailyBacktestRecord],
    benchmark_name: str,
    benchmark_returns: dict[date, float],
    annualization: int = 252,
    book: str = "net",
) -> BenchmarkComparison:
    """Align ``records`` with one benchmark and compute attribution stats.

    All statistics are computed on the strictly aligned observation set; when
    fewer than 2 observations align, the distributional statistics are NaN.
    """
    strategy = strategy_daily_returns(records, book=book)
    aligned = align_returns(strategy, benchmark_returns)
    n = len(aligned)
    if n == 0:
        return BenchmarkComparison(
            benchmark=benchmark_name, book=book, n_obs=0,
            first_date=None, last_date=None,
            strategy_total_return=float("nan"),
            benchmark_total_return=float("nan"),
            cumulative_active_return=float("nan"),
            strategy_cagr=float("nan"),
            benchmark_cagr=float("nan"),
            active_cagr=float("nan"),
            tracking_error=float("nan"),
            information_ratio=float("nan"),
            beta=float("nan"),
            alpha_daily=float("nan"),
            alpha_annualized=float("nan"),
            correlation=float("nan"),
            cumulative_strategy_nav=float("nan"),
            cumulative_benchmark_nav=float("nan"),
            relative_active_nav_final=float("nan"),
            arithmetic_active_nav_diagnostic=float("nan"),
            active_max_drawdown=float("nan"),
        )

    dates = [d for d, _, _ in aligned]
    s = [s_ret for _, s_ret, _ in aligned]
    b = [b_ret for _, _, b_ret in aligned]
    active = [s_ret - b_ret for s_ret, b_ret in zip(s, b, strict=True)]

    cum_strategy = math.prod(1.0 + value for value in s)
    cum_benchmark = math.prod(1.0 + value for value in b)
    arithmetic_active_nav = math.prod(1.0 + value for value in active)
    strategy_total = cum_strategy - 1.0
    benchmark_total = cum_benchmark - 1.0
    cumulative_active = cum_strategy / cum_benchmark - 1.0
    strategy_cagr = cum_strategy ** (annualization / n) - 1.0
    benchmark_cagr = cum_benchmark ** (annualization / n) - 1.0
    active_cagr = (cum_strategy / cum_benchmark) ** (annualization / n) - 1.0

    # Formal active wealth path is RELATIVE wealth: strategy NAV / benchmark
    # NAV, the same ratio that defines cumulative_active_return and
    # active_cagr. prod(1 + s - b) is kept only as a clearly-named
    # arithmetic-active diagnostic because the two paths diverge materially
    # when benchmark moves are large.
    relative_nav = 1.0
    relative_peak = 1.0
    active_max_drawdown = 0.0
    for s_value, b_value in zip(s, b, strict=True):
        relative_nav *= (1.0 + s_value) / (1.0 + b_value)
        relative_peak = max(relative_peak, relative_nav)
        active_max_drawdown = min(
            active_max_drawdown, relative_nav / relative_peak - 1.0
        )

    mean_active = _mean(active)
    var_active = _mean([value * value for value in _centered(active, mean_active)])
    active_std = math.sqrt(var_active)
    tracking_error = active_std * math.sqrt(annualization)
    information_ratio = (
        mean_active / active_std * math.sqrt(annualization) if active_std > 0 else float("nan")
    )

    if n >= 2:
        mean_s = _mean(s)
        mean_b = _mean(b)
        cs = _centered(s, mean_s)
        cb = _centered(b, mean_b)
        cov_sb = _mean([a * c for a, c in zip(cs, cb, strict=True)])
        var_b = _mean([value * value for value in cb])
        var_s = _mean([value * value for value in cs])
        beta = cov_sb / var_b if var_b > 0 else float("nan")
        alpha_daily = mean_s - beta * mean_b if math.isfinite(beta) else float("nan")
        correlation = (
            cov_sb / math.sqrt(var_s * var_b) if var_s > 0 and var_b > 0 else float("nan")
        )
    else:
        beta = float("nan")
        alpha_daily = float("nan")
        correlation = float("nan")

    return BenchmarkComparison(
        benchmark=benchmark_name,
        book=book,
        n_obs=n,
        first_date=dates[0],
        last_date=dates[-1],
        strategy_total_return=strategy_total,
        benchmark_total_return=benchmark_total,
        cumulative_active_return=cumulative_active,
        strategy_cagr=strategy_cagr,
        benchmark_cagr=benchmark_cagr,
        active_cagr=active_cagr,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        beta=beta,
        alpha_daily=alpha_daily,
        alpha_annualized=(
            alpha_daily * annualization if math.isfinite(alpha_daily) else float("nan")
        ),
        correlation=correlation,
        cumulative_strategy_nav=cum_strategy,
        cumulative_benchmark_nav=cum_benchmark,
        relative_active_nav_final=cum_strategy / cum_benchmark,
        arithmetic_active_nav_diagnostic=arithmetic_active_nav,
        active_max_drawdown=active_max_drawdown,
    )
