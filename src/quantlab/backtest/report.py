"""Report construction and performance-validity determination."""

from __future__ import annotations

from quantlab.backtest.metrics import compute_metrics
from quantlab.backtest.models import STATUS_COMPLETED, BacktestConfig, BacktestResult


def build_report(
    strict_result: BacktestResult,
    diagnostic_result: BacktestResult | None,
    reproducible: bool,
    config: BacktestConfig,
) -> dict:
    """Determine what performance numbers may be published.

    Full ``metrics`` are produced only when the strict run completed over the
    requested sessions with no unsupported event, no accounting error, and
    reproducible inputs. ``diagnostic_metrics`` are produced only when the
    diagnostic run itself had no accounting error, and are always marked
    ``diagnostic_metrics_valid=False``.
    """
    reasons: list[str] = []
    strict_completed = strict_result.status == STATUS_COMPLETED
    if not strict_completed:
        reasons.append(f"strict status={strict_result.status}")
    if strict_result.accounting_error is not None:
        reasons.append(f"accounting_error: {strict_result.accounting_error}")
    if not reproducible:
        reasons.append("input not reproducible")

    full_valid = (
        strict_completed
        and strict_result.accounting_error is None
        and reproducible
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
