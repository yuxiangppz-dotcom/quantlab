from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import run_backtest
from quantlab.backtest.lifecycle import LifecycleMonitor
from quantlab.backtest.models import BacktestConfig
from quantlab.data.models import DataValidationError, Security
from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.extended_economic_contract import (
    CONTROL,
    STRATEGIES,
    make_targets,
    scenarios,
    schedule,
    target_for_cross,
)
from quantlab.research.extended_economic_inputs import adjusted_marks
from quantlab.research.extended_economic_protocol import Ledger, frame_hash
from quantlab.research.extended_economic_results import write_result

D = date(2025, 9, 1)


def cross(n=25, day=D):
    return pd.DataFrame(
        {
            "instrument_id": [f"S{i:03d}" for i in range(n)],
            "trade_date": [pd.Timestamp(day)] * n,
            "score": [1.0] * n,
        }
    )


def test_exact_twenty_ties_are_label_and_row_order_independent():
    f = cross().sample(frac=1, random_state=17)
    f["future_return_5d"] = np.arange(len(f)) * 100
    f["future_price_available"] = False
    t = target_for_cross(f, D)
    assert [p.instrument_id for p in t.positions] == [f"S{i:03d}" for i in range(20)]
    assert all(p.target_weight == 0.04 for p in t.positions)
    assert t.cash_weight == pytest.approx(0.2)
    f["future_return_5d"] = -f.future_return_5d
    assert target_for_cross(f.iloc[::-1], D) == t


def test_control_denominator_keeps_every_prediction_member():
    f = cross(4)
    f.loc[0, "score"] = np.nan
    f["future_price_available"] = [False, True, True, True]
    t = target_for_cross(f, D, control=True)
    assert len(t.positions) == 4
    assert [p.target_weight for p in t.positions] == [0.2] * 4
    assert t.cash_weight == 0.2


def test_missing_signal_differs_from_explicit_cash_and_short_capacity():
    assert target_for_cross(None, D) is None
    assert target_for_cross(cross(0), D) is None
    t = target_for_cross(cross(3).assign(score=np.nan), D)
    assert t.positions == () and t.cash_weight == 1
    assert target_for_cross(cross(3), D).cash_weight == pytest.approx(0.88)


@pytest.mark.parametrize("score", [np.inf, -np.inf, np.nan])
def test_nonfinite_scores_cannot_select_a_winner(score):
    f = cross(3)
    f.loc[0, "score"] = score
    assert [p.instrument_id for p in target_for_cross(f, D).positions] == ["S001", "S002"]


@pytest.mark.parametrize("kind", ["duplicate", "null", "date", "empty_id"])
def test_bad_target_identity_fails(kind):
    f = cross(3)
    if kind == "duplicate":
        f.loc[1, "instrument_id"] = f.loc[0, "instrument_id"]
    elif kind == "null":
        f.loc[1, "instrument_id"] = None
    elif kind == "date":
        f.loc[1, "trade_date"] += pd.Timedelta(days=1)
    else:
        f.loc[1, "instrument_id"] = ""
    with pytest.raises(DataValidationError):
        target_for_cross(f, D)


def test_market_holiday_phase_lag_and_terminal_partial():
    days = [
        "2025-09-01",
        "2025-09-02",
        "2025-09-03",
        "2025-09-05",
        "2025-09-08",
        "2025-09-09",
        "2025-09-10",
        "2025-09-11",
    ]
    assert schedule(days, 5, signal_end=days[-2], end=days[-1]) == [D, date(2025, 9, 9)]
    with pytest.raises(DataValidationError):
        schedule(days[1:], 5, signal_end=days[-2], end=days[-1])
    with pytest.raises(DataValidationError):
        schedule(days + days[-1:], 5, signal_end=days[-2], end=days[-1])


def test_all_fixed_scenarios_have_one_identity_and_no_capital_duplicates():
    s = scenarios()
    assert len({x["id"] for x in s}) == len(s) == 60
    assert {x["friction_bps"] for x in s} == {0, 5, 10, 20}
    assert sum(x["policy"] == CONTROL for x in s) == 12
    assert all("capital" not in x for x in s)


def test_policy_mismatch_is_detected_without_outcome_filtering():
    f = cross().assign(
        complete_features=True,
        label_end_date=pd.Timestamp(D),
        future_return_5d=np.nan,
        label_reason="unknown",
    )
    policies = {name: f.copy() for name in STRATEGIES}
    policies["lightgbm_monthly"].loc[0, "label_reason"] = "different"
    with pytest.raises(DataValidationError, match="members or labels"):
        make_targets(policies, [str(D)])


def market():
    days = [D + timedelta(days=i) for i in range(4)]
    prices = pd.DataFrame(
        [
            {"instrument_id": i, "trade_date": d, "adj_close": p}
            for i, vals in [("A", [10, 20, 40, 40]), ("B", [10, 10, 10, 10])]
            for d, p in zip(days, vals, strict=True)
        ]
    )
    return days, prices


def target(day, code="A"):
    return TargetPortfolio(day, (TargetWeight(code, 0.8),), 0.2)


