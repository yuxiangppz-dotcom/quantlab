from dataclasses import replace
from datetime import date, timedelta

import pytest

from quantlab.backtest import BacktestConfig, DailyBacktestRecord, compute_metrics
from quantlab.backtest.models import DelistingSettlementConfig, SettlementRecord
from quantlab.backtest.report import _settlement_disclosure, build_report

START = date(2026, 1, 5)


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


def _sessions(n=3):
    return [START + timedelta(days=i) for i in range(n)]


def _records(n=3):
    sessions = _sessions(n)
    return [_record(d, 1.0 + 0.01 * i) for i, d in enumerate(sessions)]


def _result(status, accounting_error=None, records=None, run_mode="strict",
            first_blocking_event=None, diagnostic_from=None):
    from quantlab.backtest import BacktestResult

    return BacktestResult(
        run_mode=run_mode,
        status=status,
        requested_period_start=None,
        requested_period_end=None,
        simulated_period_start=None,
        simulated_period_end=None,
        valid_through=None,
        diagnostic_from=diagnostic_from,
        first_blocking_event=first_blocking_event,
        records=records or [],
        rebalances=[],
        books=[],
        trades=[],
        skipped_executions=[],
        lifecycle_events=[],
        failed_attempts=[],
        solver_root_residual=0.0,
        accounting_checks=[],
        accounting_error=accounting_error,
        accounting_error_date=None,
        accounting_error_book=None,
    )


def _cfg() -> BacktestConfig:
    return BacktestConfig(initial_nav=1.0, transaction_cost_bps=0.0, annualization=252)


def _valid_record_count(n=3):
    recs = _records(n)
    return recs, _sessions(n)


def test_reproducible_false_blocks_metrics() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, False, _cfg(), expected_sessions=sessions)
    assert rep["performance_valid"] is False
    assert rep["metrics"] is None


def test_blocked_blocks_metrics() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("blocked_by_unsupported_event", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["metrics"] is None


def test_accounting_error_blocks_metrics() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("accounting_error", accounting_error="x", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["metrics"] is None


def test_completed_valid_produces_metrics() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["performance_valid"] is True
    assert rep["metrics"] is not None


def test_coverage_missing_blocks_metrics() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=None)
    assert rep["metrics"] is None
    assert any("coverage" in r for r in rep["invalid_reasons"])


def test_shorter_simulation_than_requested() -> None:
    recs, sessions = _valid_record_count(3)
    # request a longer period than the records cover
    longer = sessions + [sessions[-1] + timedelta(days=1)]
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=longer)
    assert rep["metrics"] is None
    assert any("coverage incomplete" in r for r in rep["invalid_reasons"])


def test_missing_middle_session() -> None:
    recs, sessions = _valid_record_count(4)
    # drop the middle session
    recs = [r for r in recs if r.trade_date != sessions[1]]
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["metrics"] is None
    assert any("coverage incomplete" in r for r in rep["invalid_reasons"])


def test_diagnostic_passed_as_strict() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("completed", records=recs, run_mode="diagnostic")
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["metrics"] is None
    assert any("run_mode" in r for r in rep["invalid_reasons"])


def test_boundary_non_trading_day_valid() -> None:
    # requested period endpoints can be non-trading days; only trading sessions
    # are expected, so a complete run over those sessions is valid.
    recs, sessions = _valid_record_count(3)
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    assert rep["performance_valid"] is True


def test_diagnostic_metrics_never_valid() -> None:
    recs, sessions = _valid_record_count()
    strict = _result("blocked_by_unsupported_event", records=recs)
    diag = _result("blocked_by_unsupported_event", records=recs, run_mode="diagnostic")
    rep = build_report(strict, diag, True, _cfg(), expected_sessions=sessions)
    assert rep["diagnostic_metrics"] is not None
    assert rep["diagnostic_metrics_valid"] is False
    assert rep["metrics"] is None


def test_metrics_computed_consistently() -> None:
    recs, sessions = _valid_record_count(5)
    strict = _result("completed", records=recs)
    rep = build_report(strict, None, True, _cfg(), expected_sessions=sessions)
    expected = compute_metrics(recs, [], _cfg())
    assert rep["metrics"]["total_return_net"] == pytest.approx(expected["total_return_net"])
    assert rep["metrics"]["n_records"] == expected["n_records"]


def _settlement(instrument_id, days_since_mark, blocking_session) -> SettlementRecord:
    event_date = blocking_session - timedelta(days=(days_since_mark or 0) + 7)
    last_mark = (
        blocking_session - timedelta(days=days_since_mark)
        if days_since_mark is not None
        else None
    )
    return SettlementRecord(
        instrument_id=instrument_id,
        book="net",
        event_type="delist",
        event_date=event_date,
        blocking_session=blocking_session,
        last_mark_value=1000.0,
        last_mark_date=last_mark,
        recovery_rate=1.0,
        settlement_fee=0.0,
        settled_value=1000.0,
        recovery_shortfall=0.0,
        description="synthetic settlement for disclosure stats",
    )


def test_settlement_disclosure_days_since_last_mark_stats() -> None:
    sessions = _sessions(5)
    cfg = BacktestConfig(
        initial_nav=1.0,
        transaction_cost_bps=0.0,
        annualization=252,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=1.0, settlement_fee_bps=0.0
        ),
    )
    strict = _result(
        "completed_with_settlement_assumptions",
        records=_records(5),
    )
    # 4 settled instruments; each settlement applies at a later session
    events = [
        _settlement("000001.SZ", 3, sessions[1]),
        _settlement("000002.SZ", 7, sessions[2]),
        _settlement("000004.SZ", 11, sessions[3]),
        _settlement("000005.SZ", 21, sessions[4]),
    ]
    strict = replace(strict, settlement_events=events)
    disclosure = _settlement_disclosure(strict, cfg)
    stats = disclosure["days_since_last_mark_stats"]
    # ages sorted: [3, 7, 11, 21]
    assert stats["p25"] == pytest.approx(6.0)
    assert stats["median"] == pytest.approx(9.0)
    assert stats["p75"] == pytest.approx(13.5)
    assert stats["max"] == 21


def test_settlement_disclosure_stats_single_and_missing_marks() -> None:
    sessions = _sessions(2)
    cfg = BacktestConfig(
        initial_nav=1.0,
        transaction_cost_bps=0.0,
        annualization=252,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=0.0, settlement_fee_bps=0.0
        ),
    )
    strict = _result("completed_with_settlement_assumptions", records=_records(2))
    events = [
        _settlement("000001.SZ", 5, sessions[1]),
        _settlement("000002.SZ", None, sessions[1]),  # unknown last mark
    ]
    strict = replace(strict, settlement_events=events)
    disclosure = _settlement_disclosure(strict, cfg)
    stats = disclosure["days_since_last_mark_stats"]
    # only one instrument has a computable mark age
    assert stats["p25"] == stats["median"] == stats["p75"] == stats["max"] == 5

    empty = _result(
        "completed_with_settlement_assumptions", records=_records(2)
    )
    empty = replace(
        empty,
        settlement_events=[_settlement("000003.SZ", None, sessions[1])],
    )
    disclosure_empty = _settlement_disclosure(empty, cfg)
    assert disclosure_empty["days_since_last_mark_stats"] == {
        "p25": None, "median": None, "p75": None, "max": None,
    }
