from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from quantlab.data.models import DailyBar, Security, TradingCalendar
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import import_account_csv
from quantlab.personal.plan import build_reference_plan
from quantlab.personal.tracking import (
    build_tracking_valuation,
    import_manual_fills,
    load_effective_account,
    load_tracking_summary,
    preview_manual_fills,
)

ACCOUNT_HEADER = (
    "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
    "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
)
FILL_HEADER = (
    "account_id,broker_trade_id,trade_date,reported_at,instrument_id,side,"
    "quantity,price_cny,gross_notional_cny,fee_cny\n"
)


def _seed(tmp_path: Path) -> tuple[Path, ParquetStorage]:
    account_root = tmp_path / "accounts"
    import_account_csv(
        (
            ACCOUNT_HEADER + "mine,manual_tracking,2026-09-07T09:00:00+08:00,2000.00,"
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


def _fills() -> bytes:
    return (
        FILL_HEADER + "mine,B1,2026-09-07,2026-09-07T15:00:00+08:00,000001.SZ,BUY,"
        "100,10.00,1000.00,5.00\n" + "mine,S1,2026-09-07,2026-09-07T15:01:00+08:00,000001.SZ,SELL,"
        "100,12.00,1200.00,5.00\n"
    ).encode()


def test_preview_is_read_only_then_import_and_reimport_are_idempotent(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    preview = preview_manual_fills("mine", _fills(), account_root=account_root, storage=storage)
    assert preview["accepted_count"] == 2
    assert preview["cash_fen"] == 219_000
    assert preview["total_fee_fen"] == 1000
    assert preview["positions"] == [
        {"instrument_id": "000001.SZ", "quantity": 200, "sellable_quantity": 100}
    ]
    assert not list(account_root.glob("mine/tracking/*/journal.json"))

    path, committed = import_manual_fills(
        "mine", _fills(), account_root=account_root, storage=storage
    )
    before = path.read_bytes()
    assert committed["accepted_count"] == 2
    same_path, duplicate = import_manual_fills(
        "mine", _fills(), account_root=account_root, storage=storage
    )
    assert same_path == path
    assert duplicate["accepted_count"] == 0
    assert duplicate["duplicate_count"] == 2
    assert same_path.read_bytes() == before
    assert duplicate["cash_fen"] == committed["cash_fen"]
    effective = load_effective_account("mine", account_root=account_root, storage=storage)
    assert effective["cash_fen"] == 219_000
    assert effective["positions"][0]["quantity"] == 200
    assert effective["positions"][0]["sellable_quantity"] == 100


def test_invalid_batch_never_creates_journal(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    invalid = (
        FILL_HEADER + "mine,B1,2026-09-07,2026-09-07T15:00:00+08:00,000001.SZ,BUY,"
        "100,10.00,1000.00,5.00\n" + "mine,S1,2026-09-07,2026-09-07T15:01:00+08:00,000001.SZ,SELL,"
        "999,12.00,11988.00,5.00\n"
    ).encode()
    with pytest.raises(Exception, match="oversell"):
        import_manual_fills("mine", invalid, account_root=account_root, storage=storage)
    assert not list(account_root.glob("mine/tracking/*/journal.json"))


def test_same_broker_trade_id_with_changed_economics_is_rejected(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    first = FILL_HEADER + (
        "mine,B1,2026-09-07,2026-09-07T15:00:00+08:00,000001.SZ,BUY,100,10.00,1000.00,5.00\n"
    )
    import_manual_fills("mine", first.encode(), account_root=account_root, storage=storage)
    changed = first.replace("1000.00", "1100.00").replace("10.00", "11.00")
    with pytest.raises(ValueError, match="reused with different economics"):
        preview_manual_fills("mine", changed.encode(), account_root=account_root, storage=storage)


def test_journal_tampering_is_rejected(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    path, _ = import_manual_fills("mine", _fills(), account_root=account_root, storage=storage)
    payload = json.loads(path.read_text())
    payload["opening_mark"] = {"status": "complete_reference_mark", "nav_fen": 1}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="journal fingerprint mismatch"):
        load_tracking_summary("mine", account_root=account_root, storage=storage)


def test_reference_performance_waits_until_price_covers_latest_fill(tmp_path: Path) -> None:
    account_root, storage = _seed(tmp_path)
    opening_day = date(2026, 9, 4)
    current_day = date(2026, 9, 7)
    for day, close in ((opening_day, 8.0), (current_day, 11.0)):
        storage.save_daily_bars_by_date(
            [DailyBar("000001.SZ", day, close, close, close, close, close, 1, close)],
            day,
        )
    product_root = tmp_path / "products"
    opening_dir = product_root / opening_day.isoformat()
    opening_dir.mkdir(parents=True)
    (opening_dir / "report.json").write_text(
        '{"effective_as_of":"2026-09-04","content_fingerprint":"opening"}'
    )
    import_manual_fills(
        "mine",
        _fills(),
        account_root=account_root,
        product_root=product_root,
        storage=storage,
    )
    stale = build_tracking_valuation(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert stale["status"] == "unavailable_prices_before_latest_fill"

    current_dir = product_root / current_day.isoformat()
    current_dir.mkdir()
    (current_dir / "report.json").write_text(
        '{"effective_as_of":"2026-09-07","content_fingerprint":"current"}'
    )
    valued = build_tracking_valuation(
        "mine", account_root=account_root, product_root=product_root, storage=storage
    )
    assert valued["status"] == "complete_reference_mark_to_market"
    assert valued["current_nav_fen"] == 439_000
    assert valued["reference_return"] == pytest.approx(439_000 / 360_000 - 1)
    assert valued["performance_claim"] is False


def test_reference_plan_rejects_daily_snapshot_stale_relative_to_fills(
    tmp_path: Path,
) -> None:
    account_root, storage = _seed(tmp_path)
    import_manual_fills("mine", _fills(), account_root=account_root, storage=storage)
    product_root = tmp_path / "daily"
    out = product_root / "2026-09-04"
    out.mkdir(parents=True)
    ranking = b"instrument_id,selected,target_weight,risk_context\n"
    target = b"instrument_id,target_weight\n"
    (out / "ranking.csv").write_bytes(ranking)
    (out / "target_portfolio.csv").write_bytes(target)
    (out / "report.html").write_text("ok")
    report_core = {
        "effective_as_of": "2026-09-04",
        "next_known_open_session": "2026-09-07",
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "report": report_core,
                "ranking_sha256": hashlib.sha256(ranking).hexdigest(),
                "target_sha256": hashlib.sha256(target).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()
    (out / "report.json").write_text(
        json.dumps({**report_core, "content_fingerprint": fingerprint}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stale relative to imported fills"):
        build_reference_plan(
            "mine", account_root=account_root, product_root=product_root, storage=storage
        )
