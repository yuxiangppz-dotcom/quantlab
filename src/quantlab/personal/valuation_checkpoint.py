"""Immutable account valuation checkpoints bound to validated Daily evidence.

A checkpoint is account truth marked with same-session raw closes. It is not a
performance result and never authorizes trading. The content-addressed artifact
exists so later performance methods can consume exact, replayable valuation
evidence instead of recomputing mutable account/price state.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from quantlab.daily.integrity import load_validated_latest_snapshot
from quantlab.daily.service import DEFAULT_PRODUCT_ROOT, PROJECT_ROOT, SHANGHAI
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import DEFAULT_ACCOUNT_ROOT, atomic_json
from quantlab.personal.tracking import load_effective_account

_SCHEMA = "quantlab_account_valuation_checkpoint_v1"
_ACTIVE_SCHEMA = "quantlab_account_valuation_active_v1"


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _close_fen(value: object) -> int:
    from decimal import Decimal, InvalidOperation

    try:
        fen = Decimal(str(value)) * 100
    except InvalidOperation as exc:
        raise ValueError(f"raw close is not a decimal value: {value!r}") from exc
    if not fen.is_finite() or fen <= 0 or fen != fen.to_integral_value():
        raise ValueError(f"raw close cannot be represented as positive integer fen: {value}")
    return int(fen)


def _checkpoint_core(payload: dict) -> dict:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "checkpoint_fingerprint"}
    }


def validate_valuation_checkpoint(
    path: Path,
    *,
    expected_account_id: str | None = None,
) -> dict:
    """Validate one checkpoint from its own bytes and economic identities."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("valuation checkpoint is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
        raise ValueError("unsupported valuation checkpoint schema")
    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("valuation checkpoint account_id is invalid")
    if expected_account_id is not None and account_id != expected_account_id:
        raise ValueError("valuation checkpoint account binding mismatch")
    try:
        date.fromisoformat(payload["price_date"])
        account_as_of = datetime.fromisoformat(payload["account_as_of"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("valuation checkpoint date metadata is invalid") from exc
    if account_as_of.tzinfo is None or account_as_of.utcoffset() is None:
        raise ValueError("valuation checkpoint account_as_of must be timezone-aware")

    for field in ("account_fingerprint", "daily_content_fingerprint"):
        value = payload.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"valuation checkpoint {field} must be SHA-256")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("valuation checkpoint evidence must be an object")
    for field in ("account_state_sha256", "raw_price_set_sha256"):
        value = evidence.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"valuation checkpoint evidence.{field} must be SHA-256")
    partition_sha = evidence.get("daily_bar_partition_sha256")
    if partition_sha is not None and (
        not isinstance(partition_sha, str) or len(partition_sha) != 64
    ):
        raise ValueError("valuation checkpoint daily-bar partition hash is invalid")

    cash_fen = _nonnegative_int(payload.get("cash_fen"), "cash_fen")
    positions = payload.get("positions")
    if not isinstance(positions, list):
        raise ValueError("valuation checkpoint positions must be a list")
    ids = [item.get("instrument_id") for item in positions if isinstance(item, dict)]
    if len(ids) != len(positions) or ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ValueError("valuation checkpoint positions must be unique and sorted")
    market_value_sum = 0
    raw_price_evidence = []
    for item in positions:
        instrument = item.get("instrument_id")
        if not isinstance(instrument, str) or not instrument:
            raise ValueError("valuation checkpoint instrument_id is invalid")
        quantity = _positive_int(item.get("quantity"), f"{instrument} quantity")
        raw_close_fen = _positive_int(
            item.get("raw_close_fen"), f"{instrument} raw_close_fen"
        )
        market_value_fen = _nonnegative_int(
            item.get("market_value_fen"), f"{instrument} market_value_fen"
        )
        expected_value = quantity * raw_close_fen
        if market_value_fen != expected_value:
            raise ValueError(f"valuation checkpoint market value mismatch for {instrument}")
        market_value_sum += market_value_fen
        raw_price_evidence.append(
            {
                "instrument_id": instrument,
                "raw_close_fen": raw_close_fen,
            }
        )
    if evidence["raw_price_set_sha256"] != _canonical_hash(raw_price_evidence):
        raise ValueError("valuation checkpoint raw price evidence fingerprint mismatch")
    nav_fen = _nonnegative_int(payload.get("nav_fen"), "nav_fen")
    if nav_fen != cash_fen + market_value_sum:
        raise ValueError("valuation checkpoint NAV arithmetic mismatch")
    if payload.get("performance_claim") is not False:
        raise ValueError("valuation checkpoint cannot carry a performance claim")
    fingerprint = payload.get("checkpoint_fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError("valuation checkpoint fingerprint is invalid")
    if fingerprint != _canonical_hash(_checkpoint_core(payload)):
        raise ValueError("valuation checkpoint fingerprint mismatch")
    return payload


def _valuation_root(account_root: Path, account_id: str) -> Path:
    return account_root / account_id / "valuations"


def _activate(root: Path, checkpoint_path: Path, fingerprint: str) -> None:
    resolved_root = root.resolve()
    resolved_checkpoint = checkpoint_path.resolve()
    if not resolved_checkpoint.is_relative_to(resolved_root):
        raise ValueError("valuation checkpoint escapes valuation root")
    pointer = {
        "schema": _ACTIVE_SCHEMA,
        "checkpoint_path": resolved_checkpoint.relative_to(resolved_root).as_posix(),
        "checkpoint_fingerprint": fingerprint,
    }
    atomic_json(root / "ACTIVE.json", pointer)


def load_latest_valuation_checkpoint(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
) -> tuple[Path, dict] | None:
    """Load and validate the active immutable checkpoint for one account."""
    root = _valuation_root(account_root, account_id)
    active = root / "ACTIVE.json"
    if not active.exists():
        return None
    try:
        pointer = json.loads(active.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("valuation ACTIVE pointer is unreadable or invalid JSON") from exc
    if pointer.get("schema") != _ACTIVE_SCHEMA:
        raise ValueError("unsupported valuation ACTIVE pointer schema")
    relative = pointer.get("checkpoint_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("valuation ACTIVE pointer path is invalid")
    checkpoint_path = (root / relative).resolve()
    if not checkpoint_path.is_relative_to(root.resolve()):
        raise ValueError("valuation ACTIVE pointer escapes valuation root")
    payload = validate_valuation_checkpoint(
        checkpoint_path,
        expected_account_id=account_id,
    )
    if payload["checkpoint_fingerprint"] != pointer.get("checkpoint_fingerprint"):
        raise ValueError("valuation ACTIVE pointer fingerprint mismatch")
    expected_relative = (
        Path(payload["price_date"])
        / payload["checkpoint_fingerprint"]
        / "checkpoint.json"
    ).as_posix()
    if relative != expected_relative:
        raise ValueError("valuation ACTIVE pointer path does not match checkpoint identity")
    return checkpoint_path, payload


def materialize_valuation_checkpoint(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    product_root: Path = DEFAULT_PRODUCT_ROOT,
    storage: ParquetStorage | None = None,
    now: datetime | None = None,
) -> tuple[Path, dict, bool]:
    """Publish or reuse one immutable raw-close valuation checkpoint."""
    storage = storage or ParquetStorage(PROJECT_ROOT / "data" / "canonical")
    generated_at = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    account = load_effective_account(account_id, account_root=account_root, storage=storage)
    snapshot = load_validated_latest_snapshot(product_root)
    if snapshot is None:
        raise FileNotFoundError("no validated Daily snapshot available for account valuation")
    price_date = date.fromisoformat(snapshot.report["effective_as_of"])
    account_as_of = datetime.fromisoformat(account["as_of"])
    account_local_date = account_as_of.astimezone(SHANGHAI).date()
    if price_date < account_local_date:
        raise ValueError(
            "Daily price date predates the latest effective account fact; regenerate Daily first"
        )

    positions = sorted(account.get("positions", []), key=lambda item: item["instrument_id"])
    bars = (
        {item.instrument_id: item for item in storage.load_daily_bars_by_date(price_date)}
        if positions
        else {}
    )
    missing = sorted(
        item["instrument_id"] for item in positions if item["instrument_id"] not in bars
    )
    if missing:
        raise ValueError(
            "cannot create complete valuation checkpoint; missing same-session raw closes: "
            + ", ".join(missing[:10])
        )

    marked_positions = []
    raw_price_evidence = []
    for item in positions:
        instrument = item["instrument_id"]
        quantity = _positive_int(item["quantity"], f"{instrument} quantity")
        raw_close_fen = _close_fen(bars[instrument].close)
        raw_price_evidence.append(
            {"instrument_id": instrument, "raw_close_fen": raw_close_fen}
        )
        marked_positions.append(
            {
                "instrument_id": instrument,
                "quantity": quantity,
                "raw_close_fen": raw_close_fen,
                "market_value_fen": quantity * raw_close_fen,
            }
        )

    account_state = {
        "account_id": account_id,
        "account_fingerprint": account["account_fingerprint"],
        "account_as_of": account["as_of"],
        "cash_fen": account["cash_fen"],
        "positions": [
            {
                "instrument_id": item["instrument_id"],
                "quantity": item["quantity"],
                "sellable_quantity": item["sellable_quantity"],
            }
            for item in positions
        ],
    }
    partition_path = storage.daily_bars_path(price_date)
    partition_sha = _sha256_file(partition_path) if positions else None
    core = {
        "schema": _SCHEMA,
        "account_id": account_id,
        "account_fingerprint": account["account_fingerprint"],
        "account_as_of": account["as_of"],
        "price_date": price_date.isoformat(),
        "daily_content_fingerprint": snapshot.report["content_fingerprint"],
        "cash_fen": account["cash_fen"],
        "positions": marked_positions,
        "nav_fen": account["cash_fen"]
        + sum(item["market_value_fen"] for item in marked_positions),
        "price_basis": "raw_same_session_close",
        "evidence": {
            "account_state_sha256": _canonical_hash(account_state),
            "raw_price_set_sha256": _canonical_hash(raw_price_evidence),
            "daily_bar_partition_sha256": partition_sha,
        },
        "performance_claim": False,
        "broker_order_authority": False,
        "known_limitations": [
            "this is a valuation evidence checkpoint, not an investment return",
            "raw daily close is a mark, not executable price or fill evidence",
            "corporate-action cash/share postings remain outside v1 account truth",
        ],
    }
    fingerprint = _canonical_hash(core)
    payload = {
        **core,
        "generated_at": generated_at.isoformat(),
        "checkpoint_fingerprint": fingerprint,
    }

    # Re-read every mutable upstream identity before publication. A concurrent
    # account import/journal update, Daily activation, or price-partition rewrite
    # cannot silently publish a checkpoint from a mixed state.
    account_after = load_effective_account(account_id, account_root=account_root, storage=storage)
    snapshot_after = load_validated_latest_snapshot(product_root)
    if snapshot_after is None:
        raise ValueError("Daily snapshot disappeared during valuation materialization")
    if account_after["account_fingerprint"] != account["account_fingerprint"]:
        raise ValueError("account identity drifted during valuation materialization")
    if (
        snapshot_after.report["content_fingerprint"]
        != snapshot.report["content_fingerprint"]
    ):
        raise ValueError("Daily identity drifted during valuation materialization")
    if positions and _sha256_file(partition_path) != partition_sha:
        raise ValueError("raw daily-bar partition drifted during valuation materialization")

    root = _valuation_root(account_root, account_id)
    day_root = root / price_date.isoformat()
    final_dir = day_root / fingerprint
    final_path = final_dir / "checkpoint.json"
    if final_dir.exists():
        existing = validate_valuation_checkpoint(
            final_path,
            expected_account_id=account_id,
        )
        if existing["checkpoint_fingerprint"] != fingerprint:
            raise ValueError("existing valuation directory carries the wrong fingerprint")
        _activate(root, final_path, fingerprint)
        return final_path, existing, True

    day_root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(dir=day_root, prefix=".checkpoint-"))
    try:
        atomic_json(temp_dir / "checkpoint.json", payload)
        validate_valuation_checkpoint(
            temp_dir / "checkpoint.json",
            expected_account_id=account_id,
        )
        try:
            os.rename(temp_dir, final_dir)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            shutil.rmtree(temp_dir, ignore_errors=True)
            existing = validate_valuation_checkpoint(
                final_path,
                expected_account_id=account_id,
            )
            if existing["checkpoint_fingerprint"] != fingerprint:
                raise ValueError("concurrent valuation winner has wrong fingerprint")
            _activate(root, final_path, fingerprint)
            return final_path, existing, True
        published = validate_valuation_checkpoint(
            final_path,
            expected_account_id=account_id,
        )
        _activate(root, final_path, fingerprint)
        return final_path, published, False
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
