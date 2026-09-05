"""Report construction and performance-validity determination."""

from __future__ import annotations

from datetime import date

from quantlab.backtest.metrics import compute_metrics
from quantlab.backtest.models import (
    RUN_MODE_STRICT,
    STATUS_COMPLETED,
    BacktestConfig,
    BacktestResult,
)


def build_report(
    strict_result: BacktestResult,
    diagnostic_result: BacktestResult | None,
    reproducible: bool,
    config: BacktestConfig,
    expected_sessions: list[date] | None = None,
) -> dict:
    """Determine what performance numbers may be published.

    Full ``metrics`` are produced only when **all** of the following hold:

    - ``strict_result`` is a strict run;
    - its status is ``completed``;
    - it has no ``accounting_error``, no blocking event, and no diagnostic region;
    - inputs are reproducible;
    - its records fully cover the expected trading sessions.

    Missing coverage evidence is never treated as complete.
    """
    reasons: list[str] = []
    if strict_result.run_mode != RUN_MODE_STRICT:
        reasons.append(f"run_mode={strict_result.run_mode} (not strict)")
    if strict_result.status != STATUS_COMPLETED:
        reasons.append(f"strict status={strict_result.status}")
    if strict_result.accounting_error is not None:
        reasons.append(f"accounting_error: {strict_result.accounting_error}")
    if strict_result.first_blocking_event is not None:
        reasons.append("unsupported lifecycle event blocked the run")
    if strict_result.diagnostic_from is not None:
        reasons.append("diagnostic region present")
    if not reproducible:
        reasons.append("input not reproducible")

    if expected_sessions is None:
        reasons.append("coverage evidence missing")
        covered = False
    else:
        expected = sorted(set(expected_sessions))
        actual = [r.trade_date for r in strict_result.records]
        covered = actual == expected
        if not covered:
            reasons.append(
                f"coverage incomplete: {len(actual)} records vs "
                f"{len(expected)} expected sessions"
            )

    full_valid = (
        strict_result.run_mode == RUN_MODE_STRICT
        and strict_result.status == STATUS_COMPLETED
        and strict_result.accounting_error is None
        and strict_result.first_blocking_event is None
        and strict_result.diagnostic_from is None
        and reproducible
        and covered
    )

    metrics = (
        compute_metrics(strict_result.records, strict_result.rebalances, config)
        if full_valid
        else None
    )

    diagnostic_metrics = None
    if diagnostic_result is not None and diagnostic_result.accounting_error is None:
        diagnostic_metrics = compute_metrics(
            diagnostic_result.records, diagnostic_result.rebalances, config
        )

    return {
        "performance_valid": full_valid,
        "metrics": metrics,
        "diagnostic_metrics": diagnostic_metrics,
        "diagnostic_metrics_valid": False,
        "invalid_reasons": reasons,
    }
