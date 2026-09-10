from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DailyBar, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv
from quantlab.personal.tracking import (
    build_tracking_valuation,
    import_manual_cash_flows,
    import_manual_fills,
    load_effective_account,
    load_tracking_summary,
    preview_manual_cash_flows,
    preview_manual_fills,
)

ACCOUNT_HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)
FLOW_HEADER = (
    "account_id,external_flow_id,effective_at,reported_at,direction,amount_cny\n"
)
FILL_HEADER = (
    "account_id,broker_trade_id,trade_date,executed_at,reported_at,instrument_id,side,"
    "quantity,price_cny,gross_notional_cny,fee_cny\n"
)


def _seed(tmp_path: Path, *, cash_cny: str = "2000.00") -> tuple[Path, ParquetStorage]:
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            ACCOUNT_HEADER
            + f"mine,manual_tracking,2026-09-07T09:00:00+08:00,{cash_cny},"
            "000001.SZ,200,200,8.00,none_declared\n"
        ).encode(),
        account_root=account_root,
    )
    storage = ParquetStorage(tmp_path / "canonical")
    storage.save_securities(
        [
            Security(
                "000001.SZ",
                "000001",
                "甲",
                "SZSE",
                "SZ",
                "主板",
                "L",
                date(2000, 1, 1),
                None,
            )
        ]
    )
    storage.save_trading_calendar(
        [
            TradingCalendar("SZSE", day, True)
            for day in (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
        ]
    )
    return account_root, storage


def _flow(
    flow_id: str,
    reported_at: str,
    direction: str,
    amount: str,
    *,
    effective_at: str | None = None,
) -> bytes:
    effective = effective_at or reported_at
    return (
        FLOW_HEADER
        + f"mine,{flow_id},{effective},{reported_at},{direction},{amount}\n"
    ).encode()


def _fill(
    broker_id: str,
    reported_at: str,
    quantity: int,
    price: str = "10.00",
    fee: str = "5.00",
) -> bytes:
    gross = f"{quantity * float(price):.2f}"
    return (
        FILL_HEADER
        + f"mine,{broker_id},2026-09-07,{reported_at},{reported_at},000001.SZ,BUY,"
        + f"{quantity},{price},{gross},{fee}\n"
    ).encode()


def test_deposit_preview_is_read_only_then_updates_effective_cash_only(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    source = _flow("D1", "2026-09-07T14:00:00+08:00", "DEPOSIT", "500.00")

    preview = preview_manual_cash_flows(
        "mine", source, account_root=account_root, storage=storage
    )
    assert preview["accepted_count"] == 1
    assert preview["cash_fen"] == 250_000
    assert preview["external_cash_flow_net_fen"] == 50_000
    assert preview["cash_flow_timing_quality"] == "exact_effective_time"
    assert preview["cash_flow_timing_performance_eligible"] is True
    assert preview["positions"] == [
        {"instrument_id": "000001.SZ", "quantity": 200, "sellable_quantity": 200}
    ]
    assert not list(account_root.glob("mine/tracking/*/journal.json"))

    path, committed = import_manual_cash_flows(
        "mine",
        source,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    payload = json.loads(path.read_text())
    assert payload["schema"] == "quantlab_manual_tracking_journal_v3"
    assert payload["events"][0]["event_type"] == "external_cash_flow"
    assert payload["events"][0]["effective_at"] == "2026-09-07T14:00:00+08:00"
    assert payload["events"][0]["reported_at"] == "2026-09-07T14:00:00+08:00"
    assert committed["cash_fen"] == 250_000

    effective = load_effective_account("mine", account_root=account_root, storage=storage)
    assert effective["cash_fen"] == 250_000
    assert effective["positions"][0]["quantity"] == 200
    assert effective["external_cash_flow_net_fen"] == 50_000
    assert effective["source"] == "opening_snapshot_plus_manual_tracking_journal"
    assert effective["cash_flow_timing_quality"] == "exact_effective_time"


def test_withdrawal_is_replayed_and_insufficient_withdrawal_is_atomic(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    valid = _flow("W1", "2026-09-07T14:00:00+08:00", "WITHDRAWAL", "500.00")
    path, _ = import_manual_cash_flows(
        "mine",
        valid,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    before = path.read_bytes()
    effective = load_effective_account("mine", account_root=account_root, storage=storage)
    assert effective["cash_fen"] == 150_000

    impossible = _flow("W2", "2026-09-07T15:00:00+08:00", "WITHDRAWAL", "2000.00")
    with pytest.raises(ValueError, match="exceeds settled cash"):
        import_manual_cash_flows(
            "mine",
            impossible,
            account_root=account_root,
            product_root=tmp_path / "products",
            storage=storage,
        )
    assert path.read_bytes() == before
    effective = load_effective_account("mine", account_root=account_root, storage=storage)
    assert effective["cash_fen"] == 150_000


def test_cash_flow_duplicate_is_idempotent_and_changed_economics_conflicts(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    source = _flow("D1", "2026-09-07T14:00:00+08:00", "DEPOSIT", "500.00")
    path, _ = import_manual_cash_flows(
        "mine",
        source,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    before = path.read_bytes()

    same_path, duplicate = import_manual_cash_flows(
        "mine",
        source,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    assert same_path == path
    assert duplicate["accepted_count"] == 0
    assert duplicate["duplicate_count"] == 1
    assert path.read_bytes() == before

    changed = _flow("D1", "2026-09-07T14:00:00+08:00", "DEPOSIT", "600.00")
    with pytest.raises(ValueError, match="reused with different economics"):
        preview_manual_cash_flows(
            "mine", changed, account_root=account_root, storage=storage
        )

    changed_time = _flow(
        "D1",
        "2026-09-07T14:00:00+08:00",
        "DEPOSIT",
        "500.00",
        effective_at="2026-09-07T13:59:00+08:00",
    )
    with pytest.raises(ValueError, match="reused with different economics"):
        preview_manual_cash_flows(
            "mine", changed_time, account_root=account_root, storage=storage
        )


def test_cash_flow_and_fill_replay_follow_effective_time_not_import_order(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path, cash_cny="1000.00")
    deposit = _flow(
        "D1",
        "2026-09-07T16:00:00+08:00",
        "DEPOSIT",
        "1000.00",
        effective_at="2026-09-07T14:00:00+08:00",
    )
    import_manual_cash_flows(
        "mine",
        deposit,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    buy = _fill("B1", "2026-09-07T15:00:00+08:00", 150)
    import_manual_fills(
        "mine",
        buy,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    summary = load_tracking_summary("mine", account_root=account_root, storage=storage)
    assert summary["cash_fen"] == 49_500
    assert summary["fill_event_count"] == 1
    assert summary["cash_flow_event_count"] == 1
    assert summary["positions"][0]["quantity"] == 350
    assert summary["latest_economic_fact_at"] == "2026-09-07T15:00:00+08:00"
    assert summary["latest_reported_at"] == "2026-09-07T16:00:00+08:00"

    account_root2, storage2 = _seed(tmp_path / "late", cash_cny="1000.00")
    late_deposit = _flow(
        "D1",
        "2026-09-07T16:30:00+08:00",
        "DEPOSIT",
        "1000.00",
        effective_at="2026-09-07T16:00:00+08:00",
    )
    import_manual_cash_flows(
        "mine",
        late_deposit,
        account_root=account_root2,
        product_root=tmp_path / "late" / "products",
        storage=storage2,
    )
    early_buy = _fill("B1", "2026-09-07T15:00:00+08:00", 150)
    with pytest.raises(Exception, match="cash|insufficient|negative"):
        preview_manual_fills(
            "mine", early_buy, account_root=account_root2, storage=storage2
        )


def test_legacy_fill_only_v1_replays_and_upgrades_without_losing_fill(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    fill = _fill("B1", "2026-09-07T15:00:00+08:00", 100)
    path, _ = import_manual_fills(
        "mine",
        fill,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    legacy = json.loads(path.read_text())
    assert legacy["schema"] == "quantlab_manual_tracking_journal_v4"
    legacy["schema"] = "quantlab_manual_fill_journal_v1"
    for item in legacy["events"]:
        for key in ("event_type", "executed_at", "reported_at", "timing_quality"):
            item.pop(key)
    legacy["events_fingerprint"] = hashlib.sha256(
        json.dumps(legacy["events"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    legacy.pop("journal_fingerprint")
    legacy["journal_fingerprint"] = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(legacy))
    before = load_tracking_summary("mine", account_root=account_root, storage=storage)
    assert before["fill_event_count"] == 1
    assert before["cash_flow_event_count"] == 0
    assert before["fill_timing_performance_eligible"] is False

    flow = _flow("D1", "2026-09-07T16:00:00+08:00", "DEPOSIT", "100.00")
    import_manual_cash_flows(
        "mine",
        flow,
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    payload = json.loads(path.read_text())
    assert payload["schema"] == "quantlab_manual_tracking_journal_v4"
    assert {item["event_type"] for item in payload["events"]} == {
        "manual_fill",
        "external_cash_flow",
    }
    after = load_tracking_summary("mine", account_root=account_root, storage=storage)
    assert after["fill_event_count"] == 1
    assert after["cash_flow_event_count"] == 1
    assert after["fill_timing_performance_eligible"] is False
    assert after["cash_flow_timing_performance_eligible"] is True


def test_combined_journal_tampering_is_rejected(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    path, _ = import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-07T14:00:00+08:00", "DEPOSIT", "100.00"),
        account_root=account_root,
        product_root=tmp_path / "products",
        storage=storage,
    )
    payload = json.loads(path.read_text())
    payload["events"][0]["amount_fen"] = 999_999
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="event fingerprint mismatch"):
        load_tracking_summary("mine", account_root=account_root, storage=storage)


def test_old_cash_flow_csv_contract_is_rejected_instead_of_silent_downgrade(
    tmp_path: Path,
) -> None:
    account_root, storage = _seed(tmp_path)
    old = (
        b"account_id,external_flow_id,reported_at,direction,amount_cny\n"
        b"mine,D1,2026-09-07T14:00:00+08:00,DEPOSIT,100.00\n"
    )
    with pytest.raises(ValueError, match="columns must exactly equal"):
        preview_manual_cash_flows("mine", old, account_root=account_root, storage=storage)


def test_valuation_with_external_cash_flow_never_emits_simple_return(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    opening_day = date(2026, 9, 4)
    current_day = date(2026, 9, 7)
    for day, close in ((opening_day, 8.0), (current_day, 9.0)):
        storage.save_daily_bars_by_date(
            [DailyBar("000001.SZ", day, close, close, close, close, close, 1, close)],
            day,
        )
    product_root = tmp_path / "products"
    opening = product_root / opening_day.isoformat()
    opening.mkdir(parents=True)
    (opening / "report.json").write_text(
        '{"effective_as_of":"2026-09-04","content_fingerprint":"opening"}'
    )
    import_manual_cash_flows(
        "mine",
        _flow("D1", "2026-09-07T12:00:00+08:00", "DEPOSIT", "500.00"),
        account_root=account_root,
        product_root=product_root,
        storage=storage,
    )
    current = product_root / current_day.isoformat()
    current.mkdir()
    (current / "report.json").write_text(
        '{"effective_as_of":"2026-09-07","content_fingerprint":"current"}'
    )

    valued = build_tracking_valuation(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert valued["status"] == "complete_mark_to_market_external_cash_flows_unadjusted"
    assert valued["current_nav_fen"] == 430_000
    assert valued["external_cash_flow_net_fen"] == 50_000
    assert valued["cash_flow_timing_quality"] == "exact_effective_time"
    assert "reference_return" not in valued
    assert valued["performance_claim"] is False
