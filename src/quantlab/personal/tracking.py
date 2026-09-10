"""Preview, import, and replay user-reported account facts.

Broker fills remain execution-ledger facts. External deposits and withdrawals are
account-truth facts layered around that ledger; they never become orders, fills,
or broker authority.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
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
from quantlab.personal.cash_flow import CashFlowDirection, ExternalCashFlow

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
CASH_FLOW_COLUMNS = (
    "account_id",
    "external_flow_id",
    "reported_at",
    "direction",
    "amount_cny",
)
_JOURNAL_V1 = "quantlab_manual_fill_journal_v1"
_JOURNAL_V2 = "quantlab_manual_tracking_journal_v2"


@dataclass(frozen=True)
class _TrackingReplay:
    ledger: ExecutionLedger
    fills: tuple[ManualFillImported, ...]
    cash_flows: tuple[ExternalCashFlow, ...]
    latest_occurred_at: datetime


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


def _fill_payload(event: ManualFillImported, *, tagged: bool = False) -> dict:
    payload = {
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
    return {"event_type": "manual_fill", **payload} if tagged else payload


def _fill_from_payload(item: dict) -> ManualFillImported:
    if item.get("event_type", "manual_fill") != "manual_fill":
        raise ValueError("journal event is not a manual fill")
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


def _cash_flow_payload(event: ExternalCashFlow) -> dict:
    return {
        "event_type": "external_cash_flow",
        "event_id": event.event_id,
        "flow_id": event.flow_id,
        "occurred_at": event.occurred_at.isoformat(),
        "account_id": event.account_id,
        "direction": event.direction.value,
        "amount_fen": event.amount_fen,
        "source_sha256": event.source_sha256,
        "source_row_sha256": event.source_row_sha256,
    }


def _cash_flow_from_payload(item: dict) -> ExternalCashFlow:
    if item.get("event_type") != "external_cash_flow":
        raise ValueError("journal event is not an external cash flow")
    return ExternalCashFlow(
        event_id=item["event_id"],
        flow_id=item["flow_id"],
        occurred_at=datetime.fromisoformat(item["occurred_at"]),
        account_id=item["account_id"],
        direction=CashFlowDirection(item["direction"]),
        amount_fen=item["amount_fen"],
        source_sha256=item["source_sha256"],
        source_row_sha256=item["source_row_sha256"],
    )


def _same_fill_economics(left: ManualFillImported, right: ManualFillImported) -> bool:
    excluded = {"source_sha256", "source_row_sha256"}
    return {key: value for key, value in _fill_payload(left).items() if key not in excluded} == {
        key: value for key, value in _fill_payload(right).items() if key not in excluded
    }


def _same_cash_flow_economics(left: ExternalCashFlow, right: ExternalCashFlow) -> bool:
    excluded = {"source_sha256", "source_row_sha256"}
    return {
        key: value for key, value in _cash_flow_payload(left).items() if key not in excluded
    } == {
        key: value for key, value in _cash_flow_payload(right).items() if key not in excluded
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
    if payload.get("schema") not in {_JOURNAL_V1, _JOURNAL_V2}:
        raise ValueError("unsupported manual tracking journal schema")
    if payload.get("account_id") != account["account_id"]:
        raise ValueError("manual journal account binding mismatch")
    if payload.get("opening_account_fingerprint") != account["account_fingerprint"]:
        raise ValueError("manual journal opening-account fingerprint mismatch")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("manual journal events must be a list")
    expected = hashlib.sha256(
        json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
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


def _decode_journal(payload: dict | None) -> tuple[list[ManualFillImported], list[ExternalCashFlow]]:
    if payload is None:
        return [], []
    fills: list[ManualFillImported] = []
    flows: list[ExternalCashFlow] = []
    if payload["schema"] == _JOURNAL_V1:
        fills = [_fill_from_payload(item) for item in payload["events"]]
    else:
        for item in payload["events"]:
            event_type = item.get("event_type")
            if event_type == "manual_fill":
                fills.append(_fill_from_payload(item))
            elif event_type == "external_cash_flow":
                flows.append(_cash_flow_from_payload(item))
            else:
                raise ValueError(f"unknown manual tracking event_type: {event_type!r}")
    identities = [event.event_id for event in (*fills, *flows)]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate event_id in manual tracking journal")
    ordered = sorted((*fills, *flows), key=lambda event: (event.occurred_at, event.event_id))
    if [event.event_id for event in ordered] != [
        item.get("event_id") for item in payload["events"]
    ]:
        raise ValueError("manual tracking journal events are not deterministically ordered")
    return fills, flows


def _load_events(
    account: dict, account_root: Path
) -> tuple[list[ManualFillImported], list[ExternalCashFlow]]:
    return _decode_journal(_load_journal(account, account_root))


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


def _parse_fill_events(
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
        events.append(
            ManualFillImported(
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
                buy_lot_sellable_from=(
                    calendar.next_session(trade_date) if side is Side.BUY else None
                ),
                source_sha256=source_sha,
                source_row_sha256=row_sha,
            )
        )
    if not events:
        raise ValueError("fill CSV has no rows")
    return events


def _parse_cash_flow_events(raw: bytes, account: dict) -> list[ExternalCashFlow]:
    source_sha = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if tuple(reader.fieldnames or ()) != CASH_FLOW_COLUMNS:
        raise ValueError(f"cash-flow CSV columns must exactly equal {CASH_FLOW_COLUMNS}")
    events = []
    for index, row in enumerate(reader, start=1):
        if row["account_id"].strip() != account["account_id"]:
            raise ValueError(f"row {index} account_id does not match selected account")
        flow_id = row["external_flow_id"].strip()
        if not flow_id:
            raise ValueError(f"row {index} external_flow_id is required")
        reported_at = datetime.fromisoformat(row["reported_at"].strip())
        if reported_at.tzinfo is None or reported_at.utcoffset() is None:
            raise ValueError(f"row {index} reported_at must include timezone")
        direction_text = row["direction"].strip().lower()
        try:
            direction = CashFlowDirection(direction_text)
        except ValueError as exc:
            raise ValueError(f"row {index} direction must be DEPOSIT or WITHDRAWAL") from exc
        amount_fen = _fen(row["amount_cny"].strip(), "amount_cny", positive=True)
        row_payload = {key: row[key].strip() for key in CASH_FLOW_COLUMNS}
        row_sha = hashlib.sha256(
            json.dumps(row_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        events.append(
            ExternalCashFlow(
                event_id=f"cashflow:{account['account_id']}:{flow_id}",
                flow_id=flow_id,
                occurred_at=reported_at.astimezone(SHANGHAI),
                account_id=account["account_id"],
                direction=direction,
                amount_fen=amount_fen,
                source_sha256=source_sha,
                source_row_sha256=row_sha,
            )
        )
    if not events:
        raise ValueError("cash-flow CSV has no rows")
    return events


def _replay_tracking(
    account: dict,
    calendar: TradingCalendar,
    fills: list[ManualFillImported],
    flows: list[ExternalCashFlow],
) -> _TrackingReplay:
    opening_at = datetime.fromisoformat(account["as_of"])
    ledger = ExecutionLedger(_opening_snapshot(account, calendar), calendar=calendar)
    combined = sorted((*fills, *flows), key=lambda event: (event.occurred_at, event.event_id))
    ids = [event.event_id for event in combined]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate manual tracking event_id")
    for event in combined:
        if event.occurred_at < opening_at:
            raise ValueError("manual tracking event precedes opening account snapshot")
        if isinstance(event, ManualFillImported):
            ledger.append_manual_imports([event])
            continue
        cash = ledger.cash_fen + event.signed_amount_fen
        if cash < 0:
            raise ValueError(
                f"external withdrawal {event.flow_id} exceeds settled cash at its event time"
            )
        # Cash flows are account truth, not execution events. Rebase the
        # execution ledger on the exact post-flow cash and unchanged lots so
        # subsequent fills still use the normal fill/T+1 accounting path.
        ledger = ExecutionLedger(
            AccountSnapshot(account["account_id"], event.occurred_at, cash, ledger.lots),
            calendar=calendar,
        )
    latest = combined[-1].occurred_at if combined else opening_at
    return _TrackingReplay(ledger, tuple(fills), tuple(flows), latest)


def _summary(account: dict, replay: _TrackingReplay, duplicate_count: int) -> dict:
    valuation_date = exchange_date(replay.latest_occurred_at)
    instruments = sorted({lot.instrument_id for lot in replay.ledger.lots})
    positions = [
        {
            "instrument_id": instrument,
            "quantity": replay.ledger.position_quantity(instrument),
            "sellable_quantity": replay.ledger.sellable_quantity(instrument, valuation_date),
        }
        for instrument in instruments
        if replay.ledger.position_quantity(instrument)
    ]
    state = {
        "opening_account_fingerprint": account["account_fingerprint"],
        "as_of": replay.latest_occurred_at.isoformat(),
        "cash_fen": replay.ledger.cash_fen,
        "positions": positions,
        "fill_ids": sorted(event.fill_id for event in replay.fills),
    }
    if replay.cash_flows:
        state["cash_flow_ids"] = sorted(event.flow_id for event in replay.cash_flows)
    tracking_fingerprint = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    net_external = sum(event.signed_amount_fen for event in replay.cash_flows)
    return {
        "account_id": account["account_id"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "event_count": len(replay.fills) + len(replay.cash_flows),
        "fill_event_count": len(replay.fills),
        "cash_flow_event_count": len(replay.cash_flows),
        "duplicate_count": duplicate_count,
        "as_of": replay.latest_occurred_at.isoformat(),
        "cash_fen": replay.ledger.cash_fen,
        "total_fee_fen": sum(event.fee_fen for event in replay.fills),
        "external_cash_flow_net_fen": net_external,
        "latest_fill_trade_date": (
            max(event.trade_date for event in replay.fills).isoformat() if replay.fills else None
        ),
        "latest_external_cash_flow_at": (
            max(event.occurred_at for event in replay.cash_flows).isoformat()
            if replay.cash_flows
            else None
        ),
        "positions": positions,
        "tracking_fingerprint": tracking_fingerprint,
        "fills": [_fill_payload(event) for event in replay.fills],
        "cash_flows": [_cash_flow_payload(event) for event in replay.cash_flows],
        "mode": account["account_mode"],
        "broker_submission": False,
    }


def _journal_events(
    fills: list[ManualFillImported], flows: list[ExternalCashFlow]
) -> tuple[str, list[dict]]:
    if not flows:
        ordered = sorted(fills, key=lambda event: (event.occurred_at, event.event_id))
        return _JOURNAL_V1, [_fill_payload(event) for event in ordered]
    combined = sorted((*fills, *flows), key=lambda event: (event.occurred_at, event.event_id))
    payloads = [
        _fill_payload(event, tagged=True)
        if isinstance(event, ManualFillImported)
        else _cash_flow_payload(event)
        for event in combined
    ]
    return _JOURNAL_V2, payloads


def _write_journal(
    account: dict,
    account_root: Path,
    *,
    fills: list[ManualFillImported],
    flows: list[ExternalCashFlow],
    opening_mark: dict,
    import_kind: str,
    source_sha256: str,
    accepted_count: int,
    duplicate_count: int,
) -> Path:
    schema, events = _journal_events(fills, flows)
    payload = {
        "schema": schema,
        "account_id": account["account_id"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "events": events,
        "events_fingerprint": hashlib.sha256(
            json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "opening_mark": opening_mark,
        "latest_import": {
            "kind": import_kind,
            "source_sha256": source_sha256,
            "accepted_count": accepted_count,
            "duplicate_count": duplicate_count,
        },
    }
    payload["journal_fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = _journal_path(account, account_root)
    atomic_json(path, payload)
    return path


def _preview_fills_internal(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path,
    storage: ParquetStorage,
) -> tuple[dict, list[ManualFillImported], list[ExternalCashFlow]]:
    account = load_account(account_id, account_root=account_root)
    if account["account_mode"] != "manual_tracking":
        raise ValueError("manual fills may only be imported into a manual_tracking account")
    calendar = _calendar(storage)
    raw = source.read_bytes() if isinstance(source, Path) else source
    incoming = _parse_fill_events(
        raw, account, calendar, {item.instrument_id for item in storage.load_securities()}
    )
    existing_fills, flows = _load_events(account, account_root)
    combined = {event.fill_id: event for event in existing_fills}
    duplicate_count = 0
    accepted = []
    for event in incoming:
        previous = combined.get(event.fill_id)
        if previous is not None:
            if not _same_fill_economics(previous, event):
                raise ValueError(
                    f"broker trade id reused with different economics: {event.fill_id}"
                )
            duplicate_count += 1
            continue
        combined[event.fill_id] = event
        accepted.append(event)
    fills = list(combined.values())
    replay = _replay_tracking(account, calendar, fills, flows)
    preview = {
        **_summary(account, replay, duplicate_count),
        "accepted_count": len(accepted),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return preview, fills, flows


def _preview_cash_flows_internal(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path,
    storage: ParquetStorage,
) -> tuple[dict, list[ManualFillImported], list[ExternalCashFlow]]:
    account = load_account(account_id, account_root=account_root)
    if account["account_mode"] != "manual_tracking":
        raise ValueError("cash flows may only be imported into a manual_tracking account")
    calendar = _calendar(storage)
    raw = source.read_bytes() if isinstance(source, Path) else source
    incoming = _parse_cash_flow_events(raw, account)
    fills, existing_flows = _load_events(account, account_root)
    combined = {event.flow_id: event for event in existing_flows}
    duplicate_count = 0
    accepted = []
    for event in incoming:
        previous = combined.get(event.flow_id)
        if previous is not None:
            if not _same_cash_flow_economics(previous, event):
                raise ValueError(
                    f"external flow id reused with different economics: {event.flow_id}"
                )
            duplicate_count += 1
            continue
        combined[event.flow_id] = event
        accepted.append(event)
    flows = list(combined.values())
    replay = _replay_tracking(account, calendar, fills, flows)
    preview = {
        **_summary(account, replay, duplicate_count),
        "accepted_count": len(accepted),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }
    return preview, fills, flows


def preview_manual_fills(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Validate fills against the complete account-fact stream without writing."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    preview, _, _ = _preview_fills_internal(
        account_id, source, account_root=account_root, storage=storage
    )
    return preview


def preview_manual_cash_flows(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Validate deposits/withdrawals without writing account truth."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    preview, _, _ = _preview_cash_flows_internal(
        account_id, source, account_root=account_root, storage=storage
    )
    return preview


def import_manual_fills(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
) -> tuple[Path, dict]:
    """Atomically commit fills while preserving any external cash-flow facts."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    preview, fills, flows = _preview_fills_internal(
        account_id, source, account_root=account_root, storage=storage
    )
    account = load_account(account_id, account_root=account_root)
    path = _journal_path(account, account_root)
    if preview["accepted_count"] == 0 and path.exists():
        return path, preview
    existing_journal = _load_journal(account, account_root)
    opening_mark = (
        existing_journal.get("opening_mark")
        if existing_journal
        else _opening_mark(account, storage, product_root)
    )
    path = _write_journal(
        account,
        account_root,
        fills=fills,
        flows=flows,
        opening_mark=opening_mark,
        import_kind="manual_fill",
        source_sha256=preview["source_sha256"],
        accepted_count=preview["accepted_count"],
        duplicate_count=preview["duplicate_count"],
    )
    return path, preview


