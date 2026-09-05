import json
from datetime import date, timedelta

from quantlab.backtest import BacktestConfig
from quantlab.backtest.experiment import build_group_report, export_group
from quantlab.backtest.models import (
    STATUS_ACCOUNTING_ERROR,
    STATUS_BLOCKED_UNSUPPORTED_EVENT,
    STATUS_COMPLETED,
    BacktestResult,
    BookSnapshot,
    DailyBacktestRecord,
    PositionRecord,
)

D0 = date(2026, 1, 5)


def _record(d, nav=1.0) -> DailyBacktestRecord:
    return DailyBacktestRecord(
        trade_date=d, nav_gross=nav, nav_net=nav,
        daily_return_gross=0.0, daily_return_net=0.0,
        gross_exposure=1.0, net_exposure=1.0, cash_weight=0.0,
        turnover=0.0, traded_notional_ratio=0.0, transaction_cost=0.0,
        holdings_count=1,
        gross_book_gross_exposure=1.0, gross_book_net_exposure=1.0,
        gross_book_cash_weight=0.0, gross_book_turnover=0.0,
        gross_book_traded_notional_ratio=0.0, gross_book_holdings_count=1,
    )


def _snapshot(d, book="net") -> BookSnapshot:
    return BookSnapshot(
        trade_date=d, book=book, nav=1.0, daily_return=0.0, cash=0.0,
        market_pnl=0.0, fee=0.0, gross_exposure=1.0, net_exposure=1.0,
        cash_weight=0.0, holdings_count=1,
        positions=(PositionRecord("A", 1.0, 1.0, 100.0, D0, False),),
    )


def _result(status, accounting_error=None, n=2, failed=0):
    records = [_record(D0 + timedelta(days=i)) for i in range(n)]
    books = [_snapshot(D0 + timedelta(days=i)) for i in range(n)]
    failed_attempts = []
    from quantlab.backtest.models import FailedAttempt

    if failed:
        fa = FailedAttempt(
            trade_date=D0 + timedelta(days=n),
            reason=accounting_error or "boom",
            trades=(),
            rebalance=None,
        )
        failed_attempts = [fa]

    return BacktestResult(
        run_mode="strict", status=status,
        requested_period_start=D0, requested_period_end=D0 + timedelta(days=n - 1),
        simulated_period_start=D0, simulated_period_end=D0 + timedelta(days=n - 1),
        valid_through=D0 + timedelta(days=n - 1), diagnostic_from=None,
        first_blocking_event=None,
        records=records, rebalances=[], books=books, trades=[],
        skipped_executions=[], lifecycle_events=[],
        failed_attempts=failed_attempts,
        solver_root_residual=0.0, accounting_checks=[],
        accounting_error=accounting_error,
        accounting_error_date=None, accounting_error_book=None,
    )


def _cfg():
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def test_export_group_writes_required_files(tmp_path) -> None:
    strict = _result(STATUS_COMPLETED)
    export_group(tmp_path, "X", strict)
    for suffix in (
        "daily_records.csv", "rebalance_log.csv", "trade_details.csv",
        "daily_books.csv", "daily_positions.csv", "failed_attempts.json",
    ):
        assert (tmp_path / f"X_{suffix}").exists(), suffix


def test_empty_failed_attempts_writes_empty_list(tmp_path) -> None:
    strict = _result(STATUS_COMPLETED, failed=0)
    export_group(tmp_path, "X", strict)
    data = json.loads((tmp_path / "X_failed_attempts.json").read_text())
    assert data == []


def test_failed_attempts_exported(tmp_path) -> None:
    strict = _result(STATUS_ACCOUNTING_ERROR, accounting_error="boom", failed=1)
    export_group(tmp_path, "X", strict)
    data = json.loads((tmp_path / "X_failed_attempts.json").read_text())
    assert len(data) == 1
    assert data[0]["reason"] == "boom"


def test_group_report_blocked_metrics_null(tmp_path) -> None:
    strict = _result(STATUS_BLOCKED_UNSUPPORTED_EVENT)
    rep = build_group_report(
        strict, None, _cfg(), [D0, D0 + timedelta(days=1)], True
    )
    assert rep["performance_valid"] is False
    assert rep["metrics"] is None
    assert rep["invalid_reasons"]


def test_group_report_completed_metrics_present() -> None:
    strict = _result(STATUS_COMPLETED, n=3)
    sessions = [D0 + timedelta(days=i) for i in range(3)]
    rep = build_group_report(strict, None, _cfg(), sessions, True)
    assert rep["performance_valid"] is True
    assert rep["metrics"] is not None


def test_three_group_structure_consistent() -> None:
    strict_a = _result(STATUS_BLOCKED_UNSUPPORTED_EVENT, n=2)
    strict_b = _result(STATUS_BLOCKED_UNSUPPORTED_EVENT, n=3)
    strict_c = _result(STATUS_BLOCKED_UNSUPPORTED_EVENT, n=4)
    sessions = [D0 + timedelta(days=i) for i in range(4)]
    ra = build_group_report(strict_a, None, _cfg(), sessions, True)
    rb = build_group_report(strict_b, None, _cfg(), sessions, True)
    rc = build_group_report(strict_c, None, _cfg(), sessions, True)
    assert set(ra.keys()) == set(rb.keys()) == set(rc.keys())
    for key in ra:
        assert key in rb and key in rc
