"""Delisting settlement policy v0: synthetic-data engine tests."""

from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from quantlab.backtest import (
    DELIST_DATE_IS_FIRST_INVALID_V1,
    EXIT_POLICY_ID,
    BacktestConfig,
    DelistingSettlementConfig,
    LifecycleMonitor,
    build_report,
    run_backtest,
)
from quantlab.data.models import Security
from quantlab.portfolio import TargetPortfolio, TargetWeight

D0 = date(2026, 1, 5)
D1 = date(2026, 1, 6)
D2 = date(2026, 1, 7)
D3 = date(2026, 1, 8)
D4 = date(2026, 1, 9)

# A delists at D2 (v1: first invalid session D2); prices end on D1.
A_DELIST_PRICES = {D0: {"A": 100.0}, D1: {"A": 100.0}, D2: {}, D3: {}}


def _prices(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"instrument_id": i, "trade_date": d, "adj_close": p}
            for d, values in rows.items()
            for i, p in values.items()
        ]
    )


def _target(as_of, weights, cash=0.0) -> TargetPortfolio:
    return TargetPortfolio(
        as_of=as_of,
        positions=tuple(TargetWeight(i, w) for i, w in weights.items()),
        cash_weight=cash,
    )


def _facts(available_from=D1, instrument_id="A") -> dict:
    return {
        instrument_id: {
            "facts": [
                {
                    "fact_type": "termination_decision",
                    "fact_id": f"{instrument_id}:termination:{available_from}",
                    "content_verified": True,
                    "public_time_verified": True,
                    "available_from": available_from.isoformat(),
                    "source": "fixture",
                }
            ]
        }
    }


def _cfg(bps=10.0, recovery=1.0, settle_fee_bps=0.0) -> BacktestConfig:
    return BacktestConfig(
        initial_nav=1.0,
        transaction_cost_bps=bps,
        annualization=252,
        delisting_settlement=DelistingSettlementConfig(
            recovery_rate=recovery, settlement_fee_bps=settle_fee_bps
        ),
    )


def _legacy_cfg(bps=10.0, initial_nav=1.0) -> BacktestConfig:
    return BacktestConfig(
        initial_nav=initial_nav, transaction_cost_bps=bps, annualization=252
    )


def _security(instrument_id, delist_date) -> Security:
    return Security(
        instrument_id=instrument_id,
        symbol=instrument_id,
        name="x",
        exchange="SSE",
        market="SH",
        board="main",
        list_status="D",
        list_date=date(2000, 1, 1),
        delist_date=delist_date,
    )


def _monitor(instrument_id, delist_date) -> LifecycleMonitor:
    return LifecycleMonitor(
        [_security(instrument_id, delist_date)],
        [],
        mode=DELIST_DATE_IS_FIRST_INVALID_V1,
    )


def _settle_run(prices, dates, config, targets=None, **kwargs):
    return run_backtest(
        prices,
        dates,
        targets or {D0: _target(D0, {"A": 1.0})},
        config,
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor("A", D2),
        requested_period_start=D0,
        requested_period_end=D3,
        **kwargs,
    )


def _snap(result, d, book="net"):
    return next(b for b in result.books if b.trade_date == d and b.book == book)


def _position(result, d, instrument_id="A", book="net"):
    return next(
        (
            p
            for p in _snap(result, d, book).positions
            if p.instrument_id == instrument_id
        ),
        None,
    )


def _assert_all_checks_pass(result) -> None:
    assert result.accounting_error is None
    assert result.accounting_checks, "expected reconciliation checks to run"
    for check in result.accounting_checks:
        assert check.max_abs < 1e-9
        assert check.max_rel < 1e-9


