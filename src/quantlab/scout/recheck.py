"""Compare a fresh targeted announcement index with an immutable Scout run."""

from __future__ import annotations

from datetime import datetime

from quantlab.scout.models import Coverage, Evidence, fingerprint


def compare_announcement_index(
    report: dict, fresh: list[Evidence], coverage: Coverage, checked_at: datetime
) -> dict:
    """Report newly observed index links, never infer that missing links were withdrawn."""
    selected = [row["instrument_id"] for row in report["selection"]["selected"]]
    archived = {
        (code, item.get("url"))
        for item in report.get("evidence", [])
        if item.get("kind") == "official_announcement_index_unverified"
        for code in item.get("instrument_ids", [])
        if code in selected
    }
    seen = set()
    additions = []
    for item in fresh:
        for code in item.instrument_ids:
            key = (code, item.url)
            if code not in selected or key in archived or key in seen:
                continue
            seen.add(key)
            additions.append(
                {
                    "instrument_id": code,
                    "title": item.title,
                    "url": item.url,
                    "announcement_date": item.event_dates[0] if item.event_dates else None,
                    "published_at": item.published_at,
                    "retrieved_at": item.retrieved_at,
                    "halt_title": "停牌" in item.title,
                }
            )
    additions.sort(key=lambda item: (item["instrument_id"], item["url"]))
    return {
        "source_run_id": report["run_id"],
        "source_report_sha256": fingerprint(report),
        "source_market_session": report["market"]["session"],
        "checked_at": checked_at.isoformat(),
        "selected_stock_count": len(selected),
        "archived_selected_index_count": len(archived),
        "fresh_index_record_count": len(fresh),
        "coverage": vars(coverage),
        "not_in_archived_index": additions,
        "interpretation": (
            "Only a targeted index comparison at the check time. A new link was absent from the "
            "archived index, not necessarily first published after that run. Publication time "
            "remains unknown; failed, partial or empty queries cannot prove no announcement. "
            "Original model ranking and report are unchanged."
        ),
    }
