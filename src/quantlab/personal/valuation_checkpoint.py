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
from quantlab.personal.cash_flow import CashFlowTimingQuality
from quantlab.personal.tracking import load_effective_account

_SCHEMA_V1 = "quantlab_account_valuation_checkpoint_v1"
_SCHEMA_V2 = "quantlab_account_valuation_checkpoint_v2"
_ACTIVE_SCHEMA = "quantlab_account_valuation_active_v1"
_NO_CASH_FLOW_TIMING = "not_applicable_no_external_cash_flows"


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


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


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


def _parse_aware(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"valuation checkpoint {field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"valuation checkpoint {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"valuation checkpoint {field} must be timezone-aware")
    return parsed


def _validate_timing_metadata(payload: dict, schema: str) -> datetime:
    account_as_of = _parse_aware(payload.get("account_as_of"), "account_as_of")
    if schema == _SCHEMA_V1:
        return account_as_of

    latest_economic = _parse_aware(
        payload.get("latest_economic_account_fact_at"),
        "latest_economic_account_fact_at",
    )
    latest_reported = _parse_aware(payload.get("latest_reported_at"), "latest_reported_at")
    if latest_reported < latest_economic:
        # A report can precede a later independent fill, so only require the
        # aggregate reported watermark not to precede its own account as-of
        # when there are no later economic facts. The per-flow invariant is
        # enforced in ExternalCashFlow. This field is informational provenance.
        pass
    if account_as_of != latest_economic:
        raise ValueError("valuation checkpoint account_as_of must equal latest economic fact")

    quality = payload.get("cash_flow_timing_quality")
    eligible = payload.get("cash_flow_timing_performance_eligible")
    status = payload.get("performance_input_status")
    allowed_quality = {
        _NO_CASH_FLOW_TIMING,
        CashFlowTimingQuality.EXACT_EFFECTIVE_TIME.value,
        CashFlowTimingQuality.LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED.value,
    }
    if quality not in allowed_quality:
        raise ValueError("valuation checkpoint cash-flow timing quality is invalid")
    if not isinstance(eligible, bool):
        raise ValueError("valuation checkpoint timing eligibility must be boolean")
    if quality == CashFlowTimingQuality.LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED.value:
        if eligible or status != "blocked_legacy_cash_flow_timing":
            raise ValueError("legacy cash-flow timing cannot be performance-eligible")
    elif quality == CashFlowTimingQuality.EXACT_EFFECTIVE_TIME.value:
        if not eligible or status != "timing_eligible_no_performance_method":
            raise ValueError("exact cash-flow timing readiness metadata is inconsistent")
    else:
        if not eligible or status != _NO_CASH_FLOW_TIMING:
            raise ValueError("no-cash-flow timing metadata is inconsistent")
    return latest_economic


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
    if not isinstance(payload, dict):
        raise ValueError("valuation checkpoint must be an object")
    schema = payload.get("schema")
    if schema not in {_SCHEMA_V1, _SCHEMA_V2}:
        raise ValueError("unsupported valuation checkpoint schema")
    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("valuation checkpoint account_id is invalid")
    if expected_account_id is not None and account_id != expected_account_id:
        raise ValueError("valuation checkpoint account binding mismatch")
    try:
        price_date = date.fromisoformat(payload["price_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("valuation checkpoint price_date is invalid") from exc
    latest_economic = _validate_timing_metadata(payload, schema)
    if price_date < latest_economic.astimezone(SHANGHAI).date():
        raise ValueError("valuation checkpoint price date predates account state")

    for field in ("account_fingerprint", "daily_content_fingerprint"):
        if not _is_sha256(payload.get(field)):
            raise ValueError(f"valuation checkpoint {field} must be lowercase SHA-256")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("valuation checkpoint evidence must be an object")
    for field in ("account_state_sha256", "raw_price_set_sha256"):
        if not _is_sha256(evidence.get(field)):
            raise ValueError(
                f"valuation checkpoint evidence.{field} must be lowercase SHA-256"
            )
    partition_sha = evidence.get("daily_bar_partition_sha256")
    if partition_sha is not None and not _is_sha256(partition_sha):
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
    if positions and partition_sha is None:
        raise ValueError("valuation checkpoint with positions requires partition evidence")
    if not positions and partition_sha is not None:
        raise ValueError("cash-only valuation cannot claim unused price-partition evidence")
    nav_fen = _nonnegative_int(payload.get("nav_fen"), "nav_fen")
    if nav_fen != cash_fen + market_value_sum:
        raise ValueError("valuation checkpoint NAV arithmetic mismatch")
    if payload.get("price_basis") != "raw_same_session_close":
        raise ValueError("valuation checkpoint must use raw same-session close")
    if payload.get("performance_claim") is not False:
        raise ValueError("valuation checkpoint cannot carry a performance claim")
    if payload.get("broker_order_authority") is not False:
        raise ValueError("valuation checkpoint cannot carry broker/order authority")
    fingerprint = payload.get("checkpoint_fingerprint")
    if not _is_sha256(fingerprint):
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


def _assert_inputs_unchanged(
    *,
    account_id: str,
    account_root: Path,
    product_root: Path,
    storage: ParquetStorage,
    account_fingerprint: str,
    daily_content_fingerprint: str,
    partition_path: Path,
    partition_sha: str | None,
) -> None:
    account_after = load_effective_account(account_id, account_root=account_root, storage=storage)
    snapshot_after = load_validated_latest_snapshot(product_root)
    if snapshot_after is None:
        raise ValueError("Daily snapshot disappeared during valuation materialization")
    if account_after["account_fingerprint"] != account_fingerprint:
        raise ValueError("account identity drifted during valuation materialization")
    if snapshot_after.report["content_fingerprint"] != daily_content_fingerprint:
        raise ValueError("Daily identity drifted during valuation materialization")
    if partition_sha is not None and _sha256_file(partition_path) != partition_sha:
        raise ValueError("raw daily-bar partition drifted during valuation materialization")


def _account_timing(account: dict) -> tuple[str, str, bool, str]:
    latest_economic = account.get("latest_economic_fact_at", account["as_of"])
    latest_reported = account.get("latest_reported_at", account["as_of"])
    quality = account.get("cash_flow_timing_quality", _NO_CASH_FLOW_TIMING)
    eligible = account.get("cash_flow_timing_performance_eligible", True)
    if not isinstance(eligible, bool):
        raise ValueError("effective account timing eligibility must be boolean")
    if quality == CashFlowTimingQuality.LEGACY_REPORTED_AS_EFFECTIVE_UNVERIFIED.value:
        if eligible:
            raise ValueError("legacy cash-flow timing cannot be performance-eligible")
        status = "blocked_legacy_cash_flow_timing"
    elif quality == CashFlowTimingQuality.EXACT_EFFECTIVE_TIME.value:
        if not eligible:
            raise ValueError("exact cash-flow timing unexpectedly marked ineligible")
        status = "timing_eligible_no_performance_method"
    elif quality == _NO_CASH_FLOW_TIMING:
        if not eligible:
            raise ValueError("no-cash-flow account unexpectedly marked timing-ineligible")
        status = _NO_CASH_FLOW_TIMING
    else:
        raise ValueError("effective account cash-flow timing quality is unknown")
    return latest_economic, latest_reported, eligible, status


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
    latest_economic, latest_reported, timing_eligible, performance_input_status = (
        _account_timing(account)
    )
    snapshot = load_validated_latest_snapshot(product_root)
    if snapshot is None:
        raise FileNotFoundError("no validated Daily snapshot available for account valuation")
    price_date = date.fromisoformat(snapshot.report["effective_as_of"])
    economic_at = _parse_aware(latest_economic, "latest_economic_account_fact_at")
    if price_date < economic_at.astimezone(SHANGHAI).date():
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

    timing_quality = account.get("cash_flow_timing_quality", _NO_CASH_FLOW_TIMING)
    account_state = {
        "account_id": account_id,
        "account_fingerprint": account["account_fingerprint"],
        "account_as_of": latest_economic,
        "latest_reported_at": latest_reported,
        "cash_flow_timing_quality": timing_quality,
        "cash_flow_timing_performance_eligible": timing_eligible,
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
    daily_fingerprint = snapshot.report["content_fingerprint"]
    core = {
        "schema": _SCHEMA_V2,
        "account_id": account_id,
        "account_fingerprint": account["account_fingerprint"],
        "account_as_of": latest_economic,
        "latest_economic_account_fact_at": latest_economic,
        "latest_reported_at": latest_reported,
        "cash_flow_timing_quality": timing_quality,
        "cash_flow_timing_performance_eligible": timing_eligible,
        "performance_input_status": performance_input_status,
        "price_date": price_date.isoformat(),
        "daily_content_fingerprint": daily_fingerprint,
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
            (
                "legacy cash-flow economic timing is unverified for performance"
                if not timing_eligible
                else "cash-flow timing alone does not constitute a performance method"
            ),
            "corporate-action cash/share postings remain outside account truth",
        ],
    }
    fingerprint = _canonical_hash(core)
    payload = {
        **core,
        "generated_at": generated_at.isoformat(),
        "checkpoint_fingerprint": fingerprint,
    }

    _assert_inputs_unchanged(
        account_id=account_id,
        account_root=account_root,
        product_root=product_root,
        storage=storage,
        account_fingerprint=account["account_fingerprint"],
        daily_content_fingerprint=daily_fingerprint,
        partition_path=partition_path,
        partition_sha=partition_sha,
    )

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
        _assert_inputs_unchanged(
            account_id=account_id,
            account_root=account_root,
            product_root=product_root,
            storage=storage,
            account_fingerprint=account["account_fingerprint"],
            daily_content_fingerprint=daily_fingerprint,
            partition_path=partition_path,
            partition_sha=partition_sha,
        )
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
                raise ValueError(
                    "concurrent valuation winner has wrong fingerprint"
                ) from exc
            _assert_inputs_unchanged(
                account_id=account_id,
                account_root=account_root,
                product_root=product_root,
                storage=storage,
                account_fingerprint=account["account_fingerprint"],
                daily_content_fingerprint=daily_fingerprint,
                partition_path=partition_path,
                partition_sha=partition_sha,
            )
            _activate(root, final_path, fingerprint)
            return final_path, existing, True
        published = validate_valuation_checkpoint(
            final_path,
            expected_account_id=account_id,
        )
        _assert_inputs_unchanged(
            account_id=account_id,
            account_root=account_root,
            product_root=product_root,
            storage=storage,
            account_fingerprint=account["account_fingerprint"],
            daily_content_fingerprint=daily_fingerprint,
            partition_path=partition_path,
            partition_sha=partition_sha,
        )
        _activate(root, final_path, fingerprint)
        return final_path, published, False
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
