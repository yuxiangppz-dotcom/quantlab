"""Frozen-report price observations, never fills or investment returns."""

import json
import os
import statistics
import tempfile
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, fingerprint, finite, timestamp

HORIZONS = (1, 3, 5, 10)
PRIMARY_HORIZON = 5
DEFINITION = "d_open_to_h_close_adjusted_v1"


def _checked_report(run_dir: Path) -> tuple[dict, str]:
    report = json.loads((run_dir / "report.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    digest = fingerprint(report)
    if digest != manifest["report_sha256"]:
        raise ValueError("Original report integrity check failed")
    if report["status"] == "demo":
        raise ValueError("Synthetic demo must not be scored as real outcomes")
    return report, digest


def _write_snapshot(result: dict, output_root: Path) -> Path:
    """Same report and same observed facts reuse a snapshot; changed facts append one."""
    output_root.mkdir(parents=True, exist_ok=True)
    facts = {key: value for key, value in result.items() if key != "observed_at"}
    digest = fingerprint(facts)
    path = output_root / f"{result['run_id']}-{digest[:12]}.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if (
            fingerprint({key: value for key, value in existing.items() if key != "observed_at"})
            != digest
        ):
            raise ValueError("Observation snapshot hash collision or modification")
        return path
    # Exclusive creation ensures old observations are never rewritten.
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def observe_run(run_dir: Path, canonical_dir: Path, output_root: Path) -> Path:
    report, report_sha256 = _checked_report(run_dir)
    if report.get("nextday_freeze"):
        from quantlab.scout.nextday_tracking import observe_nextday

        return observe_nextday(run_dir, canonical_dir, output_root)
    if output_root.resolve().is_relative_to(canonical_dir.resolve()):
        raise ValueError("Outcome output cannot be inside canonical data")
    if report.get("timing") is not None:
        return _observe_current(report, report_sha256, canonical_dir, output_root)
    return _observe_legacy(report, canonical_dir, output_root)


def _observe_legacy(report: dict, canonical_dir: Path, output_root: Path) -> Path:
    """Old reports retain their original first-future-close to later-close definition."""
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

    def adjusted_close(code: str, day: date | None) -> tuple[float | None, str]:
        if day is None:
            return None, "calendar_unavailable"
        if datetime.combine(day, datetime.min.time().replace(hour=18), SHANGHAI) > now:
            return None, "not_yet_due"
        bars = {x.instrument_id: x for x in storage.load_daily_bars_by_date(day)}
        adj = {x.instrument_id: x for x in storage.load_adj_factors_by_date(day)}
        if code not in bars or code not in adj:
            return None, "price_or_factor_missing"
        value = bars[code].close * adj[code].adj_factor
        if not finite(value) or value <= 0:
            return None, "adjusted_price_invalid"
        return value, "available"

    for code in codes:
        for horizon in (1, 3, 5):
            anchor = future[0] if future else None
            target = future[horizon] if len(future) > horizon else None
            start, anchor_state = adjusted_close(code, anchor)
            end, target_state = adjusted_close(code, target)
            if anchor_state != "available":
                status = f"anchor_{anchor_state}"
            elif target_state != "available":
                status = f"target_{target_state}"
            else:
                status = "observed"
            rows.append(
                {
                    "instrument_id": code,
                    "groups": [name for name, members in groups.items() if code in members],
                    "supplemental_sources": source_flags.get(code, []),
                    "horizon_sessions": horizon,
                    "anchor_session": anchor.isoformat() if anchor else None,
                    "target_session": target.isoformat() if target else None,
                    "anchor_price_status": anchor_state,
                    "target_price_status": target_state,
                    "adjusted_close_return": end / start - 1 if start and end else None,
                    "status": status,
                }
            )
    result = {
        "run_id": report["run_id"],
        "observed_at": now.isoformat(),
        "rows": rows,
        "groups": groups,
        "definition": "first close after publication to N sessions later; not trading returns",
        "limitations": (
            "No fills/fees/tradability; missing bar/factor does not identify a suspension; "
            "a missing notice hold does not prove tradability; "
            "revised adjustment data may change marks"
        ),
    }
    result["definition_version"] = "legacy_future_close_v1"
    return _write_snapshot(result, output_root)


def summarize_tracking(report_root: Path, observation_root: Path, output_path: Path) -> Path:
    """One predeclared latest eligible version per D; never select by outcomes."""
    reports: dict[str, tuple[dict, str, Path]] = {}
    run_dirs = sorted(report_root.iterdir()) if report_root.exists() else []
    for run_dir in run_dirs:
        if not run_dir.is_dir() or not (run_dir / "manifest.json").exists():
            continue
        try:
            report, digest = _checked_report(run_dir)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        timing = report.get("timing")
        if report.get("nextday_freeze"):
            continue
        if not timing or not timing.get("primary_eligible") or not timing.get("target_session"):
            continue
        try:
            d = date.fromisoformat(timing["target_session"])
            finished = timestamp(report["finished_at"]).astimezone(SHANGHAI)
            generated = timestamp(timing.get("generated_at") or report["finished_at"]).astimezone(
                SHANGHAI
            )
        except (TypeError, ValueError):
            continue
        opening = datetime.combine(d, time(9, 30), SHANGHAI)
        if (
            finished >= opening
            or generated >= opening
            or report.get("status") not in {"live_research_unvalidated", "complete"}
        ):
            continue
        previous = reports.get(d.isoformat())
        if previous is None or (generated, report["run_id"]) > (
            timestamp(
                previous[0]["timing"].get("generated_at") or previous[0]["finished_at"]
            ).astimezone(SHANGHAI),
            previous[0]["run_id"],
        ):
            reports[d.isoformat()] = (report, digest, run_dir)

    observations: dict[tuple[str, str], tuple[dict, Path]] = {}
    if observation_root.exists():
        for path in sorted(observation_root.glob("*.json")):
            try:
                item = json.loads(path.read_text())
                if item.get("definition_version") != DEFINITION:
                    continue
                key = (item["run_id"], item["report_sha256"])
                earlier = observations.get(key)
                if earlier is None or timestamp(item["observed_at"]) > timestamp(
                    earlier[0]["observed_at"]
                ):
                    observations[key] = (item, path)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    entries = []
    buckets: dict[str, dict[int, list[float]]] = {
        "focus": {h: [] for h in HORIZONS},
        "watch": {h: [] for h in HORIZONS},
    }
    missing: dict[str, dict[int, Counter]] = {
        "focus": {h: Counter() for h in HORIZONS},
        "watch": {h: Counter() for h in HORIZONS},
    }
    sample_counts: dict[str, dict[int, int]] = {
        group: {h: 0 for h in HORIZONS} for group in buckets
    }
    stock_windows: dict[str, list[tuple[date, date | None]]] = {}
    for d, (report, digest, run_dir) in sorted(reports.items()):
        selected = report.get("selection", {}).get("selected", [])
        focus = [x["instrument_id"] for x in selected if x.get("status") == "focus"]
        watch = [x["instrument_id"] for x in selected if x.get("status") == "watch"]
        observation = observations.get((report["run_id"], digest))
        rows = observation[0].get("rows", []) if observation else []
        h10_ends = {
            row["instrument_id"]: date.fromisoformat(row["end_session"])
            for row in rows
            if row.get("horizon_sessions") == 10 and row.get("end_session")
        }
        for row in selected:
            if row.get("status") == "focus":
                code = row["instrument_id"]
                stock_windows.setdefault(code, []).append(
                    (date.fromisoformat(d), h10_ends.get(code))
                )
        for group, codes in (("focus", focus), ("watch", watch)):
            for horizon in HORIZONS:
                sample_counts[group][horizon] += len(codes)
            seen_rows: set[tuple[str, int]] = set()
            for row in rows:
                if row.get("group") != group or row.get("horizon_sessions") not in HORIZONS:
                    continue
                horizon = row["horizon_sessions"]
                code = row.get("instrument_id")
                if code not in codes or (code, horizon) in seen_rows:
                    continue
                seen_rows.add((code, horizon))
                value = row.get("d_open_to_end_close_adjusted")
                if row.get("status") == "observed" and finite(value):
                    buckets[group][horizon].append(value)
                else:
                    missing[group][horizon][row.get("status", "unknown")] += 1
            for horizon in HORIZONS:
                absent = len(codes) - sum((code, horizon) in seen_rows for code in codes)
                if absent:
                    reason = "observation_row_missing" if observation else "observation_not_run"
                    missing[group][horizon][reason] += absent
        entries.append(
            {
                "d_session": d,
                "run_id": report["run_id"],
                "report_path": str(run_dir / "report.json"),
                "report_sha256": digest,
                "observation_path": str(observation[1]) if observation else None,
                "focus_count": len(focus),
                "watch_count": len(watch),
                "no_focus_reason": (
                    report.get("selection", {}).get("no_recommendation_reason")
                    or report.get("selection", {}).get("market_view")
                    if not focus
                    else None
                ),
            }
        )

    summaries = {}
    for group in ("focus", "watch"):
        summaries[group] = {}
        for horizon in HORIZONS:
            values = sorted(buckets[group][horizon])
            summaries[group][str(horizon)] = {
                "original_candidates": sample_counts[group][horizon],
                "calculable": len(values),
                "positive": sum(value > 0 for value in values),
                "positive_fraction": (sum(value > 0 for value in values) / len(values))
                if values
                else None,
                "mean": statistics.fmean(values) if values else None,
                "median": statistics.median(values) if values else None,
                "minimum": values[0] if values else None,
                "maximum": values[-1] if values else None,
                "distribution": {
                    "below_minus_10pct": sum(value < -0.1 for value in values),
                    "minus_10_to_0pct": sum(-0.1 <= value < 0 for value in values),
                    "zero": sum(value == 0 for value in values),
                    "0_to_10pct": sum(0 < value <= 0.1 for value in values),
                    "above_10pct": sum(value > 0.1 for value in values),
                },
                "missing_reasons": dict(sorted(missing[group][horizon].items())),
            }
    repeated_stocks = {
        code: len(windows) for code, windows in stock_windows.items() if len(windows) > 1
    }
    overlaps = 0
    unknown_overlaps = 0
    for appearances in stock_windows.values():
        ordered = sorted(appearances)
        for (_, end), (later, _) in zip(ordered, ordered[1:], strict=False):
            if end is None:
                unknown_overlaps += 1
            elif later <= end:
                overlaps += 1
    result = {
        "definition_version": DEFINITION,
        "primary_horizon_sessions": PRIMARY_HORIZON,
        "covered_report_dates": len(entries),
        "no_focus_dates": sum(item["focus_count"] == 0 for item in entries),
        "effective_reports": entries,
        "summary": summaries,
        "repeated_focus_stocks": repeated_stocks,
        "overlapping_focus_windows_count": overlaps,
        "overlap_unknown_count": unknown_overlaps,
        "limitations": (
            "Only frozen eligible reports are counted. One latest pre-open report per D "
            "is chosen without inspecting outcomes. Watchlist is separate. These are "
            "correlated price observations, not independent trades or net performance."
        ),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=output_path.name + ".", dir=output_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, output_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return output_path


def _eligible_target(report: dict, days: list[date]) -> tuple[date | None, str]:
    timing = report["timing"]
    raw = timing.get("target_session")
    if not raw:
        return None, "target_session_missing"
    target = date.fromisoformat(raw)
    if target not in days:
        return target, "target_not_in_exchange_calendar"
    if not timing.get("primary_eligible"):
        return target, "not_primary_eligible"
    generated = timestamp(timing.get("generated_at") or report["finished_at"]).astimezone(SHANGHAI)
    finished = timestamp(report["finished_at"]).astimezone(SHANGHAI)
    market_open = datetime.combine(target, time(9, 30), SHANGHAI)
    if generated >= market_open or finished >= market_open:
        return target, "finished_after_target_open"
    if report["status"] not in {"live_research_unvalidated", "complete"}:
        return target, "report_not_complete"
    return target, "eligible"


def _observe_current(
    report: dict, report_sha256: str, canonical_dir: Path, output_root: Path
) -> Path:
    now = datetime.now(SHANGHAI)
    storage = ParquetStorage(canonical_dir)
    days = sorted(
        {x.trade_date for x in storage.load_trading_calendar() if x.exchange == "SSE" and x.is_open}
    )
    d, eligibility = _eligible_target(report, days)
    selected = report.get("selection", {}).get("selected", [])
    focus = [x["instrument_id"] for x in selected if x.get("status") == "focus"]
    watch = [x["instrument_id"] for x in selected if x.get("status") == "watch"]
    groups = {"focus": focus, "watch": watch}
    result = {
        "run_id": report["run_id"],
        "report_sha256": report_sha256,
        "observed_at": now.isoformat(),
        "definition_version": DEFINITION,
        "definition": (
            "D is the frozen report's target exchange session and H=1 is D. "
            "Gap compares adjusted D open with adjusted preceding session close; "
            "H observation compares adjusted D open with Hth exchange-session close. "
            "This is price observation, not a fill, same-day trade or net return."
        ),
        "primary_horizon_sessions": PRIMARY_HORIZON,
        "d_session": d.isoformat() if d else None,
        "primary_eligibility": eligibility,
        "report_kind": report["timing"].get("report_kind"),
        "groups": groups,
        "rows": [],
        "limitations": (
            "Daily bars are only observed after session completion; a missing bar alone "
            "does not prove suspension. Adjustment factors are an observation scale, "
            "not executable prices. No fills, costs, or account returns are inferred."
        ),
    }
    if eligibility != "eligible":
        return _write_snapshot(result, output_root)

    d_index = days.index(d)
    prior = days[d_index - 1] if d_index else None
    # Load each calendar partition once across every selected security and horizon.
    needed = {d, *(days[d_index + h - 1] for h in HORIZONS if d_index + h - 1 < len(days))}
    if prior:
        needed.add(prior)
    bars = {
        day: {item.instrument_id: item for item in storage.load_daily_bars_by_date(day)}
        for day in needed
    }
    factors = {
        day: {item.instrument_id: item for item in storage.load_adj_factors_by_date(day)}
        for day in needed
    }
    limits = {d: {item.instrument_id: item for item in storage.load_daily_price_limits_by_date(d)}}
    partition_present = {
        day: storage.daily_bars_path(day).exists() and storage.adj_factor_path(day).exists()
        for day in needed
    }

    def price(code: str, day: date | None, field: str) -> tuple[float | None, str, float | None]:
        if day is None:
            return None, "calendar_unavailable", None
        if datetime.combine(day, time(18), SHANGHAI) > now:
            return None, "not_yet_due", None
        if not partition_present[day]:
            return None, "partition_missing", None
        bar = bars[day].get(code)
        if bar is None:
            return None, "bar_missing_or_halt_unknown", None
        factor = factors[day].get(code)
        if factor is None:
            return None, "factor_missing", None
        value = getattr(bar, field) * factor.adj_factor
        if (
            not finite(value)
            or value <= 0
            or not finite(factor.adj_factor)
            or factor.adj_factor <= 0
        ):
            return None, "adjusted_price_invalid", None
        return value, "available", factor.adj_factor

    for candidate in selected:
        code = candidate["instrument_id"]
        if candidate.get("status") not in {"focus", "watch"}:
            continue
        d_open, open_status, d_factor = price(code, d, "open")
        known_halt = candidate.get("trading_status") in {"halted", "suspended"}
        if open_status == "bar_missing_or_halt_unknown" and known_halt:
            open_status = "reported_halt_without_bar"
        previous_close, previous_status, previous_factor = price(code, prior, "close")
        d_close, d_close_status, _ = price(code, d, "close")
        gap = d_open / previous_close - 1 if d_open and previous_close else None
        direction = d_close / previous_close - 1 if d_close and previous_close else None
        d_bar = bars[d].get(code)
        limit = limits[d].get(code)
        one_price = (
            bool(
                d_bar
                and all(finite(x) for x in (d_bar.open, d_bar.high, d_bar.low, d_bar.close))
                and max(d_bar.open, d_bar.high, d_bar.low, d_bar.close)
                - min(d_bar.open, d_bar.high, d_bar.low, d_bar.close)
                < 0.005
            )
            if open_status == "available"
            else None
        )
        limit_state = "unknown"
        if open_status == "available" and limit:
            if abs(d_bar.close - limit.up_limit) < 0.005:
                limit_state = "closed_at_upper_limit"
            elif abs(d_bar.close - limit.down_limit) < 0.005:
                limit_state = "closed_at_lower_limit"
            else:
                limit_state = "not_closed_at_limit"
        for horizon in HORIZONS:
            end = days[d_index + horizon - 1] if d_index + horizon - 1 < len(days) else None
            end_close, close_status, end_factor = price(code, end, "close")
            if open_status != "available":
                status = f"d_open_{open_status}"
            elif close_status != "available":
                status = f"end_{close_status}"
            else:
                status = "observed"
            action_status = "unknown"
            if d_factor and end_factor:
                action_status = (
                    "adjustment_factor_changed"
                    if abs(d_factor / end_factor - 1) > 1e-10
                    else "same_adjustment_factor"
                )
            result["rows"].append(
                {
                    "instrument_id": code,
                    "group": candidate["status"],
                    "horizon_sessions": horizon,
                    "d_session": d.isoformat(),
                    "end_session": end.isoformat() if end else None,
                    "previous_session": prior.isoformat() if prior else None,
                    "d_open_status": open_status,
                    "previous_close_status": previous_status,
                    "end_close_status": close_status,
                    "d_open_gap_adjusted": gap,
                    "previous_close_to_d_close_adjusted_direction": direction,
                    "d_open_to_end_close_adjusted": (
                        end_close / d_open - 1 if d_open and end_close else None
                    ),
                    "d_one_price_session": one_price,
                    "d_limit_state": limit_state,
                    "d_reported_halt": known_halt,
                    "company_action_status": action_status,
                    "prior_to_d_factor_changed": (
                        abs(previous_factor / d_factor - 1) > 1e-10
                        if previous_factor and d_factor
                        else None
                    ),
                    "status": status,
                }
            )
    return _write_snapshot(result, output_root)
