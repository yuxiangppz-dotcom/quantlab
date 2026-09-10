"""Preview, import, and replay user-reported fills through the execution ledger."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quantlab.daily.service import (
    DEFAULT_PRODUCT_ROOT,
    PROJECT_ROOT,
    SHANGHAI,
    load_latest_snapshot,
)
from quantlab.data.storage import ParquetStorage
from quantlab.execution import (
    AccountSnapshot,
    ExecutionLedger,
    ManualFillImported,
    PositionLot,
    Side,
    TradingCalendar,
    exchange_date,
)
from quantlab.personal.account import DEFAULT_ACCOUNT_ROOT, atomic_json, load_account

FILL_COLUMNS = (
    "account_id",
    "broker_trade_id",
    "trade_date",
    "reported_at",
    "instrument_id",
    "side",
    "quantity",
    "price_cny",
    "gross_notional_cny",
    "fee_cny",
)


def _fen(value: str, field: str, *, positive: bool = False) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is not a decimal amount") from exc
    if (
        not amount.is_finite()
        or amount < 0
        or (positive and amount <= 0)
        or amount.as_tuple().exponent < -2
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier} CNY with at most 2 decimals")
    return int(amount * 100)


def _price(value: str) -> Decimal:
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("price_cny is not a decimal") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("price_cny must be positive")
    return price


def _quantity(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("quantity must be a positive integer")
    return int(value)


def _calendar(storage: ParquetStorage) -> TradingCalendar:
    entries = storage.load_trading_calendar()
    if not entries:
        raise ValueError("trading calendar is unavailable")
    sessions = tuple(sorted({item.trade_date for item in entries if item.is_open}))
    payload = "\n".join(item.isoformat() for item in sessions).encode()
    return TradingCalendar(
        sessions=sessions,
        coverage_start=min(item.trade_date for item in entries),
        coverage_end=max(item.trade_date for item in entries),
        source_id="quantlab_canonical_union_calendar",
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _opening_snapshot(account: dict, calendar: TradingCalendar) -> AccountSnapshot:
    as_of = datetime.fromisoformat(account["as_of"])
    local_date = exchange_date(as_of)
    next_session = next((item for item in calendar.sessions if item > local_date), None)
    lots: list[PositionLot] = []
    for position in account["positions"]:
        instrument = position["instrument_id"]
        sellable = position["sellable_quantity"]
        locked = position["quantity"] - sellable
        if sellable:
            lots.append(
                PositionLot(
                    f"opening:sellable:{instrument}",
                    instrument,
                    sellable,
                    date(1900, 1, 1),
                    date(1900, 1, 2),
                )
            )
        if locked:
            if next_session is None:
                raise ValueError("calendar cannot derive T+1 for imported unavailable holdings")
            lots.append(
                PositionLot(
                    f"opening:unavailable:{instrument}",
                    instrument,
                    locked,
                    local_date,
                    next_session,
                )
            )
    return AccountSnapshot(account["account_id"], as_of, account["cash_fen"], tuple(lots))


def _event_payload(event: ManualFillImported) -> dict:
    return {
        "event_id": event.event_id,
        "fill_id": event.fill_id,
        "occurred_at": event.occurred_at.isoformat(),
        "account_id": event.account_id,
        "instrument_id": event.instrument_id,
        "side": event.side.value,
        "trade_date": event.trade_date.isoformat(),
        "quantity": event.quantity,
        "price": str(event.price),
        "gross_notional_fen": event.gross_notional_fen,
        "fee_fen": event.fee_fen,
        "buy_lot_sellable_from": (
            event.buy_lot_sellable_from.isoformat() if event.buy_lot_sellable_from else None
        ),
        "source_sha256": event.source_sha256,
        "source_row_sha256": event.source_row_sha256,
    }


def _event_from_payload(item: dict) -> ManualFillImported:
    return ManualFillImported(
        event_id=item["event_id"],
        fill_id=item["fill_id"],
        occurred_at=datetime.fromisoformat(item["occurred_at"]),
        account_id=item["account_id"],
        instrument_id=item["instrument_id"],
        side=Side(item["side"]),
        trade_date=date.fromisoformat(item["trade_date"]),
        quantity=item["quantity"],
        price=Decimal(item["price"]),
        gross_notional_fen=item["gross_notional_fen"],
        fee_fen=item["fee_fen"],
        buy_lot_sellable_from=(
            date.fromisoformat(item["buy_lot_sellable_from"])
            if item["buy_lot_sellable_from"]
            else None
        ),
        source_sha256=item["source_sha256"],
        source_row_sha256=item["source_row_sha256"],
    )


def _same_economics(left: ManualFillImported, right: ManualFillImported) -> bool:
    excluded = {"source_sha256", "source_row_sha256"}
    return {key: value for key, value in _event_payload(left).items() if key not in excluded} == {
        key: value for key, value in _event_payload(right).items() if key not in excluded
    }


def _journal_path(account: dict, account_root: Path) -> Path:
    return (
        account_root
        / account["account_id"]
        / "tracking"
        / account["account_fingerprint"]
        / "journal.json"
    )


def _load_journal(account: dict, account_root: Path) -> dict | None:
    path = _journal_path(account, account_root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("opening_account_fingerprint") != account["account_fingerprint"]:
        raise ValueError("manual journal opening-account fingerprint mismatch")
    expected = hashlib.sha256(
        json.dumps(payload["events"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if payload.get("events_fingerprint") != expected:
        raise ValueError("manual journal event fingerprint mismatch")
    signed = {key: value for key, value in payload.items() if key != "journal_fingerprint"}
    expected_journal = hashlib.sha256(
        json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if payload.get("journal_fingerprint") != expected_journal:
        raise ValueError("manual journal fingerprint mismatch")
    return payload


def _load_events(account: dict, account_root: Path) -> list[ManualFillImported]:
    payload = _load_journal(account, account_root)
    return [] if payload is None else [_event_from_payload(item) for item in payload["events"]]


def _close_fen(value: object) -> int:
    fen = Decimal(str(value)) * 100
    if fen <= 0 or fen != fen.to_integral_value():
        raise ValueError(f"daily close cannot be represented as positive integer fen: {value}")
    return int(fen)


def _opening_mark(
    account: dict,
    storage: ParquetStorage,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
) -> dict:
    snapshot = load_latest_snapshot(product_root)
    if snapshot is None:
        return {"status": "unavailable_no_daily_snapshot"}
    mark_date = date.fromisoformat(snapshot.report["effective_as_of"])
    if mark_date > exchange_date(datetime.fromisoformat(account["as_of"])):
        return {"status": "unavailable_daily_snapshot_after_account"}
    bars = {item.instrument_id: item for item in storage.load_daily_bars_by_date(mark_date)}
    missing = sorted(
        item["instrument_id"] for item in account["positions"] if item["instrument_id"] not in bars
    )
    if missing:
        return {"status": "unavailable_missing_opening_prices", "missing": missing}
    nav_fen = account["cash_fen"] + sum(
        item["quantity"] * _close_fen(bars[item["instrument_id"]].close)
        for item in account["positions"]
    )
    return {
        "status": "complete_reference_mark",
        "date": mark_date.isoformat(),
        "daily_content_fingerprint": snapshot.report["content_fingerprint"],
        "nav_fen": nav_fen,
        "basis": "opening_snapshot_valued_at_latest_prior_raw_close",
    }


def _parse_events(
    raw: bytes,
    account: dict,
    calendar: TradingCalendar,
    known_instruments: set[str],
) -> list[ManualFillImported]:
    source_sha = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if tuple(reader.fieldnames or ()) != FILL_COLUMNS:
        raise ValueError(f"fill CSV columns must exactly equal {FILL_COLUMNS}")
    events = []
    for index, row in enumerate(reader, start=1):
        if row["account_id"].strip() != account["account_id"]:
            raise ValueError(f"row {index} account_id does not match selected account")
        instrument = row["instrument_id"].strip()
        if instrument not in known_instruments:
            raise ValueError(f"row {index} instrument_id is unknown: {instrument}")
        trade_date = date.fromisoformat(row["trade_date"].strip())
        reported_at = datetime.fromisoformat(row["reported_at"].strip())
        if reported_at.tzinfo is None or reported_at.utcoffset() is None:
            raise ValueError(f"row {index} reported_at must include timezone")
        side_text = row["side"].strip().lower()
        if side_text not in {"buy", "sell"}:
            raise ValueError(f"row {index} side must be BUY or SELL")
        side = Side(side_text)
        quantity = _quantity(row["quantity"].strip())
        price = _price(row["price_cny"].strip())
        gross = _fen(row["gross_notional_cny"].strip(), "gross_notional_cny", positive=True)
        fee = _fen(row["fee_cny"].strip(), "fee_cny")
        broker_id = row["broker_trade_id"].strip()
        if not broker_id:
            raise ValueError(f"row {index} broker_trade_id is required")
        row_payload = {key: row[key].strip() for key in FILL_COLUMNS}
        row_sha = hashlib.sha256(
            json.dumps(row_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        fill_id = f"{trade_date.isoformat()}:{broker_id}"
        event = ManualFillImported(
            event_id=f"manual:{account['account_id']}:{fill_id}",
            fill_id=fill_id,
            occurred_at=reported_at.astimezone(SHANGHAI),
            account_id=account["account_id"],
            instrument_id=instrument,
            side=side,
            trade_date=trade_date,
            quantity=quantity,
            price=price,
            gross_notional_fen=gross,
            fee_fen=fee,
            buy_lot_sellable_from=(calendar.next_session(trade_date) if side is Side.BUY else None),
            source_sha256=source_sha,
            source_row_sha256=row_sha,
        )
        events.append(event)
    if not events:
        raise ValueError("fill CSV has no rows")
    return events


def _replay(
    account: dict,
    calendar: TradingCalendar,
    events: list[ManualFillImported],
) -> ExecutionLedger:
    ledger = ExecutionLedger(_opening_snapshot(account, calendar), calendar=calendar)
    ledger.append_manual_imports(sorted(events, key=lambda item: (item.occurred_at, item.event_id)))
    return ledger


def manual_tracking_fixture_smoke() -> bool:
    """Exercise the manual-fill ledger path entirely in memory.

    Acceptance uses this fixture rather than touching a user's account or
    journal.  It deliberately checks cash, position, and the T+1 boundary.
    """
    monday = date(2026, 9, 7)
    tuesday = date(2026, 9, 8)
    calendar = TradingCalendar(
        (monday, tuesday), monday, tuesday, "acceptance-fixture", "f" * 64
    )
    initial = AccountSnapshot(
        "acceptance_fixture",
        datetime(2026, 9, 7, 9, tzinfo=SHANGHAI),
        200_000,
    )
    event = ManualFillImported(
        event_id="manual:acceptance-fixture",
        fill_id="2026-09-07:acceptance-fixture",
        occurred_at=datetime(2026, 9, 7, 16, tzinfo=SHANGHAI),
        account_id="acceptance_fixture",
        instrument_id="000001.SZ",
        side=Side.BUY,
        trade_date=monday,
        quantity=100,
        price=Decimal("10.00"),
        gross_notional_fen=100_000,
        fee_fen=500,
        buy_lot_sellable_from=tuesday,
        source_sha256="a" * 64,
        source_row_sha256="b" * 64,
    )
    ledger = ExecutionLedger(initial, calendar=calendar)
    ledger.append_manual_imports([event])
    return (
        ledger.cash_fen == 99_500
        and ledger.position_quantity("000001.SZ") == 100
        and ledger.sellable_quantity("000001.SZ", monday) == 0
        and ledger.sellable_quantity("000001.SZ", tuesday) == 100
    )


def _summary(
    account: dict,
    ledger: ExecutionLedger,
    events: list[ManualFillImported],
    duplicate_count: int,
) -> dict:
    latest = max(
        (event.occurred_at for event in events), default=datetime.fromisoformat(account["as_of"])
    )
    valuation_date = exchange_date(latest)
    instruments = sorted({lot.instrument_id for lot in ledger.lots})
    positions = [
        {
            "instrument_id": instrument,
            "quantity": ledger.position_quantity(instrument),
            "sellable_quantity": ledger.sellable_quantity(instrument, valuation_date),
        }
        for instrument in instruments
        if ledger.position_quantity(instrument)
    ]
    state = {
        "opening_account_fingerprint": account["account_fingerprint"],
        "as_of": latest.isoformat(),
        "cash_fen": ledger.cash_fen,
        "positions": positions,
        "fill_ids": sorted(event.fill_id for event in events),
    }
    tracking_fingerprint = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "account_id": account["account_id"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "event_count": len(events),
        "duplicate_count": duplicate_count,
        "as_of": latest.isoformat(),
        "cash_fen": ledger.cash_fen,
        "total_fee_fen": sum(event.fee_fen for event in events),
        "latest_fill_trade_date": (
            max(event.trade_date for event in events).isoformat() if events else None
        ),
        "positions": positions,
        "tracking_fingerprint": tracking_fingerprint,
        "fills": [_event_payload(event) for event in events],
        "mode": account["account_mode"],
        "broker_submission": False,
    }


def preview_manual_fills(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Validate the complete combined journal without writing anything."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    account = load_account(account_id, account_root=account_root)
    if account["account_mode"] != "manual_tracking":
        raise ValueError("manual fills may only be imported into a manual_tracking account")
    calendar = _calendar(storage)
    raw = source.read_bytes() if isinstance(source, Path) else source
    incoming = _parse_events(
        raw, account, calendar, {item.instrument_id for item in storage.load_securities()}
    )
    existing = _load_events(account, account_root)
    combined = {event.fill_id: event for event in existing}
    duplicate_count = 0
    accepted = []
    for event in incoming:
        previous = combined.get(event.fill_id)
        if previous is not None:
            if not _same_economics(previous, event):
                raise ValueError(
                    f"broker trade id reused with different economics: {event.fill_id}"
                )
            duplicate_count += 1
            continue
        combined[event.fill_id] = event
        accepted.append(event)
    events = list(combined.values())
    ledger = _replay(account, calendar, events)
    return {
        **_summary(account, ledger, events, duplicate_count),
        "accepted_count": len(accepted),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "events": [
            _event_payload(event)
            for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id))
        ],
    }


def import_manual_fills(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
) -> tuple[Path, dict]:
    """Atomically commit only a fully replayable, deduplicated journal."""
    preview = preview_manual_fills(account_id, source, account_root=account_root, storage=storage)
    account = load_account(account_id, account_root=account_root)
    events = preview.pop("events")
    path = _journal_path(account, account_root)
    if preview["accepted_count"] == 0 and path.exists():
        return path, preview
    event_fingerprint = hashlib.sha256(
        json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing_journal = _load_journal(account, account_root)
    opening_mark = (
        existing_journal.get("opening_mark")
        if existing_journal
        else _opening_mark(
            account,
            storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical"),
            product_root,
        )
    )
    payload = {
        "schema": "quantlab_manual_fill_journal_v1",
        "account_id": account_id,
        "opening_account_fingerprint": account["account_fingerprint"],
        "events": events,
        "events_fingerprint": event_fingerprint,
        "opening_mark": opening_mark,
        "latest_import": {
            "source_sha256": preview["source_sha256"],
            "accepted_count": preview["accepted_count"],
            "duplicate_count": preview["duplicate_count"],
        },
    }
    payload["journal_fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    atomic_json(path, payload)
    return path, preview


def load_tracking_summary(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    account = load_account(account_id, account_root=account_root)
    calendar = _calendar(storage)
    events = _load_events(account, account_root)
    ledger = _replay(account, calendar, events)
    summary = _summary(account, ledger, events, 0)
    journal = _load_journal(account, account_root)
    summary["opening_mark"] = journal.get("opening_mark") if journal else None
    return summary


def load_effective_account(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Overlay committed manual fills on the immutable imported opening snapshot."""
    account = load_account(account_id, account_root=account_root)
    if _load_journal(account, account_root) is None:
        return account
    summary = load_tracking_summary(account_id, account_root=account_root, storage=storage)
    original_cost = {
        item["instrument_id"]: item["reference_cost_fen"] for item in account["positions"]
    }
    positions = [
        {
            **item,
            "reference_cost_fen": original_cost.get(item["instrument_id"]),
        }
        for item in summary["positions"]
    ]
    return {
        "schema": "quantlab_tracked_account_view_v1",
        "account_id": account_id,
        "account_mode": account["account_mode"],
        "as_of": summary["as_of"],
        "cash_fen": summary["cash_fen"],
        "open_orders_declaration": account["open_orders_declaration"],
        "positions": positions,
        "source": "opening_snapshot_plus_manual_fill_journal",
        "account_fingerprint": summary["tracking_fingerprint"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "latest_fill_trade_date": summary["latest_fill_trade_date"],
    }


def build_plan_fill_comparison(
    account_id: str, *, account_root: Path = DEFAULT_ACCOUNT_ROOT
) -> dict | None:
    """Compare actual imported fills with the newest reference plan trade date."""
    from quantlab.personal.plan import load_latest_plan

    plan_item = load_latest_plan(account_id, account_root=account_root)
    if plan_item is None:
        return None
    _, plan = plan_item
    account = load_account(account_id, account_root=account_root)
    events = _load_events(account, account_root)
    intended = date.fromisoformat(plan["intended_next_session"])
    actual: dict[tuple[str, str], int] = {}
    for event in events:
        if event.trade_date == intended:
            key = (event.instrument_id, event.side.value.upper())
            actual[key] = actual.get(key, 0) + event.quantity
    planned = {
        (row["instrument_id"], row["action"]): row["planned_shares"]
        for row in plan["rows"]
        if row["action"] in {"BUY", "SELL"}
    }
    rows = []
    for instrument, action in sorted(set(planned) | set(actual)):
        planned_quantity = planned.get((instrument, action), 0)
        actual_quantity = actual.get((instrument, action), 0)
        rows.append(
            {
                "instrument_id": instrument,
                "side": action,
                "planned_quantity": planned_quantity,
                "actual_quantity": actual_quantity,
                "difference_quantity": actual_quantity - planned_quantity,
                "status": ("MATCHED" if planned_quantity == actual_quantity else "DIFFERENT"),
            }
        )
    return {
        "plan_id": plan["plan_id"],
        "intended_trade_date": intended.isoformat(),
        "rows": rows,
    }


def build_tracking_valuation(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Mark the replayed account; calculate return only after prices cover all fills."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    account = load_account(account_id, account_root=account_root)
    journal = _load_journal(account, account_root)
    if journal is None:
        return {"status": "unavailable_no_imported_fills"}
    opening_mark = journal.get("opening_mark") or {"status": "unavailable_no_opening_mark"}
    snapshot = load_latest_snapshot(product_root)
    if snapshot is None:
        return {"status": "unavailable_no_daily_snapshot", "opening_mark": opening_mark}
    mark_date = date.fromisoformat(snapshot.report["effective_as_of"])
    events = [_event_from_payload(item) for item in journal["events"]]
    last_trade_date = max((event.trade_date for event in events), default=date.min)
    effective = load_effective_account(account_id, account_root=account_root, storage=storage)
    bars = {item.instrument_id: item for item in storage.load_daily_bars_by_date(mark_date)}
    missing = sorted(
        item["instrument_id"]
        for item in effective["positions"]
        if item["instrument_id"] not in bars
    )
    base = {
        "price_date": mark_date.isoformat(),
        "last_fill_trade_date": last_trade_date.isoformat() if events else None,
        "opening_mark": opening_mark,
        "missing_prices": missing,
    }
    if mark_date < last_trade_date:
        return {**base, "status": "unavailable_prices_before_latest_fill"}
    if missing:
        return {**base, "status": "unavailable_missing_current_prices"}
    current_nav = effective["cash_fen"] + sum(
        item["quantity"] * _close_fen(bars[item["instrument_id"]].close)
        for item in effective["positions"]
    )
    if opening_mark.get("status") != "complete_reference_mark":
        return {
            **base,
            "status": "unavailable_incomplete_opening_mark",
            "current_nav_fen": current_nav,
        }
    opening_nav = opening_mark["nav_fen"]
    return {
        **base,
        "status": "complete_reference_mark_to_market",
        "current_nav_fen": current_nav,
        "reference_return": current_nav / opening_nav - 1,
        "performance_claim": False,
        "limitations": [
            "opening snapshot is marked at the latest prior raw daily close",
            "cash deposits and withdrawals are not modeled",
            "corporate-action cash and share postings are not modeled",
        ],
    }
