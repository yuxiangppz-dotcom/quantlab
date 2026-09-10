import json
from datetime import datetime

import pytest
from test_cash_flow_tracking import _flow, _seed
from test_historical import _tree_hash
from test_manual_fill_time import _fill, _write_rehashed
from test_valuation_checkpoint import _setup

from quantlab.data.models import TradingCalendar
from quantlab.personal import (
    import_manual_cash_flows,
    import_manual_fills,
    inspect_performance_inputs,
    materialize_valuation_checkpoint,
)
from quantlab.personal import performance_inputs as module


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.fromisoformat("2026-09-09T23:00:00+08:00").astimezone(tz)


@pytest.fixture(autouse=True)
def fixed_time(monkeypatch):
    monkeypatch.setattr(module, "datetime", FixedDatetime)


def _checks(result):
    return {row["check_id"]: row["status"] for row in result["checks"]}


def _valued(tmp_path, *, quantity=100):
    root, product, storage, _ = _setup(tmp_path, quantity=quantity)
    storage.save_trading_calendar(
        [TradingCalendar("SZSE", datetime(2026, 9, day).date(), True) for day in (7, 8, 9)]
    )
    path, value, _ = materialize_valuation_checkpoint(
        "mine",
        account_root=root,
        product_root=product,
        storage=storage,
    )
    return root, storage, path, value


def test_exact_timing_does_not_certify_performance_or_corporate_actions(tmp_path):
    root, storage, _, _ = _valued(tmp_path)
    before = _tree_hash(tmp_path)
    result = inspect_performance_inputs("mine", account_root=root, storage=storage)
    checks = _checks(result)
    assert checks["fill_execution_timing"] == checks["external_cash_flow_timing"] == "pass"
    assert checks["valuation_account_binding"] == checks["daily_close_cutoff"] == "pass"
    assert checks["raw_price_source"] == "pass"
    assert checks["corporate_action_completeness"] == checks["broker_reconciliation"] == "unknown"
    assert checks["performance_method"] == "blocked"
    assert result["status"] == "blocked" and result["performance_eligible"] is False
    assert result["performance_claim"] is False and result["broker_order_authority"] is False
    assert before == _tree_hash(tmp_path)
    assert result == inspect_performance_inputs("mine", account_root=root, storage=storage)


def test_cash_only_does_not_invent_price_or_reconciliation_evidence(tmp_path):
    root, storage, _, _ = _valued(tmp_path, quantity=0)
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["corporate_action_completeness"] == checks["raw_price_source"] == "pass"
    assert checks["broker_reconciliation"] == "unknown"
    assert checks["performance_method"] == "blocked"


def test_exact_cash_flow_still_requires_boundary_valuations(tmp_path):
    root, storage = _seed(tmp_path)
    import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-08T10:00:00+08:00", "DEPOSIT", "500"),
        account_root=root,
        storage=storage,
    )
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["external_cash_flow_timing"] == "pass"
    assert checks["cash_flow_boundary_valuations"] == "unknown"
    assert checks["valuation_checkpoint"] == checks["daily_close_cutoff"] == "unknown"


def test_legacy_fill_timing_is_separate_from_good_cash_flow_timing(tmp_path):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    payload = json.loads(path.read_text())
    payload["schema"] = "quantlab_manual_tracking_journal_v3"
    row = payload["events"][0]
    row["occurred_at"] = row["reported_at"]
    for key in ("executed_at", "reported_at", "timing_quality"):
        row.pop(key)
    _write_rehashed(path, payload)
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["fill_execution_timing"] == "unknown"
    assert checks["external_cash_flow_timing"] == "pass"
    assert checks["economic_replay"] == "blocked"


def test_legacy_cash_flow_timing_remains_unknown(tmp_path):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-08T10:00:00+08:00", "DEPOSIT", "500"),
        account_root=root,
        storage=storage,
    )
    payload = json.loads(path.read_text())
    payload["schema"] = "quantlab_manual_tracking_journal_v2"
    row = payload["events"][0]
    row["occurred_at"] = row.pop("effective_at")
    row.pop("reported_at")
    row.pop("timing_quality")
    _write_rehashed(path, payload)
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["external_cash_flow_timing"] == "unknown"
    assert checks["economic_replay"] == "blocked"


@pytest.mark.parametrize("hour,save,expected", [(13, False, "pass"), (16, True, "blocked")])
def test_changed_state_and_post_close_fact_have_separate_gates(tmp_path, hour, save, expected):
    root, storage, _, _ = _valued(tmp_path)
    import_manual_cash_flows(
        "mine",
        _flow("D1", f"2026-09-09T{hour}:00:00+08:00", "DEPOSIT", "500"),
        account_root=root,
        storage=storage,
    )
    if save:
        materialize_valuation_checkpoint(
            "mine", account_root=root, product_root=tmp_path / "products", storage=storage
        )
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["valuation_checkpoint"] == "pass"
    assert checks["valuation_account_binding"] == ("pass" if save else "blocked")
    assert checks["daily_close_cutoff"] == expected


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_price_partition_not_silently_trusted(tmp_path, mutation):
    root, storage, _, value = _valued(tmp_path)
    path = storage.daily_bars_path(datetime.fromisoformat(value["price_date"]).date())
    if mutation == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    checks = _checks(inspect_performance_inputs("mine", account_root=root, storage=storage))
    assert checks["raw_price_source"] == ("unknown" if mutation == "missing" else "blocked")


def test_corrupt_checkpoint_is_rejected_without_returning_partial_success(tmp_path):
    root, storage, path, value = _valued(tmp_path)
    value["nav_fen"] += 100
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="NAV arithmetic"):
        inspect_performance_inputs("mine", account_root=root, storage=storage)


def test_source_drift_during_inspection_is_rejected(tmp_path, monkeypatch):
    root, storage, _, _ = _valued(tmp_path)
    original = module.replay_account_at

    def drifting(*args, **kwargs):
        result = original(*args, **kwargs)
        import_manual_cash_flows(
            "mine",
            _flow("D1", "2026-09-09T13:00:00+08:00", "DEPOSIT", "500"),
            account_root=root,
            storage=storage,
        )
        return result

    monkeypatch.setattr(module, "replay_account_at", drifting)
    with pytest.raises(ValueError, match="evidence changed"):
        inspect_performance_inputs("mine", account_root=root, storage=storage)
