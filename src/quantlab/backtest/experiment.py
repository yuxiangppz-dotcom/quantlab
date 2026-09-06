"""Reusable per-path report construction and export for A/B/C experiments.

Exports are bounded-memory (row generators streamed into chunked CSV writes)
and per-file atomic (temp sidecar + ``os.replace``), so a mid-export crash
can never leave a half-written formal file behind.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

from quantlab.backtest.artifacts import (
    DEFAULT_CHUNK_ROWS,
    atomic_write_json,
    sha256_file,
    stream_csv,
)
from quantlab.backtest.artifacts import (
    record_rows as _record_rows,
)
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


def _dataclass_columns(cls) -> list[str]:
    return list(cls.__dataclass_fields__)


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
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    position_rows_iter: Iterator[dict] | None = None,
) -> dict:
    """Export one path's records/books/positions/trades/rebalances/events.

    Every CSV is streamed in bounded chunks (never materializing the full
    position/trade row list) and written atomically: temp sidecar first, then
    ``os.replace`` after a clean close. Every file writes a stable header even
    when empty; ``failed_attempts`` is always written, including ``[]``.

    Returns the group's export manifest (``{filename: {rows, bytes, sha256}}``
    plus ``complete``), consumed by the run-level artifact manifest. On a
    mid-stream failure the exception propagates, the temp sidecar is removed,
    and no partial final file is left behind.
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
    files: dict[str, dict] = {}

    def _export(name: str, columns: list[str], rows: Iterator[dict]) -> None:
        rows_written, size = stream_csv(
            out_dir / name, columns, rows, chunk_rows=chunk_rows
        )
        files[name] = {
            "rows": rows_written,
            "bytes": size,
            "sha256": sha256_file(out_dir / name),
        }

    _export(
        f"{prefix}_daily_records.csv",
        _dataclass_columns(DailyBacktestRecord),
        _record_rows(strict.records, _dataclass_columns(DailyBacktestRecord)),
    )
    _export(
        f"{prefix}_rebalance_log.csv",
        _dataclass_columns(RebalanceRecord),
        _record_rows(strict.rebalances, _dataclass_columns(RebalanceRecord)),
    )
    _export(
        f"{prefix}_trade_details.csv",
        _dataclass_columns(TradeRecord),
        _record_rows(strict.trades, _dataclass_columns(TradeRecord)),
    )
    _export(
        f"{prefix}_lifecycle_events.csv",
        _dataclass_columns(LifecycleEvent),
        _record_rows(strict.lifecycle_events, _dataclass_columns(LifecycleEvent)),
    )

    risk_columns = _dataclass_columns(RiskPolicyAuditRecord)

    def _all_risk_rows() -> Iterator[dict]:
        yield from _record_rows(
            strict.risk_policy_audit, risk_columns
        )

    def _filtered_risk_rows(predicate) -> Iterator[dict]:
        for r in strict.risk_policy_audit:
            if predicate(r):
                yield from _record_rows([r], risk_columns)

    _export(
        f"{prefix}_risk_policy_audit.csv", risk_columns, _all_risk_rows()
    )
    _export(
        f"{prefix}_forced_exit_attempts.csv",
        risk_columns,
        _filtered_risk_rows(lambda r: r.held),
    )
    _export(
        f"{prefix}_successful_forced_exits.csv",
        risk_columns,
        _filtered_risk_rows(lambda r: r.forced_sell_value > 0),
    )
    _export(
        f"{prefix}_pending_no_price.csv",
        risk_columns,
        _filtered_risk_rows(lambda r: r.risk_state == "pending_no_price"),
    )
    _export(
        f"{prefix}_prevented_entry_refill.csv",
        risk_columns,
        _filtered_risk_rows(
            lambda r: r.prevented_new_entry or r.prevented_refill
        ),
    )
    _export(
        f"{prefix}_blocked_before_exit.csv",
        risk_columns,
        _filtered_risk_rows(lambda r: r.risk_state == "blocked_before_exit"),
    )

    def _book_rows() -> Iterator[dict]:
        for b in strict.books:
            yield {
                "trade_date": b.trade_date, "book": b.book, "nav": b.nav,
                "daily_return": b.daily_return, "cash": b.cash,
                "market_pnl": b.market_pnl, "fee": b.fee,
                "gross_exposure": b.gross_exposure,
                "net_exposure": b.net_exposure,
                "cash_weight": b.cash_weight,
                "holdings_count": b.holdings_count,
            }

    def _position_rows() -> Iterator[dict]:
        if position_rows_iter is not None:
            yield from position_rows_iter
            return
        for b in strict.books:
            for p in b.positions:
                yield {
                    "trade_date": b.trade_date, "book": b.book,
                    "instrument_id": p.instrument_id, "value": p.value,
                    "weight": p.weight, "last_price": p.last_price,
                    "last_mark_date": p.last_mark_date,
                    "missing_price": p.missing_price,
                }

    _export(f"{prefix}_daily_books.csv", _BOOK_COLUMNS, _book_rows())
    _export(f"{prefix}_daily_positions.csv", _POSITION_COLUMNS, _position_rows())

    failed_name = f"{prefix}_failed_attempts.json"
    files[failed_name] = {
        "bytes": atomic_write_json(
            out_dir / failed_name,
            _failed_attempts_to_json(strict.failed_attempts),
        ),
        "sha256": sha256_file(out_dir / failed_name),
    }
    return {"group": prefix, "files": files, "complete": True}
