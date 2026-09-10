"""Immutable supplemental index history for retrospective research, not execution.

The publication flag is only a lower-bound check. Today's historical response
does not establish the original publication time or revision of each old bar.
Existing daily benchmark partitions are neither read nor written here.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from quantlab.data.models import DataValidationError, IndexDailyBar

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PUBLICATION = {"000001.SH": date(1991, 7, 15), "000688.SH": date(2020, 7, 23)}
_SOURCES = {
    "metadata": "https://tushare.pro/document/2?doc_id=94",
    "daily": "https://tushare.pro/document/2?doc_id=95",
    "star_publication": (
        "https://www.sse.com.cn/market/sseindex/diclosure/c/c_20200619_5130634.shtml"
    ),
    "sse_identity": (
        "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
        "detail/files/zh_CN/000001factsheet.pdf"
    ),
}
_SEMANTICS = {
    "purpose": "retrospective_research_context_only",
    "historical_revision_status": "unverified",
    "bar_publication_timestamp_status": "unverified",
    "execution_authority": False,
    "prepublication_bars": "backcast_warmup_only_after_index_publication",
    "units": {"ohlc": "index_points", "volume": "shares", "amount": "CNY"},
    "provider_normalization": {"vol_multiplier": 100, "amount_multiplier": 1000},
}


@dataclass(frozen=True)
class IndexContextSpec:
    instrument_id: str
    start: date
    end: date

    def payload(self) -> dict[str, str]:
        if self.instrument_id not in _PUBLICATION or self.start > self.end:
            raise DataValidationError("unsupported index or invalid request dates")
        return {
            "instrument_id": self.instrument_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "index_published_from": _PUBLICATION[self.instrument_id].isoformat(),
        }


DEFAULT_SPECS = (
    IndexContextSpec("000001.SH", date(2019, 7, 9), date(2026, 9, 10)),
    IndexContextSpec("000688.SH", date(2020, 1, 2), date(2026, 9, 10)),
)


class IndexContextProvider(Protocol):
    def get_index_metadata(self, instrument_id: str) -> list[dict[str, Any]]: ...

    def get_index_daily(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[IndexDailyBar]: ...


def _bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataValidationError("observation time must be timezone aware")
    return value.astimezone(_SHANGHAI).isoformat()


def _sessions(payload: bytes, spec: IndexContextSpec) -> list[str]:
    frame = pd.read_parquet(io.BytesIO(payload))
    required = {"exchange", "trade_date", "is_open"}
    if not required.issubset(frame.columns):
        raise DataValidationError("calendar schema is incomplete")
    frame = frame.loc[frame.exchange.eq("SSE")].copy()
    dates = pd.to_datetime(frame.trade_date, errors="raise")
    if dates.isna().any() or dates.dt.tz is not None or not dates.eq(dates.dt.normalize()).all():
        raise DataValidationError("calendar contains invalid dates")
    frame["trade_date"] = dates.dt.date
    frame = frame.loc[frame.trade_date.between(spec.start, spec.end)]
    expected = set(pd.date_range(spec.start, spec.end).date)
    if frame.trade_date.duplicated().any() or set(frame.trade_date) != expected:
        raise DataValidationError("SSE calendar has missing or duplicate dates")
    if not frame.is_open.isin([0, 1, False, True]).all():
        raise DataValidationError("calendar has unknown open flags")
    result = sorted(frame.loc[frame.is_open.eq(1), "trade_date"].astype(str).tolist())
    if not result:
        raise DataValidationError("request has no verified trading sessions")
    return result


def _metadata(rows: list[dict[str, Any]], spec: IndexContextSpec) -> dict[str, str | None]:
    if len(rows) != 1:
        raise DataValidationError("index identity must have exactly one response row")
    row = rows[0]
    if row.get("ts_code") != spec.instrument_id or row.get("market") != "SSE":
        raise DataValidationError("provider index identity does not match the request")
    result = {}
    for key in ("ts_code", "name", "fullname", "market", "publisher", "base_date", "list_date"):
        value = row.get(key)
        result[key] = None if value is None or pd.isna(value) else str(value).strip()
    if not result["name"] or not result["publisher"]:
        raise DataValidationError("index identity name or publisher is unavailable")
    for key in ("base_date", "list_date"):
        value = result[key]
        if not value or not re.fullmatch(r"\d{8}", value):
            raise DataValidationError("index identity date is unavailable")
        parsed = datetime.strptime(value, "%Y%m%d").date()
        if key == "base_date" and parsed > spec.start:
            raise DataValidationError("request predates the verified index base date")
        if key == "list_date" and parsed > _PUBLICATION[spec.instrument_id]:
            raise DataValidationError("provider publication conflicts with the reviewed boundary")
    return result


def _rows(bars: list[IndexDailyBar], spec: IndexContextSpec, sessions: list[str]) -> list[dict]:
    result = []
    for bar in bars:
        if bar.instrument_id != spec.instrument_id or type(bar.trade_date) is not date:
            raise DataValidationError("index response has a wrong identifier or date type")
        row = asdict(bar)
        row["trade_date"] = bar.trade_date.isoformat()
        for key in ("open", "high", "low", "close", "pre_close", "volume", "amount"):
            value = row[key]
            if key in ("volume", "amount") and (value is None or pd.isna(value)):
                row[key] = None  # Unknown volume/amount is never replaced by zero.
                continue
            if isinstance(value, bool) or value is None:
                raise DataValidationError(f"invalid index {key}")
            value = float(value)
            if (
                not math.isfinite(value)
                or value < 0
                or (key not in ("volume", "amount") and not value)
            ):
                raise DataValidationError(f"invalid index {key}")
            row[key] = value
        if not (
            row["low"]
            <= min(row["open"], row["close"])
            <= max(row["open"], row["close"])
            <= row["high"]
        ):
            raise DataValidationError("inconsistent index OHLC")
        row["index_published_on_date"] = bar.trade_date >= _PUBLICATION[spec.instrument_id]
        result.append(row)
    result.sort(key=lambda row: row["trade_date"])
    if [row["trade_date"] for row in result] != sessions:
        raise DataValidationError("index dates do not exactly cover expected calendar sessions")
    return result


def _year_chunks(spec: IndexContextSpec):
    start = spec.start
    while start <= spec.end:
        end = min(date(start.year, 12, 31), spec.end)
        yield start, end
        start = end + timedelta(days=1)


def load_index_context(path: Path, *, expected_fingerprint: str | None = None) -> dict:
    """Validate a snapshot; consumers should pin its returned fingerprint."""
    bundle = json.loads(path.read_bytes())
    _validate_bundle(bundle, path.stem, expected_fingerprint)
    return bundle


def _validate_bundle(bundle: dict, request_id: str, expected_fingerprint: str | None) -> None:
    fingerprint = bundle.get("fingerprint")
    content = {key: value for key, value in bundle.items() if key != "fingerprint"}
    if fingerprint != _hash(content) or (
        expected_fingerprint is not None and fingerprint != expected_fingerprint
    ):
        raise DataValidationError("index snapshot fingerprint mismatch")
    request = bundle["request"]
    if request["schema_version"] != 1 or request["semantics"] != _SEMANTICS:
        raise DataValidationError("unsupported index snapshot semantics")
    if request_id != _hash(request):
        raise DataValidationError("index snapshot request identity mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", bundle["code_head"]):
        raise DataValidationError("invalid index snapshot code identity")
    previous = _aware_time(bundle["started_at"])
    completed = _aware_time(bundle["completed_at"])
    if len(bundle["series"]) != len(request["specs"]):
        raise DataValidationError("index snapshot series count mismatch")
    for series, requested in zip(bundle["series"], request["specs"], strict=True):
        spec = IndexContextSpec(
            requested["instrument_id"],
            date.fromisoformat(requested["start"]),
            date.fromisoformat(requested["end"]),
        )
        if {key: value for key, value in requested.items() if key != "sessions"} != spec.payload():
            raise DataValidationError("index publication boundary mismatch")
        _metadata([series["metadata"]], spec)
        bars = [
            IndexDailyBar(
                **{
                    key: date.fromisoformat(value) if key == "trade_date" else value
                    for key, value in row.items()
                    if key != "index_published_on_date"
                }
            )
            for row in series["rows"]
        ]
        if _rows(bars, spec, requested["sessions"]) != series["rows"]:
            raise DataValidationError("index snapshot row semantics mismatch")
        times = [series["metadata_requested_at"], series["metadata_observed_at"]]
        expected_chunks = [
            (start, end)
            for start, end in _year_chunks(spec)
            if any(str(start) <= day <= str(end) for day in requested["sessions"])
        ]
        if len(series["requests"]) != len(expected_chunks):
            raise DataValidationError("index request receipt coverage mismatch")
        for receipt, (start, end) in zip(series["requests"], expected_chunks, strict=True):
            rows = [row for row in series["rows"] if str(start) <= row["trade_date"] <= str(end)]
            if (
                receipt["start"] != str(start)
                or receipt["end"] != str(end)
                or receipt["row_count"] != len(rows)
                or receipt["normalized_rows_sha256"] != _hash(rows)
            ):
                raise DataValidationError("index request receipt content mismatch")
            times.extend([receipt["requested_at"], receipt["observed_at"]])
        for value in times:
            current = _aware_time(value)
            if not previous <= current <= completed:
                raise DataValidationError("index observation timestamps are out of order")
            previous = current


def _aware_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataValidationError("index observation time must be timezone aware")
    return parsed


def sync_index_context(
    provider_factory: Callable[[], IndexContextProvider],
    calendar_path: Path,
    output_root: Path,
    *,
    code_head: str,
    specs: tuple[IndexContextSpec, ...] = DEFAULT_SPECS,
    clock: Callable[[], datetime] = _now,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, dict, bool]:
    """Acquire exactly these requests, or reuse them without constructing a provider.

    The caller must have provider/write authorization. Failures leave existing
    snapshots intact; a complete JSON is published atomically without overwrite.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", code_head):
        raise DataValidationError("a full source code commit is required")
    started_at = _timestamp(clock)
    today = datetime.fromisoformat(started_at).date()
    if not specs or len({spec.instrument_id for spec in specs}) != len(specs):
        raise DataValidationError("index requests must be nonempty and unique")
    if any(spec.end >= today for spec in specs):
        raise DataValidationError("only completed prior dates may be acquired")
    calendar = calendar_path.read_bytes()
    requests = []
    for spec in specs:
        item = spec.payload()
        item["sessions"] = _sessions(calendar, spec)
        requests.append(item)
    request = {
        "schema_version": 1,
        "provider": "tushare.index_basic+index_daily",
        "calendar_sha256": hashlib.sha256(calendar).hexdigest(),
        "specs": requests,
        "semantics": _SEMANTICS,
    }
    target = output_root / f"{_hash(request)}.json"
    if target.exists():
        bundle = load_index_context(target)
        if bundle["request"] != request:
            raise DataValidationError("cached request mismatch")
        return target, bundle, True
    provider = provider_factory()
    series = []
    for spec, requested in zip(specs, requests, strict=True):
        metadata_requested_at = _timestamp(clock)
        metadata = _metadata(provider.get_index_metadata(spec.instrument_id), spec)
        metadata_observed_at = _timestamp(clock)
        rows = []
        receipts = []
        for start, end in _year_chunks(spec):
            expected = [day for day in requested["sessions"] if str(start) <= day <= str(end)]
            if not expected:
                continue
            if progress:
                progress(f"index_daily {spec.instrument_id}: {start}..{end}")
            requested_at = _timestamp(clock)
            bars = provider.get_index_daily(spec.instrument_id, start, end)
            observed_at = _timestamp(clock)
            normalized = _rows(bars, spec, expected)
            rows.extend(normalized)
            receipts.append(
                {
                    "start": str(start),
                    "end": str(end),
                    "requested_at": requested_at,
                    "observed_at": observed_at,
                    "row_count": len(normalized),
                    "normalized_rows_sha256": _hash(normalized),
                }
            )
        series.append(
            {
                "metadata": metadata,
                "metadata_requested_at": metadata_requested_at,
                "metadata_observed_at": metadata_observed_at,
                "requests": receipts,
                "rows": rows,
            }
        )
    if calendar_path.read_bytes() != calendar:
        raise DataValidationError("calendar changed during index acquisition")
    bundle = {
        "request": request,
        "code_head": code_head,
        "started_at": started_at,
        "completed_at": _timestamp(clock),
        "sources": _SOURCES,
        "series": series,
    }
    bundle["fingerprint"] = _hash(bundle)
    _validate_bundle(bundle, target.stem, bundle["fingerprint"])
    output_root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=output_root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_bytes(bundle))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)  # Atomic and fails if another writer already published.
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target, load_index_context(target, expected_fingerprint=bundle["fingerprint"]), False


def main() -> None:
    from quantlab.data.tushare_provider import TushareProvider

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized-provider-write", action="store_true", required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise RuntimeError("index acquisition requires a clean worktree")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    upstream = subprocess.check_output(
        ["git", "rev-parse", "@{upstream}"], cwd=root, text=True
    ).strip()
    if head != upstream:
        raise RuntimeError("index acquisition requires pushed code")
    path, bundle, reused = sync_index_context(
        TushareProvider,
        root / "data/canonical/calendar/calendar.parquet",
        root / "data/canonical/research_index_context_v1",
        code_head=head,
        progress=lambda message: print(message, flush=True),
    )
    print(
        json.dumps(
            {
                "path": str(path),
                "fingerprint": bundle["fingerprint"],
                "reused": reused,
                "series": [
                    {"index": series["metadata"]["ts_code"], "rows": len(series["rows"])}
                    for series in bundle["series"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