def import_manual_cash_flows(
    account_id: str,
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
) -> tuple[Path, dict]:
    """Atomically commit validated external cash-flow facts."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    preview, fills, flows = _preview_cash_flows_internal(
        account_id, source, account_root=account_root, storage=storage
    )
    account = load_account(account_id, account_root=account_root)
    path = _journal_path(account, account_root)
    if preview["accepted_count"] == 0 and path.exists():
        return path, preview
    existing_journal = _load_journal(account, account_root)
    opening_mark = (
        existing_journal.get("opening_mark")
        if existing_journal
        else _opening_mark(account, storage, product_root)
    )
    path = _write_journal(
        account,
        account_root,
        fills=fills,
        flows=flows,
        opening_mark=opening_mark,
        import_kind="external_cash_flow",
        source_sha256=preview["source_sha256"],
        accepted_count=preview["accepted_count"],
        duplicate_count=preview["duplicate_count"],
    )
    return path, preview


def manual_tracking_fixture_smoke() -> bool:
    """Exercise the manual-fill ledger path entirely in memory."""
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


def load_tracking_summary(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    account = load_account(account_id, account_root=account_root)
    calendar = _calendar(storage)
    fills, flows = _load_events(account, account_root)
    replay = _replay_tracking(account, calendar, fills, flows)
    summary = _summary(account, replay, 0)
    journal = _load_journal(account, account_root)
    summary["opening_mark"] = journal.get("opening_mark") if journal else None
    return summary


def load_effective_account(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Overlay committed manual account facts on the immutable opening snapshot."""
    account = load_account(account_id, account_root=account_root)
    if _load_journal(account, account_root) is None:
        return account
    summary = load_tracking_summary(account_id, account_root=account_root, storage=storage)
    original_cost = {
        item["instrument_id"]: item["reference_cost_fen"] for item in account["positions"]
    }
    positions = [
        {**item, "reference_cost_fen": original_cost.get(item["instrument_id"])}
        for item in summary["positions"]
    ]
    has_flows = summary["cash_flow_event_count"] > 0
    return {
        "schema": "quantlab_tracked_account_view_v1",
        "account_id": account_id,
        "account_mode": account["account_mode"],
        "as_of": summary["as_of"],
        "cash_fen": summary["cash_fen"],
        "open_orders_declaration": account["open_orders_declaration"],
        "positions": positions,
        "source": (
            "opening_snapshot_plus_manual_tracking_journal"
            if has_flows
            else "opening_snapshot_plus_manual_fill_journal"
        ),
        "account_fingerprint": summary["tracking_fingerprint"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "latest_fill_trade_date": summary["latest_fill_trade_date"],
        "latest_external_cash_flow_at": summary["latest_external_cash_flow_at"],
        "external_cash_flow_net_fen": summary["external_cash_flow_net_fen"],
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
    fills, _ = _load_events(account, account_root)
    intended = date.fromisoformat(plan["intended_next_session"])
    actual: dict[tuple[str, str], int] = {}
    for event in fills:
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
                "status": "MATCHED" if planned_quantity == actual_quantity else "DIFFERENT",
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
    """Mark the replayed account without misclassifying external cash as P&L."""
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
    fills, flows = _decode_journal(journal)
    last_trade_date = max((event.trade_date for event in fills), default=date.min)
    last_flow_date = max((exchange_date(event.occurred_at) for event in flows), default=date.min)
    effective = load_effective_account(account_id, account_root=account_root, storage=storage)
    bars = {item.instrument_id: item for item in storage.load_daily_bars_by_date(mark_date)}
    missing = sorted(
        item["instrument_id"] for item in effective["positions"] if item["instrument_id"] not in bars
    )
    base = {
        "price_date": mark_date.isoformat(),
        "last_fill_trade_date": last_trade_date.isoformat() if fills else None,
        "latest_external_cash_flow_at": (
            max(event.occurred_at for event in flows).isoformat() if flows else None
        ),
        "opening_mark": opening_mark,
        "missing_prices": missing,
    }
    if mark_date < last_trade_date:
        status = (
            "unavailable_prices_before_latest_account_event"
            if flows
            else "unavailable_prices_before_latest_fill"
        )
        return {**base, "status": status}
    if flows and mark_date < last_flow_date:
        return {**base, "status": "unavailable_prices_before_latest_account_event"}
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
    if flows:
        return {
            **base,
            "status": "complete_mark_to_market_external_cash_flows_unadjusted",
            "current_nav_fen": current_nav,
            "external_cash_flow_net_fen": sum(event.signed_amount_fen for event in flows),
            "performance_claim": False,
            "limitations": [
                "opening snapshot is marked at the latest prior raw daily close",
                "external cash flows are account truth but cash-flow-aware performance is not implemented",
                "corporate-action cash and share postings are not modeled",
            ],
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
            "corporate-action cash and share postings are not modeled",
        ],
    }
