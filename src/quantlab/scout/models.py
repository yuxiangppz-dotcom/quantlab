"""Small, explicit contracts for timestamped research evidence."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result


def web_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Evidence:
    source: str
    title: str
    body: str
    url: str | None
    published_at: str | None
    retrieved_at: str
    kind: str = "news"
    instrument_ids: tuple[str, ...] = ()
    evidence_id: str = ""
    event_dates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        timestamp(self.retrieved_at)
        if self.published_at:
            timestamp(self.published_at)
        if self.url and not web_url(self.url):
            raise ValueError("Evidence URL must be HTTP(S)")
        if not self.title.strip() or not self.source.strip():
            raise ValueError("Evidence needs a title and source")
        # Identity is based on content, not a fresh retrieval time.
        identity = fingerprint(
            [
                self.source,
                self.url,
                self.title,
                self.body,
                self.published_at,
                self.instrument_ids,
                self.event_dates,
            ]
        )
        object.__setattr__(self, "evidence_id", f"ev-{identity[:16]}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Coverage:
    source: str
    status: str
    count: int = 0
    detail: str = ""


@dataclass
class Candidate:
    instrument_id: str
    name: str
    metrics: dict[str, float | None]
    score: float
    routes: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def finite(value: object) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def admit_evidence(
    items: list[Evidence], cutoff: datetime, lookback_hours: int
) -> tuple[list[Evidence], dict[str, int]]:
    """Future dates rejected; missing publication time is explicit, not backdated."""
    admitted: list[Evidence] = []
    counts = {"future": 0, "stale": 0, "duplicate": 0, "undated": 0}
    seen: set[str] = set()
    for item in sorted(items, key=lambda x: timestamp(x.retrieved_at)):
        retrieved = timestamp(item.retrieved_at)
        published = timestamp(item.published_at) if item.published_at else None
        if retrieved > cutoff or (published and published > cutoff):
            counts["future"] += 1
            continue
        if published and (cutoff - published).total_seconds() > lookback_hours * 3600:
            counts["stale"] += 1
            continue
        # Exact cross-provider syndicated copies count once; no fuzzy factual merger.
        identity = fingerprint([" ".join(item.title.split()), " ".join(item.body.split())])
        if identity in seen:
            counts["duplicate"] += 1
            continue
        seen.add(identity)
        counts["undated"] += int(published is None)
        admitted.append(item)
    return admitted, counts