@pytest.mark.parametrize("capital", [1.0, 50000.0, 200000.0, 1000000.0])
@pytest.mark.parametrize("bps", [0, 5, 10, 20])
def test_next_close_self_financing_and_scale_invariance(capital, bps):
    days, p = market()
    result = run_backtest(p, days, {D: target(D)}, BacktestConfig(capital, bps))
    c = bps / 10000
    first_value = 1 / (1 + 0.8 * c)
    books = [b for b in result.books if b.book == "net"]
    assert books[0].nav == capital and not books[0].positions
    assert books[1].positions[0].last_price == 20
    assert books[1].nav / capital == pytest.approx(first_value, abs=1e-12)
    assert books[1].cash / capital == pytest.approx(0.2 * first_value, abs=1e-12)
    assert books[-1].nav / capital == pytest.approx(1.8 * first_value, abs=1e-12)
    assert len(books[-1].positions) == 1
    assert sum(b.fee for b in books) / capital == pytest.approx(0.8 * c * first_value, abs=1e-12)
    assert all(b.nav == pytest.approx(b.cash + sum(p.value for p in b.positions)) for b in books)


def test_absent_signal_does_not_liquidate_but_explicit_cash_does():
    days, p = market()
    stay = run_backtest(p, days, {D: target(D)}, BacktestConfig())
    exit_ = run_backtest(
        p, days, {D: target(D), days[1]: TargetPortfolio(days[1], (), 1)}, BacktestConfig()
    )
    assert stay.records[-1].holdings_count == 1
    assert exit_.records[-1].holdings_count == 0
    assert exit_.records[-1].cash_weight == 1


def test_missing_held_price_freezes_and_unavailable_new_target_stays_cash():
    days, p = market()
    p.loc[(p.instrument_id == "A") & (p.trade_date >= days[2]), "adj_close"] = np.nan
    p.loc[(p.instrument_id == "B") & (p.trade_date == days[2]), "adj_close"] = np.nan
    result = run_backtest(p, days, {D: target(D), days[1]: target(days[1], "B")}, BacktestConfig())
    final = [b for b in result.books if b.book == "net"][-1]
    assert final.positions[0].instrument_id == "A" and final.positions[0].value == pytest.approx(
        0.8
    )
    assert final.cash == pytest.approx(0.2)
    assert final.positions[0].missing_price
    assert result.rebalances[-1].frozen_count == 1
    assert result.rebalances[-1].unavailable_target_count == 1


def test_unsupported_held_lifecycle_stops_before_mark_without_settlement():
    days, p = market()
    security = Security("A", "A", "A", "SZSE", "SZ", "main", "D", D, days[1])
    result = run_backtest(
        p, days, {D: target(D)}, BacktestConfig(), lifecycle=LifecycleMonitor([security], [])
    )
    assert result.status == "blocked_by_unsupported_event"
    assert result.valid_through == days[1]
    assert not result.settlement_events
    assert result.first_blocking_event.position_value == pytest.approx(0.8)


def test_mark_units_and_unknowns_are_not_favorable_prices():
    raw = pd.DataFrame(
        {
            "instrument_id": ["A", "B", "C", "D", "E"],
            "trade_date": [D] * 5,
            "close": [10.0, 10.0, 10.0, 0.0, 10.0],
            "adj_factor": [2.0, 2.0, np.nan, 2.0, 2.0],
        }
    )
    inventory = {
        "instruments": list("ABCDE"),
        "list_dates": {i: str(D) for i in "ACDE"},
        "delist_dates": {"E": str(D - timedelta(days=1))},
    }
    marks = adjusted_marks(raw, inventory)
    assert marks.adj_close.iloc[0] == 20
    assert marks.adj_close.iloc[1:].isna().all()
    assert list(marks.mark_reason) == [
        "valid",
        "unknown_lifecycle",
        "invalid_factor",
        "invalid_close",
        "inactive_lifecycle",
    ]
    assert frame_hash(marks) == frame_hash(marks.copy())
    with pytest.raises(DataValidationError):
        adjusted_marks(pd.concat([raw, raw.iloc[:1]]), inventory)


def test_ledger_does_not_retry_failure_skip_slots_or_hide_files(tmp_path):
    ledger = Ledger(tmp_path, "fixed")
    a, b = scenarios()[:2]
    with pytest.raises(DataValidationError):
        ledger.start(b["id"])
    folder = ledger.start(a["id"])
    assert ledger.read(a["id"])["status"] == "interrupted"
    ledger.finish(a["id"], "failed", error="known failure")
    with pytest.raises(DataValidationError):
        ledger.start(a["id"])
    with pytest.raises(DataValidationError):
        ledger.start(b["id"])
    (folder / "hidden.txt").write_text("unreported")
    with pytest.raises(DataValidationError, match="hides"):
        ledger.read(a["id"])


def test_result_labels_and_terminal_positions_remain_inspectable(tmp_path):
    days, p = market()
    result = run_backtest(p, days, {D: target(D)}, BacktestConfig(1, 5))
    summary = write_result(
        tmp_path,
        result,
        scenarios()[1],
        {"fingerprint": "plan", "code_head": "source"},
        elapsed_seconds=0.1,
    )
    assert summary["terminal_open_positions"] == 1
    assert summary["complete_user_fee_accounting"] is False
    daily = pd.read_parquet(tmp_path / "daily.parquet")
    assert "value_after_declared_friction" in daily and "nav_net" not in daily
    positions = pd.read_parquet(tmp_path / "positions.parquet")
    assert positions.iloc[-1]["value"] == pytest.approx(result.books[-1].positions[0].value)
    assert summary["capital_scalings"]["50000"]["final_value"] == pytest.approx(
        50000 * summary["final_value_after_declared_friction"]
    )
    with pytest.raises(DataValidationError):
        write_result(
            tmp_path,
            replace(result, run_mode="diagnostic"),
            scenarios()[1],
            {"fingerprint": "plan", "code_head": "source"},
            elapsed_seconds=0.1,
        )
