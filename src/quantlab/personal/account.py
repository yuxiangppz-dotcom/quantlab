"""Validated, local-only account snapshots."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quantlab.daily.service import PROJECT_ROOT

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_ACCOUNT_ROOT = PROJECT_ROOT / "data" / "accounts"
REQUIRED_COLUMNS = (
    "account_id",
    "account_mode",
    "as_of",
    "cash_cny",
    "instrument_id",
    "quantity",
    "sellable_quantity",
    "reference_cost_cny",
    "open_orders_declaration",
)


def _fen(value: str, field: str) -> int:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise ValueError(f"{field} must be a non-negative CNY amount with at most 2 decimals")
    return int(amount * 100)


def _int(value: str, field: str) -> int:
    if not value.strip() or not value.strip().isdigit():
        raise ValueError(f"{field} must be a non-negative integer")
    return int(value)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def atomic_text(path: Path, content: str) -> None:
    """Publish a UTF-8 text artifact without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def import_account_csv(
    source: Path | bytes,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
) -> Path:
    """Validate a complete account snapshot CSV and atomically store it."""
    raw = source.read_bytes() if isinstance(source, Path) else source
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != REQUIRED_COLUMNS:
        raise ValueError(f"account CSV columns must exactly equal {REQUIRED_COLUMNS}")
    rows = list(reader)
    if not rows:
        raise ValueError("account CSV has no rows")
    account_ids = {row["account_id"].strip() for row in rows}
    modes = {row["account_mode"].strip() for row in rows}
    timestamps = {row["as_of"].strip() for row in rows}
    cash_values = {row["cash_cny"].strip() for row in rows}
    declarations = {row["open_orders_declaration"].strip() for row in rows}
    if any(
        len(values) != 1 for values in (account_ids, modes, timestamps, cash_values, declarations)
    ):
        raise ValueError("account-level fields must be identical on every CSV row")
    account_id = account_ids.pop()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", account_id):
        raise ValueError("account_id must be 1-64 safe ASCII identifier characters")
    mode = modes.pop()
    if mode not in {"manual_tracking", "demo_simulation"}:
        raise ValueError("account_mode must be manual_tracking or demo_simulation")
    as_of = datetime.fromisoformat(timestamps.pop())
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("account as_of must include an explicit timezone offset")
    declaration = declarations.pop()
    if not declaration:
        raise ValueError("open_orders_declaration is required")

    lots = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        instrument = row["instrument_id"].strip()
        quantity = _int(row["quantity"], f"row {index} quantity")
        sellable = _int(row["sellable_quantity"], f"row {index} sellable_quantity")
        if not instrument and quantity == 0 and sellable == 0:
            continue
        if not instrument:
            raise ValueError(f"row {index} has quantity without instrument_id")
        if instrument in seen:
            raise ValueError(f"duplicate instrument_id: {instrument}")
        seen.add(instrument)
        if quantity <= 0 or sellable > quantity:
            raise ValueError(f"invalid quantity/sellable_quantity for {instrument}")
        cost = row["reference_cost_cny"].strip()
        lots.append(
            {
                "instrument_id": instrument,
                "quantity": quantity,
                "sellable_quantity": sellable,
                "reference_cost_fen": _fen(cost, "reference_cost_cny") if cost else None,
            }
        )
    payload = {
        "schema": "quantlab_account_snapshot_v1",
        "account_id": account_id,
        "account_mode": mode,
        "as_of": as_of.astimezone(SHANGHAI).isoformat(),
        "cash_fen": _fen(cash_values.pop(), "cash_cny"),
        "open_orders_declaration": declaration,
        "positions": sorted(lots, key=lambda item: item["instrument_id"]),
        "source": "user_imported_csv" if mode == "manual_tracking" else "demo_generated",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }
    economic = {key: value for key, value in payload.items() if key != "source_sha256"}
    payload["account_fingerprint"] = hashlib.sha256(
        json.dumps(economic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = account_root / account_id / "snapshot.json"
    if path.exists():
        previous = load_account(account_id, account_root=account_root)
        if previous["account_mode"] != mode:
            raise ValueError("account_mode cannot change for an existing account_id")
        _archive_snapshot(path.parent, previous)
    _archive_snapshot(path.parent, payload)
    atomic_json(path, payload)
    return path


def _archive_snapshot(directory: Path, payload: dict) -> None:
    """Keep each opening basis available without replacing an existing archive."""
    fingerprint = payload["account_fingerprint"]
    archive = directory / "snapshots" / f"{fingerprint}.json"
    if archive.exists():
        saved = json.loads(archive.read_text(encoding="utf-8"))
        # Source CSV formatting can differ for identical economic facts.
        if not isinstance(saved, dict) or {
            key: value for key, value in saved.items() if key != "source_sha256"
        } != {key: value for key, value in payload.items() if key != "source_sha256"}:
            raise ValueError("existing account snapshot archive conflicts with its fingerprint")
        return
    atomic_json(archive, payload)


def create_demo_account(
    account_id: str = "demo_200k",
    *,
    cash_cny: str = "200000.00",
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    now: datetime | None = None,
) -> Path:
    local_now = now or datetime.now(SHANGHAI)
    content = "\n".join(
        [
            ",".join(REQUIRED_COLUMNS),
            f"{account_id},demo_simulation,{local_now.isoformat()},{cash_cny},,0,0,,none_declared",
            "",
        ]
    ).encode()
    return import_account_csv(content, account_root=account_root)


def load_account(account_id: str, *, account_root: Path = DEFAULT_ACCOUNT_ROOT) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", account_id):
        raise ValueError("account_id must be 1-64 safe ASCII identifier characters")
    path = account_root / account_id / "snapshot.json"
    if not path.exists():
        raise FileNotFoundError(f"account snapshot not found: {account_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _validate_account_payload(payload, account_id)


def _validate_account_payload(payload: dict, account_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("account snapshot must be an object")
    if payload.get("schema") != "quantlab_account_snapshot_v1":
        raise ValueError("unsupported account snapshot schema")
    if payload.get("account_id") != account_id:
        raise ValueError("stored account binding does not match its path")
    economic = {
        key: value
        for key, value in payload.items()
        if key not in {"source_sha256", "account_fingerprint"}
    }
    expected = hashlib.sha256(
        json.dumps(economic, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if payload.get("account_fingerprint") != expected:
        raise ValueError("account snapshot fingerprint mismatch")
    return payload


def load_account_basis(
    account_id: str, fingerprint: str | None = None, *, account_root: Path = DEFAULT_ACCOUNT_ROOT
) -> dict:
    """Read an explicitly selected opening basis without changing the active account."""
    if fingerprint is None:
        return load_account(account_id, account_root=account_root)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", account_id):
        raise ValueError("unsafe account_id")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("opening fingerprint must be lowercase SHA-256")
    path = account_root / account_id / "snapshots" / f"{fingerprint}.json"
    if path.exists():
        payload = _validate_account_payload(
            json.loads(path.read_text(encoding="utf-8")), account_id
        )
    else:
        # Accounts imported before archival support may still be the active basis.
        payload = load_account(account_id, account_root=account_root)
    if payload["account_fingerprint"] != fingerprint:
        raise ValueError("requested opening fingerprint does not match the saved basis")
    return payload


def list_accounts(account_root: Path = DEFAULT_ACCOUNT_ROOT) -> list[str]:
    if not account_root.exists():
        return []
    return sorted(
        path.parent.name for path in account_root.glob("*/snapshot.json") if path.is_file()
    )
