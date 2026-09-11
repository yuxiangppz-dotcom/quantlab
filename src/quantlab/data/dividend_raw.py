"""Bounded Tushare wire transport and lossless retrospective observation checks."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime

import requests

FIELDS = (
    "ts_code",
    "end_date",
    "ann_date",
    "div_proc",
    "stk_div",
    "stk_bo_rate",
    "stk_co_rate",
    "cash_div",
    "cash_div_tax",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
    "base_date",
    "base_share",
)
DATES = (
    "end_date",
    "ann_date",
    "record_date",
    "ex_date",
    "pay_date",
    "div_listdate",
    "imp_ann_date",
    "base_date",
)
MEASURES = ("stk_div", "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax", "base_share")
TRANSIENT = {"transport_error", "http_transient", "rate_limited"}
FATAL = {"permission_error", "schema_error", "secret_echo", "http_error", "provider_error"}


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("nonfinite JSON constant")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def inspect_response(raw, code, fields=FIELDS, row_cap=2000):
    """Retain all plans and nulls; classification never establishes entitlement."""
    try:
        payload = strict_json(raw)
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ValueError("missing numeric server code")
        server_code = payload["code"]
        if server_code != 0:
            message = str(payload.get("msg") or "")
            status = "provider_error"
            if server_code == 2002 or any(
                x in message.lower()
                for x in (
                    "token",
                    "permission",
                    "权限",
                    "积分",
                    "认证",
                    "授权",
                )
            ):
                status = "permission_error"
            elif any(x in message.lower() for x in ("频率", "每分钟", "rate limit", "too many")):
                status = "rate_limited"
            return {"status": status, "server_code": server_code, "server_message": message}
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("missing data object")
        names, items = data.get("fields"), data.get("items")
        if (
            not isinstance(names, list)
            or not all(isinstance(x, str) for x in names)
            or len(set(names)) != len(names)
            or not set(fields).issubset(names)
            or not isinstance(items, list)
        ):
            raise ValueError("missing/duplicate fields or invalid rows")
        rows = []
        for row in items:
            if not isinstance(row, list) or len(row) != len(names):
                raise ValueError("row length mismatch")
            item = dict(zip(names, row, strict=True))
            if item["ts_code"] != code:
                raise ValueError("response contains another instrument")
            rows.append(item)
        saturated = len(rows) >= row_cap
        # Unexpected server pagination metadata cannot silently become completeness.
        for container in (payload, data):
            if container.get("has_more") or container.get("truncated"):
                saturated = True
            total = container.get("total")
            if isinstance(total, (int, float)) and total > len(rows):
                saturated = True
        return {
            "status": "saturated" if saturated else ("nonempty" if rows else "empty"),
            "server_code": 0,
            "server_message": payload.get("msg"),
            "rows": len(rows),
            "returned_fields": names,
            "profile": profile_rows(rows),
        }
    except (ValueError, TypeError, KeyError, UnicodeError):
        return {"status": "schema_error", "rows": None}


def profile_rows(rows):
    """Counts are observations, not corrections; per-share fields stay per share."""
    nulls = {key: 0 for key in (*DATES, *MEASURES, "div_proc")}
    malformed = {key: 0 for key in (*DATES, *MEASURES)}
    ranges = {key: [] for key in DATES}
    statuses, seen, plans = {}, set(), {}
    duplicates = relevant = unknown_relevance = implemented_cash_missing_pay = 0
    for row in rows:
        text = json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False)
        duplicates += text in seen
        seen.add(text)
        status = row.get("div_proc")
        statuses[str(status)] = statuses.get(str(status), 0) + 1
        parsed = {}
        for key in nulls:
            value = row.get(key)
            if value is None or value == "":
                nulls[key] += 1
                continue
            if key in DATES:
                try:
                    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
                        raise ValueError
                    parsed[key] = datetime.strptime(value, "%Y%m%d").date().isoformat()
                    ranges[key].append(parsed[key])
                except ValueError:
                    malformed[key] += 1
            elif key in MEASURES:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    malformed[key] += 1
        event_dates = [
            parsed[x] for x in ("record_date", "ex_date", "pay_date", "div_listdate") if x in parsed
        ]
        relevant += any("2023-01-04" <= day <= "2026-09-10" for day in event_dates)
        unknown_relevance += not event_dates
        cash = row.get("cash_div_tax")
        if status == "实施" and isinstance(cash, (float, int)) and cash > 0:
            implemented_cash_missing_pay += "pay_date" not in parsed
        # A collision is only a review candidate. No provider event ID exists here.
        key = tuple(str(row.get(x)) for x in ("end_date", "ann_date", "div_proc", "imp_ann_date"))
        plans.setdefault(key, set()).add(text)
    return {
        "null_counts": nulls,
        "malformed_counts": malformed,
        "status_counts": statuses,
        "date_ranges": {key: [min(v), max(v)] if v else None for key, v in ranges.items()},
        "exact_duplicate_rows": duplicates,
        "candidate_identity_conflicts": sum(len(v) > 1 for v in plans.values()),
        "rows_with_date_in_required_window": relevant,
        "rows_without_valid_event_date": unknown_relevance,
        "implemented_positive_cash_without_valid_pay_date": implemented_cash_missing_pay,
    }


class WireClient:
    """One POST, no redirects/retries; token never enters receipts or exceptions."""

    def __init__(self, token, endpoint, *, session=None):
        self.token, self.endpoint = token, endpoint
        self.session = session or requests.Session()
        if session is None:
            self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

    def close(self):
        self.session.close()

    def fetch(self, parameters, cap):
        body, status, http_status = bytearray(), "received", None
        started = time.monotonic()
        try:
            with self.session.post(
                self.endpoint,
                json={**parameters, "token": self.token},
                timeout=(10, 10),
                stream=True,
                allow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                http_status = response.status_code
                response.raw.decode_content = False
                while len(body) < cap:
                    chunk = response.raw.read(min(8192, cap - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if time.monotonic() - started > 30:
                        status = "transport_error"
                        break
                if len(body) == cap:
                    status = "body_limit"
                elif status == "received" and http_status != 200:
                    if http_status in (401, 403):
                        status = "permission_error"
                    elif http_status == 429 or 500 <= http_status < 600:
                        status = "http_transient"
                    else:
                        status = "http_error"
        except Exception as exc:
            # Exception text can contain request details. Keep only its class name.
            status = "transport_error"
            error_type = type(exc).__name__
        else:
            error_type = None
        raw = bytes(body)
        digest = hashlib.sha256(raw).hexdigest()
        if self.token.encode() in raw:
            raw = raw.replace(self.token.encode(), b"[REDACTED]")
            status = "secret_echo"
        return raw, {
            "transport_status": status,
            "http_status": http_status,
            "received_bytes": len(body),
            "wire_sha256": digest,
            "error_type": error_type,
            "seconds": time.monotonic() - started,
            "raw_redacted": status == "secret_echo",
        }
