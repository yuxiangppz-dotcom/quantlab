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
    lifecycle_mode: str | None = None,
) -> dict:
    """Build a uniform per-path report dict using the shared validity check."""
    report = build_report(
        strict, diagnostic, reproducible, config, expected_sessions=expected_sessions
    )

    def _iso(d: date | None) -> str | None:
        return d.isoformat() if d else None

    return {
        "lifecycle_mode": lifecycle_mode,
        "strict_status": strict.status,
        "strict_record_count": len(strict.records),
        "performance_valid": report["performance_valid"],
        "metrics": report["metrics"],
        "invalid_reasons": report["invalid_reasons"],
        "reproducible": reproducible,
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
        "solver_root_residual": strict.solver_root_residual,
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


def _frame_of(items, cls) -> pd.DataFrame:
    """Build a DataFrame with explicit columns so empty data still writes a header."""
    fields = list(cls.__dataclass_fields__)
    return pd.DataFrame([x.__dict__ for x in items], columns=fields)


_BOOK_COLUMNS = [
    "trade_date", "book", "nav", "daily_return", "cash", "market_pnl", "fee",
    "gross_exposure", "net_exposure", "cash_weight", "holdings_count",
]
_POSITION_COLUMNS = [
    "trade_date", "book", "instrument_id", "value", "weight", "last_price",
    "last_mark_date", "missing_price",
]


def export_group(
    out_dir: str | Path,
    prefix: str,
    strict: BacktestResult,
    diagnostic: BacktestResult | None = None,
) -> None:
    """Export one path's records/books/positions/trades/rebalances/events.

    Every CSV writes a header even when empty; ``failed_attempts`` is always
    written, including ``[]`` when empty.
    """
    from quantlab.backtest.models import (
        DailyBacktestRecord,
        LifecycleEvent,
        RebalanceRecord,
        RiskPolicyAuditRecord,
        TradeRecord,
    )

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _frame_of(strict.records, DailyBacktestRecord).to_csv(
        out_dir / f"{prefix}_daily_records.csv", index=False
    )
    _frame_of(strict.rebalances, RebalanceRecord).to_csv(
        out_dir / f"{prefix}_rebalance_log.csv", index=False
    )
    _frame_of(strict.trades, TradeRecord).to_csv(
        out_dir / f"{prefix}_trade_details.csv", index=False
    )
    _frame_of(strict.lifecycle_events, LifecycleEvent).to_csv(
        out_dir / f"{prefix}_lifecycle_events.csv", index=False
    )
    risk_audit = _frame_of(strict.risk_policy_audit, RiskPolicyAuditRecord)
    risk_audit.to_csv(out_dir / f"{prefix}_risk_policy_audit.csv", index=False)
    risk_audit.loc[risk_audit["held"].astype(bool)].to_csv(
        out_dir / f"{prefix}_forced_exit_attempts.csv", index=False
    )
    risk_audit[risk_audit["forced_sell_value"] > 0].to_csv(
        out_dir / f"{prefix}_successful_forced_exits.csv", index=False
    )
    risk_audit[risk_audit["risk_state"] == "pending_no_price"].to_csv(
        out_dir / f"{prefix}_pending_no_price.csv", index=False
    )
    prevented = risk_audit["prevented_new_entry"].astype(bool) | risk_audit[
        "prevented_refill"
    ].astype(bool)
    risk_audit.loc[prevented].to_csv(
        out_dir / f"{prefix}_prevented_entry_refill.csv", index=False
    )
    risk_audit[risk_audit["risk_state"] == "blocked_before_exit"].to_csv(
        out_dir / f"{prefix}_blocked_before_exit.csv", index=False
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
    pd.DataFrame(book_rows, columns=_BOOK_COLUMNS).to_csv(
        out_dir / f"{prefix}_daily_books.csv", index=False
    )
    pd.DataFrame(position_rows, columns=_POSITION_COLUMNS).to_csv(
        out_dir / f"{prefix}_daily_positions.csv", index=False
    )
