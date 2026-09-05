"""Systematic lifecycle-event normalization, PIT access, and coverage audits.

This module owns the canonical event boundary.  It intentionally does not know
about portfolios or backtest execution; consumers receive immutable events and
may only query them by ``available_from``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from quantlab.data.models import (
    RawLifecycleAnnouncement,
    SecurityLifecycleEvent,
    StockSTStatus,
    SuspensionRecord,
)

CLASSIFIER_VERSION = "termination_decision_title_v1"
EVENT_TYPE_TERMINATION_DECISION = "termination_decision"
VERIFICATION_TRUSTED = "trusted"
VERIFICATION_REVIEW_REQUIRED = "review_required"
VERIFICATION_REJECTED = "rejected"
VERIFICATION_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ClassifiedAnnouncement:
    announcement: RawLifecycleAnnouncement
    category: str
    verification_status: str
    classification_reason: str


def classify_announcement(announcement: RawLifecycleAnnouncement) -> ClassifiedAnnouncement:
    """High-recall title classifier with conservative auto-trust criteria."""
    title = announcement.title.replace(" ", "")
    if "终止上市" in title and ("决定" in title or "作出" in title):
        return ClassifiedAnnouncement(
            announcement, "decision", VERIFICATION_TRUSTED,
            "formal termination-decision wording in announcement title",
        )
    if "终止上市" in title:
        return ClassifiedAnnouncement(
            announcement, "possible", VERIFICATION_REVIEW_REQUIRED,
            "termination wording without unambiguous formal decision",
        )
    if "风险提示" in title or "退市风险警示" in title:
        category = "risk_warning"
    elif "听证" in title:
        category = "hearing"
    elif "整理期" in title:
        category = "arrangement"
    elif "生效" in title or "摘牌" in title:
        category = "effective"
    elif "退市" in title or "上市" in title:
        category = "procedure"
    else:
        category = "other"
    return ClassifiedAnnouncement(
        announcement, category, VERIFICATION_REJECTED,
        f"title classified as {category}; not a termination decision",
    )


def first_open_session_after(calendar: list[tuple[date, bool]], event_date: date) -> date | None:
    """Conservative daily PIT rule: usable only on the next open session."""
    open_dates = sorted({d for d, is_open in calendar if is_open})
    return next((d for d in open_dates if d > event_date), None)


def normalize_lifecycle_events(
    announcements: list[RawLifecycleAnnouncement],
    calendar: list[tuple[date, bool]],
) -> list[SecurityLifecycleEvent]:
    """Classify raw index rows and create deterministic canonical v0 events."""
    events: list[SecurityLifecycleEvent] = []
    for classified in (classify_announcement(item) for item in announcements):
        ann = classified.announcement
        if classified.category != "decision" or ann.instrument_id is None:
            continue
        available_from = first_open_session_after(calendar, ann.announcement_date)
        if available_from is None:
            # The announcement is real but the local calendar cannot support a
            # safe PIT decision yet; retain no executable canonical event.
            continue
        identity = "|".join((
            ann.source, ann.source_record_id, ann.instrument_id,
            EVENT_TYPE_TERMINATION_DECISION, ann.announcement_date.isoformat(),
            ann.content_fingerprint,
        ))
        event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        events.append(SecurityLifecycleEvent(
            event_id=event_id,
            instrument_id=ann.instrument_id,
            event_type=EVENT_TYPE_TERMINATION_DECISION,
            event_date=ann.announcement_date,
            event_time=ann.announcement_time,
            available_from=available_from,
            effective_date=None,
            source=ann.source,
            source_record_id=ann.source_record_id,
            source_url=ann.source_url,
            raw_title=ann.title,
            verification_status=classified.verification_status,
            classification_reason=(
                f"{CLASSIFIER_VERSION}: {classified.classification_reason}; "
                "daily PIT=next_open_session_after_announcement_date"
            ),
            content_fingerprint=ann.content_fingerprint,
        ))
    return sorted(events, key=lambda item: (item.available_from, item.event_id))


def events_available_as_of(
    events: list[SecurityLifecycleEvent], as_of: date
) -> list[SecurityLifecycleEvent]:
    """Return only trusted canonical events usable at the requested session."""
    return [
        event for event in events
        if event.verification_status == VERIFICATION_TRUSTED and event.available_from <= as_of
    ]


def termination_decisions_available_as_of(
    events: list[SecurityLifecycleEvent], as_of: date
) -> dict[str, SecurityLifecycleEvent]:
    """Select one deterministic trusted termination event per instrument."""
    candidates = [
        event for event in events_available_as_of(events, as_of)
        if event.event_type == EVENT_TYPE_TERMINATION_DECISION
    ]
    by_instrument: dict[str, SecurityLifecycleEvent] = {}
    for event in candidates:
        existing = by_instrument.get(event.instrument_id)
        if existing is None or (event.available_from, event.event_id) < (
            existing.available_from, existing.event_id
        ):
            by_instrument[event.instrument_id] = event
    return by_instrument


def event_as_fact(event: SecurityLifecycleEvent) -> dict:
    """Adapt a canonical event to the existing risk-policy fact contract."""
    return {
        "fact_type": event.event_type,
        "fact_id": event.event_id,
        "available_from": event.available_from.isoformat(),
        "effective_date": event.effective_date.isoformat() if event.effective_date else None,
        "source": event.source_url or event.source,
        "content_verified": event.verification_status == VERIFICATION_TRUSTED,
        "public_time_verified": event.verification_status == VERIFICATION_TRUSTED,
    }


def events_to_facts(events: list[SecurityLifecycleEvent]) -> dict:
    """Build a deterministic legacy adapter, preserving the engine API for v0."""
    output: dict[str, dict] = {}
    for event in events:
        output.setdefault(event.instrument_id, {"facts": []})["facts"].append(event_as_fact(event))
    return output


def golden_event_audit(
    manual_facts: dict, events: list[SecurityLifecycleEvent]
) -> list[dict]:
    """Compare manual verified facts with systematic events without hardcoded IDs."""
    rows: list[dict] = []
    by_instrument: dict[str, list[SecurityLifecycleEvent]] = {}
    for event in events:
        by_instrument.setdefault(event.instrument_id, []).append(event)
    for instrument_id, entry in sorted(manual_facts.items()):
        for fact in entry.get("facts", []):
            if fact.get("fact_type") != EVENT_TYPE_TERMINATION_DECISION:
                continue
            candidates = by_instrument.get(instrument_id, [])
            exact = [e for e in candidates if e.event_date.isoformat() == fact.get("document_date")]
            compatible = [e for e in candidates if e.verification_status == VERIFICATION_TRUSTED]
            matched = exact[0] if exact else (compatible[0] if compatible else None)
            status = "exact" if exact else ("compatible" if matched else "missing")
            rows.append({
                "instrument_id": instrument_id,
                "manual_fact_id": fact.get("fact_id"),
                "systematic_event_id": matched.event_id if matched else None,
                "status": status,
                "manual_date": fact.get("document_date"),
                "systematic_date": matched.event_date.isoformat() if matched else None,
                "available_from": matched.available_from.isoformat() if matched else None,
                "classification": matched.verification_status if matched else None,
                "source_url": matched.source_url if matched else None,
            })
    return rows


def lifecycle_coverage_rows(
    instruments: list[str],
    delist_dates: dict[str, date],
    events: list[SecurityLifecycleEvent],
    stock_st: list[StockSTStatus],
    suspensions: list[SuspensionRecord],
    bars_by_instrument: dict[str, list[tuple[date, float]]],
) -> list[dict]:
    """Produce explicit coverage diagnostics; missing suspension rows are unknown."""
    decisions = termination_decisions_available_as_of(events, date.max)
    st_ids = {row.instrument_id for row in stock_st}
    suspensions_by_id: dict[str, list[SuspensionRecord]] = {}
    for suspension in suspensions:
        suspensions_by_id.setdefault(suspension.instrument_id, []).append(suspension)
    rows = []
    for instrument_id in sorted(instruments):
        event = decisions.get(instrument_id)
        prices = sorted(bars_by_instrument.get(instrument_id, []))
        first_after = None
        if event:
            first_after = next((d for d, _ in prices if d >= event.available_from), None)
        suspension_state = None
        if event and instrument_id in suspensions_by_id:
            suspension_state = any(
                suspension.suspend_date <= event.available_from
                and (
                    suspension.resume_date is None
                    or suspension.resume_date >= event.available_from
                )
                for suspension in suspensions_by_id[instrument_id]
            )
        rows.append({
            "instrument_id": instrument_id,
            "delist_date": (
                delist_dates[instrument_id].isoformat()
                if instrument_id in delist_dates
                else None
            ),
            "st_observed": instrument_id in st_ids,
            "termination_decision_available": event is not None,
            "decision_available_from": event.available_from.isoformat() if event else None,
            "suspension_context_observed": instrument_id in suspensions_by_id,
            "suspended_on_available_from": suspension_state,
            "first_tradable_price_after_available_from": (
                first_after.isoformat() if first_after else None
            ),
            "last_observed_price_date": prices[-1][0].isoformat() if prices else None,
            "exit_window_exists": bool(event and first_after),
        })
    return rows