def test_settlement_cash_flow_identity_and_full_run_completion() -> None:
    dates = [D0, D1, D2, D3]
    result = _settle_run(_prices(A_DELIST_PRICES), dates, _cfg(bps=10.0))

    assert result.status == "completed_with_settlement_assumptions"
    assert result.valid_through == D3
    assert result.accounting_error is None

    net_rows = [e for e in result.settlement_events if e.book == "net"]
    gross_rows = [e for e in result.settlement_events if e.book == "gross"]
    assert len(net_rows) == len(gross_rows) == 1

    buy_value_net = 1.0 / 1.001  # 10 bps cost on the T+1 buy
    row = net_rows[0]
    assert row.instrument_id == "A"
    assert row.event_type == "delist"
    assert row.event_date == D2
    assert row.blocking_session == D2
    assert row.last_mark_date == D1
    assert row.last_mark_value == pytest.approx(buy_value_net)
    assert row.recovery_rate == 1.0
    # cash credited = last_mark * recovery - settlement_fee
    assert row.settled_value == pytest.approx(
        row.last_mark_value * row.recovery_rate - row.settlement_fee
    )
    assert row.recovery_shortfall == pytest.approx(
        row.last_mark_value - row.last_mark_value * row.recovery_rate
    )
    assert gross_rows[0].settlement_fee == 0.0
    assert gross_rows[0].settled_value == pytest.approx(1.0)

    assert _position(result, D3) is None
    assert _snap(result, D3, "net").cash == pytest.approx(buy_value_net)
    assert _snap(result, D3, "net").nav == pytest.approx(buy_value_net)
    _assert_all_checks_pass(result)


def test_gross_book_never_pays_settlement_fee() -> None:
    dates = [D0, D1, D2, D3]
    result = _settle_run(
        _prices(A_DELIST_PRICES), dates, _cfg(bps=10.0, settle_fee_bps=50.0)
    )

    net_row = next(e for e in result.settlement_events if e.book == "net")
    gross_row = next(e for e in result.settlement_events if e.book == "gross")
    assert net_row.settlement_fee == pytest.approx(0.005 * net_row.last_mark_value)
    assert net_row.settled_value == pytest.approx(net_row.last_mark_value * 0.995)
    # gross uses the same recovery but never pays the settlement fee
    assert gross_row.settlement_fee == 0.0
    assert gross_row.settled_value == pytest.approx(gross_row.last_mark_value)

    nav_d1_net = _snap(result, D1, "net").nav
    nav_d2_net = _snap(result, D2, "net").nav
    assert nav_d2_net == pytest.approx(
        nav_d1_net - net_row.recovery_shortfall - net_row.settlement_fee, abs=1e-12
    )
    nav_d1_gross = _snap(result, D1, "gross").nav
    nav_d2_gross = _snap(result, D2, "gross").nav
    assert nav_d2_gross == pytest.approx(
        nav_d1_gross - gross_row.recovery_shortfall, abs=1e-12
    )
    _assert_all_checks_pass(result)


def test_nav_continuous_across_settlement_no_hidden_jump() -> None:
    dates = [D0, D1, D2, D3]
    result = _settle_run(
        _prices(A_DELIST_PRICES), dates, _cfg(bps=10.0, settle_fee_bps=50.0)
    )
    nav = {r.trade_date: r.nav_net for r in result.records}
    net_row = next(e for e in result.settlement_events if e.book == "net")
    drop = nav[D1] - nav[D2]
    assert drop == pytest.approx(
        net_row.settlement_fee + net_row.recovery_shortfall, abs=1e-12
    )
    d2 = next(r for r in result.records if r.trade_date == D2)
    assert d2.daily_return_net == pytest.approx(nav[D2] / nav[D1] - 1, abs=1e-12)
    assert nav[D3] == pytest.approx(nav[D2], abs=1e-12)


def test_zero_recovery_boundary_settles_to_zero_proceeds() -> None:
    dates = [D0, D1, D2, D3]
    result = _settle_run(_prices(A_DELIST_PRICES), dates, _cfg(bps=10.0, recovery=0.0))

    assert result.status == "completed_with_settlement_assumptions"
    for book_name in ("gross", "net"):
        row = next(e for e in result.settlement_events if e.book == book_name)
        assert row.recovery_rate == 0.0
        assert row.settled_value == 0.0
        assert row.recovery_shortfall == pytest.approx(row.last_mark_value)
    assert _position(result, D3) is None
    _assert_all_checks_pass(result)


