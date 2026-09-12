import json
from datetime import date

import pandas as pd
import pytest

from quantlab.data.models import DataValidationError
from quantlab.portfolio.models import TargetPortfolio, TargetWeight
from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.extended_economic_contract import OUT, scenarios, target_manifest
from quantlab.research.extended_economic_protocol import Ledger, frame_hash, load_targets
from quantlab.research.extended_economic_results import write_result
from quantlab.research.input_audit import _sha


def test_marks_hash_survives_parquet_and_detects_value_or_row_change(tmp_path):
    f = pd.DataFrame(
        {
            "instrument_id": ["A", "B"],
            "trade_date": [date(2025, 9, 1)] * 2,
            "adj_close": [1.345678901234567, None],
            "mark_reason": ["valid", "unknown"],
        }
    )
    path = tmp_path / "marks.parquet"
    f.to_parquet(path, index=False)
    assert frame_hash(f) == frame_hash(pd.read_parquet(path))
    assert frame_hash(f) != frame_hash(f.iloc[::-1])
    changed = f.copy()
    changed.loc[0, "adj_close"] += 0.000000001
    assert frame_hash(f) != frame_hash(changed)


def test_target_roundtrip_is_exact_and_detects_name_and_contents(tmp_path):
    name = "ridge_weekly_h5"
    day = date(2025, 9, 1)
    targets = {day: TargetPortfolio(day, (TargetWeight("A", 0.04),), 0.96)}
    path = tmp_path / "targets" / f"{name}.json"
    atomic_seal(
        path, {"name": name, "targets": {str(day): {"cash_weight": 0.96, "positions": {"A": 0.04}}}}
    )
    plan = {
        "sources": {"targets": target_manifest({name: targets})},
        "artifacts": {f"targets/{name}.json": {"sha256": _sha(path), "bytes": path.stat().st_size}},
    }
    assert load_targets(tmp_path, plan, name) == targets
    with pytest.raises(DataValidationError):
        load_targets(tmp_path, plan, "undeclared")
    plan["sources"]["targets"][name]["fingerprint"] = "changed"
    with pytest.raises(DataValidationError):
        load_targets(tmp_path, plan, name)


def test_terminal_report_cannot_hide_later_reservation(tmp_path):
    from quantlab.research.extended_economic import publish

    out = tmp_path / OUT
    out.mkdir(parents=True)
    plan = {"fingerprint": "fixed", "code_head": "source"}
    ledger = Ledger(out, "fixed")
    first = scenarios()[0]["id"]
    ledger.start(first)
    ledger.finish(first, "failed", error="preserved")
    old = publish(tmp_path, plan)
    assert len(old["scenarios"]) == 60 and old["finished_paths"] == 0
    second = scenarios()[1]["id"]
    atomic_seal(
        ledger.base / second / "started.json",
        {"identity": "fixed", "scenario": second, "reservation": 2},
    )
    with pytest.raises(DataValidationError, match="terminal economic report changed"):
        publish(tmp_path, plan)


def test_ledger_cannot_replace_closed_outputs_or_reinvoke_success(tmp_path):
    ledger = Ledger(tmp_path, "fixed")
    name = scenarios()[0]["id"]
    folder = ledger.start(name)
    (folder / "value.txt").write_text("original")
    ledger.finish(name, "finished")
    with pytest.raises(DataValidationError):
        ledger.start(name)
    (folder / "value.txt").write_text("changed")
    with pytest.raises(DataValidationError, match="bound file changed"):
        ledger.read(name)


def test_terminal_holding_interval_is_explicit(tmp_path):
    from quantlab.backtest.engine import run_backtest
    from quantlab.backtest.models import BacktestConfig

    days = [date(2025, 9, d) for d in (1, 2, 3, 4)]
    p = pd.DataFrame(
        {"instrument_id": ["A"] * 4, "trade_date": days, "adj_close": [10.0, 10.0, 10.0, 10.0]}
    )
    t = {days[0]: TargetPortfolio(days[0], (TargetWeight("A", 0.8),), 0.2)}
    result = run_backtest(p, days, t, BacktestConfig())
    s = write_result(
        tmp_path,
        result,
        scenarios()[0],
        {"fingerprint": "p", "code_head": "h", "sources": {"sessions": [str(d) for d in days]}},
        elapsed_seconds=0.1,
    )
    assert s["last_rebalance_value_date"] == "2025-09-02"
    assert s["observed_sessions_since_last_rebalance"] == 2
    assert s["terminal_planned_holding_interval_complete"] is False
    assert s["terminal_open_positions"] == 1
    assert json.loads((tmp_path / "accounting.json").read_text())["settlement_events"] == []
