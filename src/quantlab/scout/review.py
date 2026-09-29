"""Validate a separate, post-run human review without changing archived research."""

from __future__ import annotations

import re
from datetime import datetime

from quantlab.scout.models import web_url

SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_post_run_review(report: dict, review: dict) -> dict:
    """Bind every note to a selected stock and its archived official PDF index."""
    run_id = report.get("run_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(review, dict)
        or review.get("run_id") != run_id
    ):
        raise ValueError("review run_id must match the archived report")
    try:
        reviewed_at = datetime.fromisoformat(review["reviewed_at"])
        finished_at = datetime.fromisoformat(report["finished_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("reviewed_at and report finished_at must be ISO datetimes") from exc
    if reviewed_at.utcoffset() is None or finished_at.utcoffset() is None:
        raise ValueError("reviewed_at and report finished_at must include timezones")
    if reviewed_at <= finished_at:
        raise ValueError("post-run review must be later than report generation")
    rows = review.get("findings")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("review must contain 1 to 20 findings")
    selected = {row["instrument_id"] for row in report["selection"]["selected"]}
    official = {
        (code, item.get("url"))
        for item in report.get("evidence", [])
        if item.get("kind") == "official_announcement_index_unverified"
        for code in item.get("instrument_ids", [])
    }
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each finding must be an object")
        code, url = row.get("instrument_id"), row.get("source_url")
        if (
            not isinstance(code, str)
            or not isinstance(url, str)
            or code not in selected
            or (code, url) not in official
            or not web_url(url)
        ):
            raise ValueError(
                "review finding must bind to a selected stock and archived official URL"
            )
        if (code, url) in seen:
            raise ValueError("duplicate review finding")
        seen.add((code, url))
        if row.get("category") not in {"risk", "context"}:
            raise ValueError("review category must be risk or context")
        summary = row.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
            raise ValueError("review summary must contain 1 to 600 characters")
        digest = row.get("source_sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("review source_sha256 must be a lowercase SHA-256 digest")
    return review