def test_trusted_fact_exit_prevents_double_settlement() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices({d: {"A": 100.0} for d in dates})
    result = run_backtest(
        prices,
        dates,
        {D0: _target(D0, {"A": 1.0})},
        _cfg(bps=10.0),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor("A", D3),
        requested_period_start=D0,
        requested_period_end=D3,
        risk_facts=_facts(available_from=D2),
        risk_policy=EXIT_POLICY_ID,
    )
    # the T+1 buy lands at D1 close (no decision yet); the fact appears at D2
    # and the risk policy force-exits at D2 while the instrument is tradable
    assert result.status == "completed"
    assert result.settlement_events == []
    exited = next(
        r
        for r in result.risk_policy_audit
        if r.risk_state == "exited" and r.book == "net"
    )
    assert exited.decision_date == D2
    assert exited.execution_price == 100.0
    assert _position(result, D3) is None
    _assert_all_checks_pass(result)


def test_target_only_event_stays_non_blocking_without_settlement() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(
        {
            D0: {"A": 100.0, "B": 100.0},
            D1: {"A": 100.0, "B": 100.0},
            D2: {"A": 100.0},
            D3: {"A": 100.0},
        }
    )
    config = _cfg(bps=10.0)
    result = run_backtest(
        prices,
        dates,
        {D1: _target(D1, {"A": 0.5, "B": 0.5})},
        config,
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor("B", D2),
        requested_period_start=D0,
        requested_period_end=D3,
    )
    # B never held: the blocked target entry is audit-only, run completes
    assert result.status == "completed"
    assert result.settlement_events == []
    assert any(
        e.book == "target" and e.instrument_id == "B" for e in result.lifecycle_events
    )
    assert _position(result, D2, instrument_id="B") is None
    assert result.valid_through == D3
    _assert_all_checks_pass(result)

    report = build_report(result, None, True, config, expected_sessions=dates)
    assert report["performance_valid"] is True
    assert report["invalid_reasons"] == []


def test_settled_instrument_cannot_be_re_entered_or_settled_twice() -> None:
    dates = [D0, D1, D2, D3]
    result = _settle_run(
        _prices(A_DELIST_PRICES),
        dates,
        _cfg(bps=10.0),
        targets={
            D0: _target(D0, {"A": 1.0}),
            D2: _target(D2, {"A": 1.0}),  # exec D3: after settlement
        },
    )
    assert result.status == "completed_with_settlement_assumptions"
    assert len([e for e in result.settlement_events if e.book == "net"]) == 1
    assert len([e for e in result.settlement_events if e.book == "gross"]) == 1
    assert _position(result, D3) is None
    _assert_all_checks_pass(result)


def test_settlement_and_rebalance_same_session_reconcile() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(
        {
            D0: {"A": 100.0, "B": 100.0},
            D1: {"A": 100.0, "B": 100.0},
            D2: {"A": 100.0, "B": 100.0},
            D3: {"B": 100.0},
        }
    )
    result = run_backtest(
        prices,
        dates,
        {
            D0: _target(D0, {"A": 0.5, "B": 0.5}),
            D1: _target(D1, {"B": 1.0}),  # exec D2: same session as settlement
        },
        _cfg(bps=10.0),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor("A", D2),
        requested_period_start=D0,
        requested_period_end=D3,
    )
    assert result.status == "completed_with_settlement_assumptions"
    assert _position(result, D2, instrument_id="A") is None
    assert _position(result, D3, instrument_id="A") is None
    assert _position(result, D3, instrument_id="B") is not None
    # A must not appear as a market trade on the settlement session
    assert not any(
        t.instrument_id == "A" and t.execution_date == D2 for t in result.trades
    )
    for book in result.books:
        assert book.nav == pytest.approx(
            book.cash + sum(p.value for p in book.positions), abs=1e-12
        )
    _assert_all_checks_pass(result)


