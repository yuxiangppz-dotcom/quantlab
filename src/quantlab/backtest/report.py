"""Report construction and performance-validity determination."""

from __future__ import annotations

import math
from datetime import date

from quantlab.backtest.metrics import compute_metrics
from quantlab.backtest.models import (
    RUN_MODE_STRICT,
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_SETTLEMENT,
    BacktestConfig,
    BacktestResult,
)

_VALID_STATUSES = (STATUS_COMPLETED, STATUS_COMPLETED_WITH_SETTLEMENT)


def _settlement_disclosure(
    strict_result: BacktestResult,
    config: BacktestConfig,
) -> dict | None:
    """Describe the explicit settlement assumptions used by the strict run.

    The disclosure exists so a reader can never mistake settlement for a
    verified market transaction or a verified delisting fact: it repeats the
    assumption parameters, lists every settled instrument with the age of its
    last mark (stale-mark risk), and sizes the settled notional against the
    period average NAV.
    """
    cfg = config.delisting_settlement
    events = strict_result.settlement_events
    if cfg is None and not events:
        return None

    navs = [r.nav_net for r in strict_result.records]
    average_nav = (
        math.fsum(navs) / len(navs) if navs else float("nan")
    )

    net_rows = [e for e in events if e.book == "net"]
    gross_rows = [e for e in events if e.book == "gross"]

    by_instrument: dict[str, list] = {}
    for row in net_rows:
        by_instrument.setdefault(row.instrument_id, []).append(row)

    instruments: list[dict] = []
    for instrument_id, rows in sorted(by_instrument.items()):
        row = rows[0]
        days_since_last_mark = (
            (row.blocking_session - row.last_mark_date).days
            if row.last_mark_date is not None
            else None
        )
        instruments.append(
            {
                "instrument_id": instrument_id,
                "event_type": row.event_type,
                "event_date": row.event_date.isoformat(),
                "blocking_session": row.blocking_session.isoformat(),
                "last_mark_value": row.last_mark_value,
                "last_mark_date": (
                    row.last_mark_date.isoformat()
                    if row.last_mark_date is not None
                    else None
                ),
                "days_since_last_mark": days_since_last_mark,
                "recovery_rate": row.recovery_rate,
                "settlement_fee": row.settlement_fee,
                "settled_value": row.settled_value,
                "recovery_shortfall": row.recovery_shortfall,
                "description": row.description,
            }
        )

    total_settled_value_net = math.fsum(row.settled_value for row in net_rows)
    total_settled_value_gross = math.fsum(row.settled_value for row in gross_rows)
    total_shortfall_net = math.fsum(row.recovery_shortfall for row in net_rows)
    total_fee_net = math.fsum(row.settlement_fee for row in net_rows)

    mark_ages = sorted(
        entry["days_since_last_mark"]
        for entry in instruments
        if entry["days_since_last_mark"] is not None
    )

    return {
        "nature": (
            "explicit settlement assumption; not a market transaction and "
            "not a verified delisting fact"
        ),
        "assumptions": {
            "recovery_rate": cfg.recovery_rate if cfg is not None else None,
            "settlement_fee_bps": (
                cfg.settlement_fee_bps if cfg is not None else None
            ),
            "gross_book_fee": 0.0,
        },
        "affected_instrument_count": len(by_instrument),
        "instruments": instruments,
        "total_settled_value_net": total_settled_value_net,
        "total_settled_value_gross": total_settled_value_gross,
        "total_recovery_shortfall_net": total_shortfall_net,
        "total_settlement_fee_net": total_fee_net,
        "period_average_nav_net": average_nav,
        "settled_notional_to_period_average_nav": (
            total_settled_value_net / average_nav
            if average_nav and math.isfinite(average_nav) and average_nav > 0
            else None
        ),
        "days_since_last_mark_stats": {
            "p25": _percentile(mark_ages, 0.25),
            "median": _percentile(mark_ages, 0.5),
            "p75": _percentile(mark_ages, 0.75),
            "max": mark_ages[-1] if mark_ages else None,
        },
        "stale_mark_risk": (
            "days_since_last_mark exposes how stale the settlement mark is; "
            "a large value means the last available price predates the "
            "settlement session by a long window"
        ),
    }


def _percentile(sorted_values: list, quantile: float):
    """Linear-interpolated percentile (numpy 'linear' default) of sorted input."""
    if not sorted_values:
        return None
    if not 0.0 <= quantile <= 1.0:
        raise ValueError(f"quantile out of range: {quantile}")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


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
    - its status is ``completed`` or, when the run opted into the explicit
      delisting settlement assumption, ``completed_with_settlement_assumptions``;
    - it has no ``accounting_error``, no unresolved blocking event, and no
      diagnostic region;
    - inputs are reproducible;
    - its records fully cover the expected trading sessions.

    Missing coverage evidence is never treated as complete. A settled run is
    valid only together with its ``settlement_disclosure``: the settlement is
    an explicit assumption, not a fact.
    """
    reasons: list[str] = []
    settlement_status = (
        strict_result.status == STATUS_COMPLETED_WITH_SETTLEMENT
    )
    if strict_result.run_mode != RUN_MODE_STRICT:
        reasons.append(f"run_mode={strict_result.run_mode} (not strict)")
    if strict_result.status not in _VALID_STATUSES:
        reasons.append(f"strict status={strict_result.status}")
    if strict_result.accounting_error is not None:
        reasons.append(f"accounting_error: {strict_result.accounting_error}")
    if (
        strict_result.first_blocking_event is not None
        and not settlement_status
    ):
        reasons.append("unsupported lifecycle event blocked the run")
    if settlement_status and not strict_result.settlement_events:
        reasons.append(
            "settlement status declared without any settlement record"
        )
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
        and strict_result.status in _VALID_STATUSES
        and strict_result.accounting_error is None
        and (
            strict_result.first_blocking_event is None or settlement_status
        )
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
        "settlement_disclosure": _settlement_disclosure(strict_result, config),
    }
