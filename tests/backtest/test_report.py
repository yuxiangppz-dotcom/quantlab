from datetime import date, timedelta

import pytest

from quantlab.backtest import BacktestConfig, DailyBacktestRecord, compute_metrics
from quantlab.backtest.report import build_report


def _record(d, nav) -> DailyBacktestRecord:
    return DailyBacktestRecord(
        trade_date=d,
        nav_gross=nav,
        nav_net=nav,
        daily_return_gross=0.0,
        daily_return_net=0.0,
        gross_exposure=1.0,
        net_exposure=1.0,
        cash_weight=0.0,
        turnover=0.0,
        traded_notional_ratio=0.0,
        transaction_cost=0.0,
        holdings_count=1,
        gross_book_gross_exposure=1.0,
        gross_book_net_exposure=1.0,
        gross_book_cash_weight=0.0,
        gross_book_turnover=0.0,
        gross_book_traded_notional_ratio=0.0,
        gross_book_holdings_count=1,
    )


def _records(n=3):
    start = date(2026, 1, 5)
    return [_record(start + timedelta(days=i), 1.0 + 0.01 * i) for i in range(n)]


def _result(status, accounting_error=None, records=None, diag=False):
    from quantlab.backtest import BacktestResult

    return BacktestResult(
        run_mode="diagnostic" if diag else "strict",
        status=status,
        requested_period_start=None,
        requested_period_end=None,
        simulated_period_start=None,
        simulated_period_end=None,
        valid_through=None,
        diagnostic_from=None,
        first_blocking_event=None,
        records=records or [],
        rebalances=[],
        books=[],
        trades=[],
        skipped_executions=[],
        lifecycle_events=[],
        solver_root_residual=0.0,
        accounting_checks=[],
        accounting_error=accounting_error,
        accounting_error_date=None,
        accounting_error_book=None,
    )


def _cfg() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def test_reproducible_false_blocks_metrics() -> None:
    strict = _result("completed", records=_records())
    rep = build_report(strict, None, reproducible=False, config=_cfg())
    assert rep["performance_valid"] is False
    assert rep["metrics"] is None
    assert any("reproducible" in r for r in rep["invalid_reasons"])


def test_blocked_blocks_metrics() -> None:
    strict = _result("blocked_by_unsupported_event", records=_records())
    rep = build_report(strict, None, reproducible=True, config=_cfg())
    assert rep["metrics"] is None
    assert rep["performance_valid"] is False


def test_accounting_error_blocks_metrics() -> None:
    strict = _result("accounting_error", accounting_error="x", records=_records())
    rep = build_report(strict, None, reproducible=True, config=_cfg())
    assert rep["metrics"] is None
    assert rep["performance_valid"] is False


def test_completed_valid_produces_metrics() -> None:
    strict = _result("completed", records=_records())
    rep = build_report(strict, None, reproducible=True, config=_cfg())
    assert rep["performance_valid"] is True
    assert rep["metrics"] is not None
    assert rep["metrics"]["total_return_net"] is not None


def test_diagnostic_accounting_error_null_metrics() -> None:
    strict = _result("completed", records=_records())
    diag = _result("accounting_error", accounting_error="x", diag=True)
    rep = build_report(strict, diag, reproducible=True, config=_cfg())
    assert rep["diagnostic_metrics"] is None


def test_diagnostic_metrics_never_valid() -> None:
    strict = _result("blocked_by_unsupported_event", records=_records())
    diag = _result("blocked_by_unsupported_event", records=_records(), diag=True)
    rep = build_report(strict, diag, reproducible=True, config=_cfg())
    assert rep["diagnostic_metrics"] is not None  # diagnostic has no accounting error
    assert rep["diagnostic_metrics_valid"] is False
    assert rep["metrics"] is None  # strict blocked
    assert rep["performance_valid"] is False


def test_metrics_computed_consistently() -> None:
    recs = _records(5)
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, reproducible=True, config=_cfg())
    expected = compute_metrics(recs, [], _cfg())
    assert rep["metrics"]["total_return_net"] == pytest.approx(expected["total_return_net"])
    assert rep["metrics"]["n_records"] == expected["n_records"]