def test_legacy_blocking_unchanged_without_settlement_config() -> None:
    dates = [D0, D1, D2, D3]
    config = _legacy_cfg(bps=10.0)
    assert config.delisting_settlement is None
    result = _settle_run(_prices(A_DELIST_PRICES), dates, config)
    assert result.status == "blocked_by_unsupported_event"
    assert result.settlement_events == []
    report = build_report(result, None, True, config, expected_sessions=dates)
    assert report["performance_valid"] is False
    assert any("blocked" in reason for reason in report["invalid_reasons"])
    assert report["metrics"] is None


def test_build_report_accepts_settlement_run_when_coverage_is_full() -> None:
    dates = [D0, D1, D2, D3]
    config = _cfg(bps=10.0)
    result = _settle_run(_prices(A_DELIST_PRICES), dates, config)
    report = build_report(result, None, True, config, expected_sessions=dates)

    assert report["performance_valid"] is True
    assert report["invalid_reasons"] == []
    assert report["metrics"] is not None
    disclosure = report["settlement_disclosure"]
    assert disclosure is not None
    assert disclosure["assumptions"] == {
        "recovery_rate": 1.0,
        "settlement_fee_bps": 0.0,
        "gross_book_fee": 0.0,
    }
    assert disclosure["affected_instrument_count"] == 1
    row = disclosure["instruments"][0]
    assert row["instrument_id"] == "A"
    assert row["days_since_last_mark"] == 1  # last mark D1, settled D2
    assert row["settled_value"] > 0.0
    assert disclosure["total_settled_value_net"] > 0.0
    assert 0.0 < disclosure["settled_notional_to_period_average_nav"] <= 1.0

    # partial coverage must stay invalid even for a settled run
    partial = build_report(
        result, None, True, config, expected_sessions=[D0, D1, D2, D3, D4]
    )
    assert partial["performance_valid"] is False
    assert any("coverage" in reason for reason in partial["invalid_reasons"])


def test_settlement_configuration_validation() -> None:
    with pytest.raises(ValueError):
        DelistingSettlementConfig(recovery_rate=1.5)
    with pytest.raises(ValueError):
        DelistingSettlementConfig(recovery_rate=-0.1)
    with pytest.raises(ValueError):
        DelistingSettlementConfig(settlement_fee_bps=-1.0)
    cfg = DelistingSettlementConfig(recovery_rate=0.5, settlement_fee_bps=25.0)
    assert cfg.recovery_rate == 0.5
    assert cfg.settlement_fee_rate == pytest.approx(0.0025)
    assert BacktestConfig(delisting_settlement=cfg).delisting_settlement is cfg


def test_settlement_scale_and_input_order_invariance() -> None:
    dates = [D0, D1, D2, D3]
    prices = _prices(A_DELIST_PRICES)
    config = _cfg(bps=10.0)
    base = _settle_run(prices, dates, config)
    shuffled = _settle_run(
        prices.sample(frac=1, random_state=11).reset_index(drop=True),
        dates,
        deepcopy(config),
    )
    scaled = run_backtest(
        prices,
        dates,
        {D0: _target(D0, {"A": 1.0})},
        BacktestConfig(
            initial_nav=10.0,
            transaction_cost_bps=10.0,
            annualization=252,
            delisting_settlement=DelistingSettlementConfig(
                recovery_rate=1.0, settlement_fee_bps=0.0
            ),
        ),
        execution_lag_sessions=1,
        mode="strict",
        lifecycle=_monitor("A", D2),
        requested_period_start=D0,
        requested_period_end=D3,
    )

    assert [r.daily_return_net for r in base.records] == pytest.approx(
        [r.daily_return_net for r in shuffled.records]
    )
    assert [r.nav_net for r in scaled.records] == pytest.approx(
        [10 * r.nav_net for r in base.records]
    )
    base_row = next(e for e in base.settlement_events if e.book == "net")
    scaled_row = next(e for e in scaled.settlement_events if e.book == "net")
    assert scaled_row.last_mark_value == pytest.approx(10 * base_row.last_mark_value)
    assert scaled_row.settled_value == pytest.approx(10 * base_row.settled_value)

