"""Reusable per-path report construction and export for A/B/C experiments."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from quantlab.backtest.models import BacktestConfig, BacktestResult
from quantlab.backtest.report import build_report


def build_group_report(
    strict: BacktestResult,
    diagnostic: BacktestResult | None,
    config: BacktestConfig,
    expected_sessions: list[date],
    reproducible: bool,
) -> dict:
    """Build a uniform per-path report dict using the shared validity check."""
    report = build_report(
        strict, diagnostic, reproducible, config, expected_sessions=expected_sessions
    )

    def _iso(d: date | None) -> str | None:
        return d.isoformat() if d else None

    return {
        "performance_valid": report["performance_valid"],
        "metrics": report["metrics"],
        "invalid_reasons": report["invalid_reasons"],
        "requested_period": {
            "start": _iso(strict.requested_period_start),
            "end": _iso(strict.requested_period_end),
        },
        "simulated_period": {
            "start": _iso(strict.simulated_period_start),
            "end": _iso(strict.simulated_period_end),
        },
        "valid_through": _iso(strict.valid_through),
        "first_blocking_event": (
            strict.first_blocking_event.__dict__
            if strict.first_blocking_event else None
        ),
        "accounting_checks": [c.__dict__ for c in strict.accounting_checks],
        "accounting_error": strict.accounting_error,
        "accounting_error_date": _iso(strict.accounting_error_date),
        "accounting_error_book": strict.accounting_error_book,
        "diagnostic": (
            {
                "status": diagnostic.status,
                "diagnostic_from": _iso(diagnostic.diagnostic_from),
                "diagnostic_event_count": len(diagnostic.lifecycle_events),
            }
            if diagnostic is not None
            else None
        ),
    }


def _failed_attempts_to_json(failed_attempts) -> list:
    return [
        {
            "trade_date": fa.trade_date.isoformat(),
            "reason": fa.reason,
            "trades": [t.__dict__ for t in fa.trades],
            "rebalance": fa.rebalance.__dict__ if fa.rebalance else None,
        }
        for fa in failed_attempts
    ]


def export_group(
    out_dir: str | Path,
    prefix: str,
    strict: BacktestResult,
    diagnostic: BacktestResult | None = None,
) -> None:
    """Export one path's records/books/positions/trades/rebalances/events.

    ``failed_attempts`` is always written, including ``[]`` when empty.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame([r.__dict__ for r in strict.records]).to_csv(
        out_dir / f"{prefix}_daily_records.csv", index=False
    )
    pd.DataFrame([rb.__dict__ for rb in strict.rebalances]).to_csv(
        out_dir / f"{prefix}_rebalance_log.csv", index=False
    )
    pd.DataFrame([t.__dict__ for t in strict.trades]).to_csv(
        out_dir / f"{prefix}_trade_details.csv", index=False
    )
    if strict.lifecycle_events:
        pd.DataFrame([e.__dict__ for e in strict.lifecycle_events]).to_csv(
            out_dir / f"{prefix}_lifecycle_events.csv", index=False
        )
    (out_dir / f"{prefix}_failed_attempts.json").write_text(
        json.dumps(_failed_attempts_to_json(strict.failed_attempts), indent=2, default=str)
    )

    book_rows = []
    position_rows = []
    for b in strict.books:
        book_rows.append({
            "trade_date": b.trade_date, "book": b.book, "nav": b.nav,
            "daily_return": b.daily_return, "cash": b.cash,
            "market_pnl": b.market_pnl, "fee": b.fee,
            "gross_exposure": b.gross_exposure, "net_exposure": b.net_exposure,
            "cash_weight": b.cash_weight, "holdings_count": b.holdings_count,
        })
        for p in b.positions:
            position_rows.append({
                "trade_date": b.trade_date, "book": b.book,
                "instrument_id": p.instrument_id, "value": p.value,
                "weight": p.weight, "last_price": p.last_price,
                "last_mark_date": p.last_mark_date,
                "missing_price": p.missing_price,
            })
    pd.DataFrame(book_rows).to_csv(out_dir / f"{prefix}_daily_books.csv", index=False)
    pd.DataFrame(position_rows).to_csv(out_dir / f"{prefix}_daily_positions.csv", index=False)
