from __future__ import annotations

import hashlib
import json

import pytest
from test_cash_flow_tracking import _flow, _seed

from quantlab.execution.ledger import LedgerAccountingError
from quantlab.personal import (
    import_manual_cash_flows,
    import_manual_fills,
    load_effective_account,
    load_tracking_summary,
    preview_manual_fills,
)
from quantlab.personal.tracking import FILL_COLUMNS


def _fill(*, executed="2026-09-07T10:00:00+08:00", reported="2026-09-09T20:00:00+08:00",
          side="BUY", trade_date="2026-09-07"):
    return (
        ",".join(FILL_COLUMNS) + "\n"
        + f"mine,B1,{trade_date},{executed},{reported},000001.SZ,{side},100,10.00,1000.00,5.00\n"
    ).encode()


def _write_rehashed(path, payload):
    payload["events_fingerprint"] = hashlib.sha256(
        json.dumps(payload["events"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload.pop("journal_fingerprint", None)
    payload["journal_fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(payload))


def test_delayed_report_does_not_advance_economic_state_or_t_plus_one(tmp_path):
    root, storage = _seed(tmp_path)
    preview = preview_manual_fills("mine", _fill(), account_root=root, storage=storage)
    assert preview["as_of"] == "2026-09-07T10:00:00+08:00"
    assert preview["latest_reported_at"] == "2026-09-09T20:00:00+08:00"
    assert preview["cash_fen"] == 99500
    assert preview["positions"] == [
        {"instrument_id": "000001.SZ", "quantity": 300, "sellable_quantity": 200},
    ]
    assert preview["fill_timing_quality"] == "exact_execution_time"
    assert preview["intraday_timing_eligible"] is True
    assert not list(root.glob("mine/tracking/*/journal.json"))
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    payload = json.loads(path.read_text())
    assert payload["schema"] == "quantlab_manual_tracking_journal_v4"
    event = payload["events"][0]
    assert event["occurred_at"] == event["executed_at"] == preview["as_of"]
    assert event["buy_lot_sellable_from"] == "2026-09-08"
    account = load_effective_account("mine", account_root=root, storage=storage)
    assert account["fill_timing_quality"] == "exact_execution_time"
    assert account["latest_reported_at"] == preview["latest_reported_at"]


@pytest.mark.parametrize("changes,pattern", [
    ({"reported": "2026-09-07T09:59:59+08:00"}, "cannot precede"),
    ({"executed": "2026-09-07T10:00:00"}, "timezone"),
    ({"reported": "2026-09-09T20:00:00"}, "timezone"),
    ({"executed": "2026-09-08T10:00:00+08:00"}, "trade_date"),
    ({"executed": "2026-09-07T08:59:59+08:00"}, "precedes opening"),
])
def test_invalid_timing_rejected_before_journal_write(tmp_path, changes, pattern):
    root, storage = _seed(tmp_path)
    with pytest.raises(ValueError, match=pattern):
        import_manual_fills("mine", _fill(**changes), account_root=root, storage=storage)
    assert not list(root.glob("mine/tracking/*/journal.json"))


def test_utc_execution_is_compared_on_shanghai_trade_date(tmp_path):
    root, storage = _seed(tmp_path)
    result = preview_manual_fills(
        "mine", _fill(executed="2026-09-07T02:00:00+00:00"), account_root=root, storage=storage,
    )
    assert result["as_of"] == "2026-09-07T10:00:00+08:00"


def test_sale_before_withdrawal_replays_by_execution_even_when_reported_later(tmp_path):
    root, storage = _seed(tmp_path, cash_cny="0.00")
    import_manual_fills("mine", _fill(side="SELL"), account_root=root, storage=storage)
    _, summary = import_manual_cash_flows(
        "mine", _flow("W1", "2026-09-07T11:00:00+08:00", "WITHDRAWAL", "900.00"),
        account_root=root, storage=storage,
    )
    assert summary["cash_fen"] == 9500
    assert summary["as_of"] == "2026-09-07T11:00:00+08:00"
    assert summary["latest_reported_at"] == "2026-09-09T20:00:00+08:00"


def test_later_deposit_cannot_fund_an_earlier_delayed_buy(tmp_path):
    root, storage = _seed(tmp_path, cash_cny="0.00")
    path, _ = import_manual_cash_flows(
        "mine", _flow("D1", "2026-09-07T11:00:00+08:00", "DEPOSIT", "2000.00"),
        account_root=root, storage=storage,
    )
    before = path.read_bytes()
    with pytest.raises(LedgerAccountingError, match="cash"):
        import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"executed": "2026-09-07T10:01:00+08:00"},
    {"reported": "2026-09-09T20:01:00+08:00"},
])
def test_changed_timing_under_same_broker_id_is_not_a_duplicate(tmp_path, changes):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    before = path.read_bytes()
    _, duplicate = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    assert duplicate["duplicate_count"] == 1
    with pytest.raises(ValueError, match="different economics"):
        import_manual_fills("mine", _fill(**changes), account_root=root, storage=storage)
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_legacy_versions_replay_without_claiming_exact_execution(tmp_path, version):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    payload = json.loads(path.read_text())
    payload["schema"] = (
        "quantlab_manual_fill_journal_v1" if version == 1
        else f"quantlab_manual_tracking_journal_v{version}"
    )
    event = payload["events"][0]
    event["occurred_at"] = event["reported_at"]
    for key in ("executed_at", "reported_at", "timing_quality"):
        event.pop(key)
    if version == 1:
        event.pop("event_type")
    _write_rehashed(path, payload)
    before = path.read_bytes()
    summary = load_tracking_summary("mine", account_root=root, storage=storage)
    assert summary["as_of"] == "2026-09-09T20:00:00+08:00"
    assert summary["positions"][0]["sellable_quantity"] == 300
    assert summary["fill_timing_quality"] == "legacy_reported_time_used_as_execution_unverified"
    assert summary["intraday_timing_eligible"] is False
    assert summary["fills"][0]["executed_at"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["downgrade", "execution_mismatch", "legacy_claim"])
def test_rehashed_semantic_timing_tampering_fails_closed(tmp_path, mutation):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    payload = json.loads(path.read_text())
    if mutation == "downgrade":
        payload["schema"] = "quantlab_manual_tracking_journal_v3"
    elif mutation == "execution_mismatch":
        payload["events"][0]["executed_at"] = "2026-09-07T11:00:00+08:00"
    else:
        payload["events"][0]["timing_quality"] = (
            "legacy_reported_time_used_as_execution_unverified"
        )
    _write_rehashed(path, payload)
    with pytest.raises(ValueError, match="v4|executed_at"):
        load_tracking_summary("mine", account_root=root, storage=storage)


def test_missing_v4_timing_cannot_be_silently_treated_as_legacy(tmp_path):
    root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fill(), account_root=root, storage=storage)
    payload = json.loads(path.read_text())
    payload["events"][0].pop("executed_at")
    _write_rehashed(path, payload)
    with pytest.raises(ValueError, match="v4 fill timing"):
        load_tracking_summary("mine", account_root=root, storage=storage)


def test_old_csv_is_rejected_instead_of_inventing_execution_time(tmp_path):
    root, storage = _seed(tmp_path)
    old = _fill().decode().replace("executed_at,", "").replace("2026-09-07T10:00:00+08:00,", "")
    with pytest.raises(ValueError, match="columns must exactly equal"):
        preview_manual_fills("mine", old.encode(), account_root=root, storage=storage)


def test_report_time_is_bound_in_tracking_identity(tmp_path):
    root, storage = _seed(tmp_path)
    first = preview_manual_fills("mine", _fill(), account_root=root, storage=storage)
    second = preview_manual_fills(
        "mine", _fill(reported="2026-09-09T21:00:00+08:00"), account_root=root, storage=storage,
    )
    assert first["cash_fen"] == second["cash_fen"]
    assert first["tracking_fingerprint"] != second["tracking_fingerprint"]
