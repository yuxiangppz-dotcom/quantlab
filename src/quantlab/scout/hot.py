"""Point-in-time snapshots of the public Eastmoney A-share popularity top 100.

Popularity is a discovery clue, not an issuer fact or a calibrated buy signal.
The AKShare endpoint does not label the as-of time of each row; retrieval time
must never be presented as the provider's publication time.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from quantlab.scout.models import Coverage, timestamp

SOURCE = "akshare:eastmoney_hot_rank"
INTERFACE = "stock_hot_rank_em"
SOURCE_URL = "https://guba.eastmoney.com/rank/"
CODE_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
MAX_RESPONSE_BYTES = 1_000_000
MAX_COMPARISON_AGE = timedelta(days=7)


def _fetch_akshare_rows() -> list[dict]:
    """Isolate AKShare so an optional source has a hard wall-clock deadline."""
    script = (
        "import akshare as ak, sys\n"
        "frame = ak.stock_hot_rank_em()\n"
        "if len(frame) > 1000: raise ValueError('Oversized hot-rank frame')\n"
        "sys.stdout.write(frame.to_json(orient='records', force_ascii=False))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        timeout=35,
    )
    if len(completed.stdout) > MAX_RESPONSE_BYTES:
        raise ValueError("Oversized hot-rank response")
    rows = json.loads(completed.stdout)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Hot-rank response must be a list of objects")
    return rows


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _compatible_previous(previous: dict | None, now: datetime) -> bool:
    if not isinstance(previous, dict) or (
        previous.get("schema_version") != 1
        or previous.get("source") != SOURCE
        or previous.get("interface") != INTERFACE
        or previous.get("scope") != "current_top100"
        or not isinstance(previous.get("normalized"), list)
    ):
        return False
    try:
        before = timestamp(previous["retrieved_at"])
        return timedelta(0) < now - before <= MAX_COMPARISON_AGE
    except (KeyError, TypeError, ValueError):
        return False


def normalize_hot_rank(
    raw_rows: list[dict], now: datetime, previous_snapshot: dict | None = None
) -> tuple[dict | None, Coverage]:
    """Preserve provider rows and reject ambiguous identities/rank drift.

    Only stocks present in *both* comparable snapshots receive a rank delta.
    An absent previous top-100 row has unknown previous rank, never rank zero.
    """
    if now.tzinfo is None:
        raise ValueError("Hot-rank retrieval time must be timezone-aware")
    if not isinstance(raw_rows, list) or len(raw_rows) > 1000:
        return None, Coverage(SOURCE, "failed", detail="invalid_or_oversized_response")
    compatible = _compatible_previous(previous_snapshot, now)
    previous_ranks = {}
    if compatible:
        previous_ranks = {
            item["instrument_id"]: item["rank"]
            for item in previous_snapshot["normalized"]
            if isinstance(item, dict)
            and isinstance(item.get("instrument_id"), str)
            and type(item.get("rank")) is int
            and 1 <= item["rank"] <= 100
        }
    normalized: list[dict] = []
    seen_codes: set[str] = set()
    seen_ranks: set[int] = set()
    rejected = 0
    for raw in raw_rows:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        rank = raw.get("当前排名")
        code = raw.get("代码")
        name = raw.get("股票名称")
        match = CODE_RE.fullmatch(code) if isinstance(code, str) else None
        if (
            type(rank) is not int
            or not 1 <= rank <= 100
            or rank in seen_ranks
            or not match
            or code in seen_codes
            or not isinstance(name, str)
            or not name.strip()
        ):
            rejected += 1
            continue
        seen_codes.add(code)
        seen_ranks.add(rank)
        prior = previous_ranks.get(f"{match[2]}.{match[1]}")
        if not compatible:
            change_status = (
                "unknown_no_previous_snapshot"
                if previous_snapshot is None
                else "unknown_incompatible_previous_snapshot"
            )
        elif prior is None:
            change_status = "unknown_outside_previous_top100"
        else:
            change_status = "comparable"
        normalized.append(
            {
                "instrument_id": f"{match[2]}.{match[1]}",
                "provider_code": code,
                "name": name.strip()[:80],
                "rank": rank,
                "last_price": _optional_number(raw.get("最新价")),
                "price_change": _optional_number(raw.get("涨跌额")),
                "price_change_pct": _optional_number(raw.get("涨跌幅")),
                "previous_rank": prior,
                "rank_delta": prior - rank if prior is not None else None,
                "change_status": change_status,
            }
        )
    normalized.sort(key=lambda item: item["rank"])
    if not normalized:
        return None, Coverage(SOURCE, "failed", detail=f"no_valid_top100_rows; rejected={rejected}")
    snapshot = {
        "schema_version": 1,
        "source": SOURCE,
        "interface": INTERFACE,
        "source_url": SOURCE_URL,
        "retrieved_at": now.isoformat(),
        "data_asof": None,
        "data_asof_detail": "Provider endpoint does not return an exact ranking timestamp",
        "scope": "current_top100",
        "previous_retrieved_at": previous_snapshot["retrieved_at"] if compatible else None,
        "comparison_status": "compatible" if compatible else "unavailable",
        "raw_rows": raw_rows,
        "normalized": normalized,
    }
    status = "ok" if len(normalized) == 100 and not rejected else "partial"
    detail = (
        f"current top-100 snapshot; {len(raw_rows)} provider rows, "
        f"{len(normalized)} normalized, {rejected} rejected; provider as-of unknown; "
        "rank delta only for stocks present in both compatible snapshots"
    )
    return snapshot, Coverage(SOURCE, status, len(normalized), detail)


def collect_hot_rank(
    now: datetime,
    online: bool,
    previous_snapshot: dict | None = None,
    *,
    fetcher: Callable[[], list[dict]] | None = None,
) -> tuple[dict | None, Coverage]:
    """Fetch the free public AKShare ranking once; failure degrades locally."""
    if not online:
        return None, Coverage(SOURCE, "disabled", detail="offline mode")
    try:
        raw = (fetcher or _fetch_akshare_rows)()
        retrieved_at = now if fetcher is not None else datetime.now(now.tzinfo)
        return normalize_hot_rank(raw, retrieved_at, previous_snapshot)
    except Exception as exc:
        # Never persist exception text: dependency and network errors can include URLs.
        return None, Coverage(SOURCE, "failed", detail=type(exc).__name__)


def save_hot_snapshot(path: Path, snapshot: dict) -> Path:
    """Write one immutable UTF-8 snapshot; a second run needs a new path."""
    if snapshot.get("source") != SOURCE or snapshot.get("schema_version") != 1:
        raise ValueError("Not a validated hot-rank snapshot")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(snapshot, file, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
        file.write("\n")
    return path
