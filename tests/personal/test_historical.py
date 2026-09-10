import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_account import HEADER
from test_cash_flow_tracking import _flow, _seed
from test_manual_fill_time import _fill, _write_rehashed

from quantlab.personal import import_manual_cash_flows, import_manual_fills, replay_account_at
from quantlab.personal.account import import_account_csv, load_account


def _at(root, storage, value, **kwargs):
    return replay_account_at(
        "mine", datetime.fromisoformat(value), account_root=root, storage=storage, **kwargs
    )


def _tree_hash(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_inclusive_economic_cutoff_discloses_late_reports_and_never_writes(tmp_path):
    root, storage = _seed(tmp_path)
    import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-08T10:00:00+08:00", "DEPOSIT", "500"),
        account_root=root,
        storage=storage,
    )
    before = _tree_hash(tmp_path)
    prior = _at(root, storage, "2026-09-07T09:59:59+08:00")
    assert prior["cash_fen"] == 200000
    assert prior["excluded_later_event_count"] == 2
    exact = _at(root, storage, "2026-09-07T10:00:00+08:00")
    assert exact["cash_fen"] == 99500
    assert exact["positions"][0] == {
        "instrument_id": "000001.SZ",
        "quantity": 300,
        "sellable_quantity": 200,
    }
    assert exact["late_reported_event_count"] == 1
    assert exact["latest_included_reported_at"] == "2026-09-09T20:00:00+08:00"
    assert not exact["knowledge_cutoff_supported"] and not exact["performance_eligible"]
    assert exact == _at(root, storage, "2026-09-07T02:00:00+00:00")
    assert before == _tree_hash(tmp_path)


def test_t_plus_one_progresses_at_cutoff_without_new_events(tmp_path):
    root, storage = _seed(tmp_path)
    import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    next_day = _at(root, storage, "2026-09-08T09:00:00+08:00")
    assert next_day["latest_included_economic_at"] == "2026-09-07T10:00:00+08:00"
    assert next_day["positions"][0]["sellable_quantity"] == 300
    assert next_day["cash_fen"] == 99500


def test_deposit_and_withdrawal_are_sliced_at_economic_time(tmp_path):
    root, storage = _seed(tmp_path)
    import_manual_cash_flows(
        "mine",
        _flow(
            "D1",
            "2026-09-09T10:00:00+08:00",
            "DEPOSIT",
            "500",
            effective_at="2026-09-07T10:00:00+08:00",
        ),
        account_root=root,
        storage=storage,
    )
    import_manual_cash_flows(
        "mine",
        _flow("W1", "2026-09-08T10:00:00+08:00", "WITHDRAWAL", "300"),
        account_root=root,
        storage=storage,
    )
    assert _at(root, storage, "2026-09-07T10:00:00+08:00")["cash_fen"] == 250000
    later = _at(root, storage, "2026-09-08T10:00:00+08:00")
    assert later["cash_fen"] == 220000 and later["late_reported_event_count"] == 1


def test_archived_basis_uses_its_own_journal_without_switching_active_snapshot(tmp_path):
    root, storage = _seed(tmp_path)
    old = load_account("mine", account_root=root)
    import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    import_account_csv(
        (
            HEADER + "mine,manual_tracking,2026-09-08T09:00:00+08:00,5000,,0,0,,none_declared\n"
        ).encode(),
        account_root=root,
    )
    before = _tree_hash(root)
    blocked = _at(root, storage, "2026-09-07T10:00:00+08:00")
    assert "cutoff_precedes_opening_basis" in blocked["blocked_reasons"]
    history = _at(
        root, storage, "2026-09-07T10:00:00+08:00", opening_fingerprint=old["account_fingerprint"]
    )
    assert history["cash_fen"] == 99500
    assert _at(root, storage, "2026-09-08T10:00:00+08:00")["cash_fen"] == 500000
    assert before == _tree_hash(root)


@pytest.mark.parametrize(
    "cutoff,blocked",
    [
        ("2026-09-07T09:00:00+08:00", False),
        ("2026-09-08T09:00:00+08:00", True),
    ],
)
def test_legacy_fill_is_not_treated_as_absent_just_because_report_is_late(
    tmp_path, cutoff, blocked
):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills(
        "mine",
        _fill(executed="2026-09-08T10:00:00+08:00", trade_date="2026-09-08"),
        account_root=root,
        storage=storage,
    )
    payload = json.loads(path.read_text())
    payload["schema"] = "quantlab_manual_tracking_journal_v3"
    row = payload["events"][0]
    row["occurred_at"] = row["reported_at"]
    for key in ("executed_at", "reported_at", "timing_quality"):
        row.pop(key)
    _write_rehashed(path, payload)
    result = _at(root, storage, cutoff)
    if blocked:
        assert result["status"] == "blocked" and result["cash_fen"] is None
        assert "legacy_fill_execution_time_unknown" in result["blocked_reasons"]
    else:
        assert result["cash_fen"] == 200000
        assert result["excluded_later_event_count"] == 1


def test_even_future_legacy_cash_flow_report_blocks_exact_cutoff(tmp_path):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-09T10:00:00+08:00", "DEPOSIT", "500"),
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
    result = _at(root, storage, "2026-09-07T10:00:00+08:00")
    assert result["cash_fen"] is None and result["positions"] is None
    assert "legacy_cash_flow_effective_time_unknown" in result["blocked_reasons"]


def test_missing_calendar_day_inside_interval_is_unknown_not_closed(tmp_path):
    root, storage = _seed(tmp_path)
    entries = [row for row in storage.load_trading_calendar() if row.trade_date.day != 8]
    storage.save_trading_calendar(entries)
    result = _at(root, storage, "2026-09-09T10:00:00+08:00")
    assert "calendar_has_unverified_days_inside_replay_interval" in result["blocked_reasons"]
    assert result["cash_fen"] is None


@pytest.mark.parametrize("mutation", ["journal", "archive"])
def test_corrupt_evidence_is_rejected_before_any_state_is_returned(tmp_path, mutation):
    root, storage = _seed(tmp_path)
    account = load_account("mine", account_root=root)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    if mutation == "archive":
        path = root / "mine/snapshots" / f"{account['account_fingerprint']}.json"
    payload = json.loads(path.read_text())
    payload["cash_fen"] = 123
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="fingerprint"):
        _at(
            root,
            storage,
            "2026-09-08T10:00:00+08:00",
            opening_fingerprint=account["account_fingerprint"],
        )


@pytest.mark.parametrize(
    "cutoff,pattern",
    [
        (datetime(2026, 9, 7), "timezone-aware"),
        (datetime.now(UTC) + timedelta(days=365), "future"),
    ],
)
def test_invalid_cutoff_is_rejected_before_loading_any_account(tmp_path, cutoff, pattern):
    with pytest.raises(ValueError, match=pattern):
        replay_account_at("missing", cutoff, account_root=tmp_path)
    assert not list(tmp_path.iterdir())
