"""Actual browser image evidence; never infer a chart from a URL or page text."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path

from quantlab.scout.models import timestamp, web_url

VERSION = "recent_chart_visual_v1"
CHECKLIST = ("recovery", "support_test", "average_line", "higher_lows", "volume", "negative")


def load_charts(manifest: Path | None, codes, sessions, signal_date, cutoff_at) -> dict:
    """Load only original local captures explicitly bound to stock, dates, and cutoff."""
    result = {
        code: {
            "review_mode": "recent_chart_visual",
            "chart_status": "unavailable",
            "image_ids": [],
            "visible_dates": [],
            "intraday_quality": "unknown",
            "reason": "No readable original chart was supplied",
            "images": [],
        }
        for code in codes
    }
    if manifest is None:
        return result
    saved = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if saved.get("version") != VERSION:
        raise ValueError("Unknown chart evidence contract")
    recent = sorted(day for day in sessions if day <= str(signal_date))[-5:]
    image_root = Path(manifest).parent.resolve()
    for row in saved.get("charts", []):
        code = row["ts_code"]
        if code not in result:
            continue
        if timestamp(row["captured_at"]) > timestamp(str(cutoff_at)):
            raise ValueError("Chart was captured after the research cutoff")
        if timestamp(row["data_asof"]) > timestamp(str(cutoff_at)):
            raise ValueError("Chart data is from the future")
        if not web_url(row["source_url"]):
            raise ValueError("Chart source URL must be attributable")
        dates = row.get("visible_dates", [])
        if any(date.fromisoformat(day) > date.fromisoformat(str(signal_date)) for day in dates):
            raise ValueError("Target-day chart cannot confirm a prior frozen signal")
        if len(set(dates)) != len(dates) or not set(dates) <= set(recent):
            raise ValueError("Chart dates are not the recent completed trading window")
        original = image_root / row["image_file"]
        if not original.resolve().is_relative_to(image_root) or original.is_symlink():
            raise ValueError("Chart must be an original image in the explicit image directory")
        raw = original.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row["sha256"]:
            raise ValueError("Original chart bytes changed")
        mime = (
            "image/png"
            if raw.startswith(b"\x89PNG\r\n\x1a\n")
            else ("image/jpeg" if raw.startswith(b"\xff\xd8\xff") else None)
        )
        if mime is None or len(raw) > 2_000_000:
            raise ValueError("Unreadable or oversized chart capture")
        # These are capture assertions, not the model's chart-quality conclusion.
        required = ("stock_visible", "dates_visible", "price_axis_visible", "price_line_visible")
        readable = all(row.get(key) is True for key in required)
        item = {key: value for key, value in row.items() if key != "image_file"}
        item.update(
            image_id="chart-" + digest[:24],
            readable=readable,
            mime=mime,
            data_url="data:" + mime + ";base64," + base64.b64encode(raw).decode(),
        )
        packet = result[code]
        packet["images"].append(item)
        packet["image_ids"].append(item["image_id"])
        packet["visible_dates"] = sorted(set(packet["visible_dates"]) | set(dates))
        usable = {
            day for image in packet["images"] if image["readable"] for day in image["visible_dates"]
        }
        packet["chart_status"] = (
            "complete" if len(usable) >= 3 else ("partial" if usable else "unavailable")
        )
        packet["reason"] = None if len(usable) >= 3 else "Recent chart window is incomplete"
    return result


def chart_context(packet: dict) -> dict:
    return {
        **{
            key: value for key, value in packet.items() if key not in {"images", "intraday_quality"}
        },
        "images": [
            {key: value for key, value in image.items() if key != "data_url"}
            for image in packet["images"]
        ],
    }


def visual_errors(review: dict, packet: dict) -> list[dict]:
    """Require actual image references and per-day coverage before a window conclusion."""
    errors = []
    ids = set(packet["image_ids"])
    dates = set(packet["visible_dates"])
    cited = set(review.get("image_ids", []))
    if cited - ids:
        errors.append({"code": "unknown_chart_reference", "ids": sorted(cited - ids)})
    findings = review.get("per_day_findings", [])
    if not isinstance(findings, list) or any(not isinstance(row, dict) for row in findings):
        return errors + [{"code": "per_day_findings_contract_invalid"}]
    observed = {row.get("date") for row in findings}
    if observed - dates:
        errors.append({"code": "unseen_chart_date"})
    if not ids and (cited or findings or review.get("intraday_quality") != "unknown"):
        errors.append({"code": "no_image_no_visual_claim"})
    readable_dates = {
        day
        for image in packet["images"]
        if image.get("readable") and image["image_id"] in cited
        for day in image["visible_dates"]
    }
    if findings and any(
        any(not isinstance(row.get(field), str) or not row[field].strip() for field in CHECKLIST)
        for row in findings
    ):
        errors.append({"code": "complete_daily_visual_checklist_required"})
    if len(observed) != len(findings):
        errors.append({"code": "duplicate_chart_review_date"})
    if review.get("intraday_quality") == "good" and (
        packet["chart_status"] != "complete"
        or len(observed & readable_dates) < 3
        or not observed <= readable_dates
    ):
        errors.append({"code": "partial_window_cannot_be_good"})
    return errors
