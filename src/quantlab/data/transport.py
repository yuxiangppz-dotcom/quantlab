"""Bounded TuShare transport; raw observations are never historical publication times."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd


class ProviderRequestError(RuntimeError):
    """Safe diagnostic without vendor response text or credentials."""


class ObservedClient:
    def __init__(
        self,
        client,
        *,
        archive=None,
        interval=0.3,
        attempts=3,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        if (
            not math.isfinite(interval)
            or interval < 0
            or type(attempts) is not int
            or not 1 <= attempts <= 5
        ):
            raise ValueError("invalid provider retry/rate policy")
        self.records = []
        self.client, self.archive = client, Path(archive) if archive else None
        self.interval, self.attempts = interval, attempts
        self.sleep, self.monotonic, self.last_call = sleep, monotonic, None

    def __getattr__(self, endpoint):
        if endpoint.startswith("_"):
            raise AttributeError(endpoint)

        def request(**params):
            for attempt in range(self.attempts):
                if self.last_call is not None:
                    self.sleep(max(0, self.interval - (self.monotonic() - self.last_call)))
                self.last_call = self.monotonic()
                try:
                    frame = getattr(self.client, endpoint)(**params)
                except Exception as exc:
                    message = str(exc).lower()
                    denied = any(x in message for x in ("permission", "权限", "积分", "token"))
                    transient = isinstance(exc, (TimeoutError, ConnectionError)) or any(
                        x in message
                        for x in (
                            "timeout",
                            "timed out",
                            "connection",
                            "频率",
                            "每分钟",
                            "429",
                            "502",
                            "503",
                        )
                    )
                    if transient and not denied and attempt + 1 < self.attempts:
                        self.sleep(min(8, 2**attempt))
                        continue
                    reason = "permission_denied" if denied else "request_failed"
                    raise ProviderRequestError(
                        f"{endpoint}:{reason}:{type(exc).__name__}"
                    ) from None
                if not isinstance(frame, pd.DataFrame):
                    raise ProviderRequestError(f"{endpoint}:unexpected_response_type")
                if self.archive is not None:
                    self._record(endpoint, params, frame)
                return frame
            raise AssertionError("unreachable")

        return request

    def _record(self, endpoint, params, frame):
        # Only the known non-secret query parameters are persisted.
        allowed = {
            "ts_code",
            "trade_date",
            "start_date",
            "end_date",
            "ann_date",
            "period",
            "exchange",
            "list_status",
            "fields",
            "market",
            "index_code",
        }
        if set(params) - allowed:
            raise ProviderRequestError(f"{endpoint}:unreviewed_archive_parameters")
        from quantlab.research.ml.artifacts import atomic_json

        payload = frame.to_json(orient="split", date_format="iso", force_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        folder = self.archive / endpoint
        folder.mkdir(parents=True, exist_ok=True)
        # Unique, complete response + receipt in one atomically replaced JSON file.
        path = folder / f"{uuid4().hex}.json"
        atomic_json(
            path,
            {
                "endpoint": endpoint,
                "parameters": params,
                "observed_at": datetime.now(UTC).isoformat(),
                "row_count": len(frame),
                "response_sha256": digest,
                "response": json.loads(payload),
                "historical_publication_certified": False,
            },
        )
        self.records.append((str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()))
