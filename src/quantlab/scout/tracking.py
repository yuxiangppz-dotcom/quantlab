"""Forward close-to-close diagnostics, never simulated fills or strategy P&L."""

import json
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, fingerprint, finite, timestamp


def observe_run(run_dir: Path, canonical_dir: Path, output_root: Path) -> Path:
    report = json.loads((run_dir / "report.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    if fingerprint(report) != manifest["report_sha256"]:
        raise ValueError("Original report integrity check failed")
    if report["status"] == "demo":
        raise ValueError("Synthetic demo must not be scored as real outcomes")
    if output_root.resolve().is_relative_to(canonical_dir.resolve()):
        raise ValueError("Outcome output cannot be inside canonical data")
    now = datetime.now(SHANGHAI)
    generated = timestamp(report["finished_at"]).astimezone(SHANGHAI)
    storage = ParquetStorage(canonical_dir)
    days = sorted(
        {x.trade_date for x in storage.load_trading_calendar() if x.exchange == "SSE" and x.is_open}
    )
    # Anchor at first close strictly after publication, never at a pre-recommendation close.
    future = [
        day
        for day in days
        if datetime.combine(day, datetime.min.time().replace(hour=15), SHANGHAI) > generated
    ]
    codes = sorted(
        {x["instrument_id"] for x in report["baseline"]}
        | {x["instrument_id"] for x in report["selection"]["selected"]}
    )
    rows = []
    selected = report["selection"]["selected"]
    unheld = [
        row for row in selected if row.get("screening_status") != "hold_for_official_notice_review"
    ]
    groups = {
        "rule_baseline": [x["instrument_id"] for x in report["baseline"]],
        "ai_focus": [x["instrument_id"] for x in selected if x["status"] == "focus"],
        "ai_watch": [x["instrument_id"] for x in selected if x["status"] == "watch"],
        "ai_focus_without_notice_hold": [
            x["instrument_id"] for x in unheld if x["status"] == "focus"
        ],
        "ai_watch_without_notice_hold": [
            x["instrument_id"] for x in unheld if x["status"] == "watch"
        ],
        "official_notice_hold": [
            x["instrument_id"]
            for x in selected
            if x.get("screening_status") == "hold_for_official_notice_review"
        ],
    }
    source_flags = {
        x["instrument_id"]: sorted(x.get("context", {})) for x in report.get("candidates", [])
    }

    def adjusted_close(code: str, day: date) -> float | None:
        if datetime.combine(day, datetime.min.time().replace(hour=18), SHANGHAI) > now:
            return None
        bars = {x.instrument_id: x for x in storage.load_daily_bars_by_date(day)}
        adj = {x.instrument_id: x for x in storage.load_adj_factors_by_date(day)}
        if code not in bars or code not in adj:
            return None
        value = bars[code].close * adj[code].adj_factor
        return value if finite(value) and value > 0 else None

    for code in codes:
        for horizon in (1, 3, 5):
            anchor = future[0] if future else None
            target = future[horizon] if len(future) > horizon else None
            start = adjusted_close(code, anchor) if anchor else None
            end = adjusted_close(code, target) if target else None
            rows.append(
                {
                    "instrument_id": code,
                    "groups": [name for name, members in groups.items() if code in members],
                    "supplemental_sources": source_flags.get(code, []),
                    "horizon_sessions": horizon,
                    "anchor_session": anchor.isoformat() if anchor else None,
                    "target_session": target.isoformat() if target else None,
                    "adjusted_close_return": end / start - 1 if start and end else None,
                    "status": "observed" if start and end else "pending_or_missing",
                }
            )
    result = {
        "run_id": report["run_id"],
        "observed_at": now.isoformat(),
        "rows": rows,
        "groups": groups,
        "definition": "first close after publication to N sessions later; not trading returns",
        "limitations": (
            "No fills/fees/tradability; a missing notice hold does not prove tradability; "
            "revised adjustment data may change marks"
        ),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"{report['run_id']}-{uuid4().hex[:8]}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return path
