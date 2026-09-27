"""PSR/DSR equations (Bailey & Lopez de Prado 2014), with explicit assumptions.

All Sharpe inputs are per observation, NEVER annualized. These asymptotic
statistics do not correct serial dependence or discover unregistered trials.
Kurtosis is Pearson kurtosis (normal = 3), not excess kurtosis.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist, mean, stdev
from typing import TypedDict


class SharpeMoments(TypedDict):
    observations: int
    sharpe_per_observation: float
    skewness: float
    pearson_kurtosis: float
    lag1_autocorrelation: float | None


def return_moments(returns: Sequence[float]) -> SharpeMoments:
    values = [float(v) for v in returns]
    if len(values) < 3 or not all(math.isfinite(v) and v > -1 for v in values):
        raise ValueError("at least three finite returns above -1 required")
    average = mean(values)
    centered = [v - average for v in values]
    m2 = mean([v**2 for v in centered])
    if m2 <= 0:
        raise ValueError("Sharpe undefined for constant returns")
    left, right = values[:-1], values[1:]
    a, b = mean(left), mean(right)
    denominator = math.sqrt(sum((v - a) ** 2 for v in left) * sum((v - b) ** 2 for v in right))
    autocorrelation = (
        sum((x - a) * (y - b) for x, y in zip(left, right, strict=True)) / denominator
        if denominator > 0
        else None
    )
    return {
        "observations": len(values),
        "sharpe_per_observation": average / stdev(values),
        "skewness": mean([v**3 for v in centered]) / m2**1.5,
        "pearson_kurtosis": mean([v**4 for v in centered]) / m2**2,
        "lag1_autocorrelation": autocorrelation,
    }


def probabilistic_sharpe(
    sharpe: float,
    observations: int,
    skewness: float,
    pearson_kurtosis: float,
    *,
    reference_sharpe: float = 0.0,
) -> float:
    if (
        type(observations) is not int
        or observations < 3
        or not all(math.isfinite(v) for v in (sharpe, skewness, pearson_kurtosis, reference_sharpe))
    ):
        raise ValueError("finite moments and at least three observations required")
    if pearson_kurtosis + 1e-12 < skewness**2 + 1:
        raise ValueError("inconsistent Pearson moments")
    variance = 1 - skewness * sharpe + (pearson_kurtosis - 1) * sharpe**2 / 4
    if variance <= 0:
        raise ValueError("nonpositive Sharpe estimator variance")
    z = (sharpe - reference_sharpe) * math.sqrt((observations - 1) / variance)
    return NormalDist().cdf(z)


def expected_maximum_sharpe(trials: int, trial_sharpe_std: float) -> float:
    if type(trials) is not int or trials < 1:
        raise ValueError("positive integer trial count required")
    if not math.isfinite(trial_sharpe_std) or trial_sharpe_std < 0:
        raise ValueError("finite nonnegative across-trial Sharpe std required")
    if trials == 1:
        return 0.0
    gamma = 0.5772156649015329
    normal = NormalDist()
    return trial_sharpe_std * (
        (1 - gamma) * normal.inv_cdf(1 - 1 / trials)
        + gamma * normal.inv_cdf(1 - 1 / (trials * math.e))
    )


def deflated_sharpe(
    sharpe: float,
    observations: int,
    skewness: float,
    pearson_kurtosis: float,
    *,
    trials: int,
    trial_sharpe_std: float,
) -> float:
    reference = expected_maximum_sharpe(trials, trial_sharpe_std)
    return probabilistic_sharpe(
        sharpe, observations, skewness, pearson_kurtosis, reference_sharpe=reference
    )
