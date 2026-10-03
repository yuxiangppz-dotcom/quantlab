"""Append-only event history and deterministic research heuristics, never predictions."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean

from quantlab.data.storage import ParquetStorage
from quantlab.scout.models import SHANGHAI, Candidate, Evidence, fingerprint, finite, timestamp

VERSION = "opportunity_v1"
TYPE_LABELS = {
    "event_update": "新事件或实质更新",
    "trend_continuation": "强势延续",
    "pullback_improvement": "回撤后改善",
    "insufficient_evidence": "证据不足",
}
EVENT_KINDS = {
    "news",
    "official_announcement_index_unverified",
    "official_pdf_text_unverified",
    "company_event_date_only",
    "unverified_user_clue",
    "public_comment_unverified",
}
EVENT_TERMS = (
    ("routine_schedule", ("说明会", "股东大会", "披露日程")),
    ("risk_notice", ("风险提示", "异常波动", "诉讼", "澄清")),
    ("framework", ("框架协议", "意向协议")),
    ("order", ("订单", "合同", "中标")),
    ("earnings", ("业绩预告", "业绩快报", "年度报告", "半年度报告")),
    ("asset_change", ("收购", "重组", "资产出售")),
    ("capital_action", ("回购", "增持", "减持")),
    ("policy", ("政策", "补贴", "关税")),
)


def event_type(title: str) -> str:
    return next((kind for kind, words in EVENT_TERMS if any(w in title for w in words)), "unknown")


def _body(item: Evidence) -> tuple[str, dict]:
    try:
        raw = json.loads(item.body)
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # Retrieval/provider labels are not independent content. Original bytes stay archived.
    content = raw.get("content") if isinstance(raw.get("content"), str) else item.body
    return re.sub(r"\s+", "", content), raw


def nominal_contract_scale(raw: dict) -> dict:
    numerator, denominator = raw.get("order_amount_cny"), raw.get("annual_revenue_cny")
    valid = (
        finite(numerator)
        and numerator > 0
        and finite(denominator)
        and denominator > 0
        and raw.get("currency") == "CNY"
        and raw.get("denominator_scope") == "annual_consolidated_revenue"
        and re.fullmatch(r"\d{4}1231", str(raw.get("revenue_period") or ""))
    )
    return {
        "nominal_ratio": numerator / denominator if valid else None,
        "numerator_cny": numerator if finite(numerator) else None,
        "denominator_cny": denominator if finite(denominator) else None,
        "denominator_period": raw.get("revenue_period"),
        "status": "nominal_scale_only" if valid else "scale_or_denominator_unknown",
        "note": (
            "Nominal contract amount / annual consolidated revenue; "
            "not same-year revenue recognition, profit or surprise."
        ),
    }


def load_event_history(root: Path, cutoff: datetime) -> list[dict]:
    rows = {}
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot.get("version") != VERSION:
            raise ValueError("Unknown event index version")
        for row in snapshot["events"]:
            if (
                timestamp(row["first_seen_at"]) <= cutoff
                and timestamp(row["retrieved_at"]) <= cutoff
            ):
                existing = rows.get(row["record_id"])
                if existing is None or row["first_seen_at"] < existing["first_seen_at"]:
                    rows[row["record_id"]] = row
    return list(rows.values())


def event_records(
    evidence: list[Evidence], history: list[dict], session: date, cutoff: datetime
) -> tuple[list[dict], list[dict]]:
    """Content repetitions link together; changed same-event content retains its parent.

    Event matching is conservative: company/type/period AND explicit identity or
    normalized title. A similar industry or keyword cannot link economic events.
    Neither a recent publication nor first ingestion proves market novelty.
    """
    records: dict[str, dict] = {}
    excluded = []
    admitted = []
    for item in evidence:
        if timestamp(item.retrieved_at) > cutoff or (
            item.published_at and timestamp(item.published_at) > cutoff
        ):
            excluded.append({"evidence_id": item.evidence_id, "reason": "after_cutoff"})
            continue
        admitted.append(item)
    linked_indexes: dict[str, list[Evidence]] = defaultdict(list)
    indices_to_skip = set()
    for item in admitted:
        if item.kind != "official_announcement_index_unverified" or not item.url:
            continue
        full = next(
            (
                e
                for e in admitted
                if e.kind == "official_pdf_text_unverified"
                and e.url == item.url
                and e.instrument_ids == item.instrument_ids
            ),
            None,
        )
        if full:
            linked_indexes[full.evidence_id].append(item)
            indices_to_skip.add(item.evidence_id)
    for item in sorted(admitted, key=lambda x: (timestamp(x.retrieved_at), x.evidence_id)):
        if item.evidence_id in indices_to_skip:
            continue
        if item.kind not in EVENT_KINDS:
            continue
        body, raw = _body(item)
        kind = event_type(item.title)
        period_match = re.search(
            r"(?:19|20)\d{2}(?:年(?:半年度|年度|第[一二三四]季度)|H[12])", item.title
        )
        period = str(
            raw.get("end_date")
            or raw.get("period")
            or (period_match.group() if period_match else "unknown")
        )
        title_identity = re.sub(r"转载[:：]?|转发[:：]?|更新|补充|进展|\s", "", item.title)
        identity = str(raw.get("event_identity") or raw.get("order_id") or title_identity)
        for code in item.instrument_ids:
            key = fingerprint([code, kind, period, identity])
            # A title/link index has no document content: the collector's common
            # placeholder must never collapse distinct announcements.
            document_identity = fingerprint(
                [code, item.url or [title_identity, item.published_at, item.event_dates]]
            )
            is_index = item.kind == "official_announcement_index_unverified"
            content_key = (
                fingerprint(["index_document_v2", document_identity])
                if is_index
                else fingerprint([code, kind, period, body])
            )
            index_pair = next(
                (
                    r
                    for r in records.values()
                    if item.url
                    and r["url"] == item.url
                    and r["instrument_id"] == code
                    and item.kind == "official_announcement_index_unverified"
                ),
                None,
            )
            if index_pair:
                index_pair["source_ids"] = sorted(
                    set(index_pair["source_ids"] + [item.evidence_id])
                )
                index_pair["first_seen_at"] = min(index_pair["first_seen_at"], item.retrieved_at)
                continue
            if content_key in records:
                row = records[content_key]
                row["source_ids"] = sorted(set(row["source_ids"] + [item.evidence_id]))
                if len(body) > row["content_length"]:
                    row.update(
                        content_excerpt=item.body[:900],
                        content_length=len(body),
                        title_only=item.kind == "official_announcement_index_unverified",
                    )
                continue
            # Across URLs/providers, exact content is still a single independent item.
            body_hash = content_key
            same = next(
                (
                    r
                    for r in [*history, *records.values()]
                    if r["body_hash"] == body_hash
                    or (
                        (is_index or r["title_only"])
                        and r["instrument_id"] == code
                        and item.url
                        and r["url"] == item.url
                    )
                ),
                None,
            )
            if same and same in records.values():
                same["source_ids"] = sorted(set(same["source_ids"] + [item.evidence_id]))
                continue
            prior = same or next(
                (
                    r
                    for r in sorted(
                        [*history, *records.values()],
                        key=lambda r: timestamp(r["retrieved_at"]),
                        reverse=True,
                    )
                    if r["event_key"] == key
                ),
                None,
            )
            public_date = (
                timestamp(item.published_at).astimezone(SHANGHAI).date()
                if item.published_at
                else None
            )
            source_date = public_date or next(
                (
                    date.fromisoformat(d)
                    for d in item.event_dates
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
                ),
                None,
            )
            old = source_date is not None and (session - source_date).days > 35
            old_period = (
                period != "unknown"
                and period[:4].isdigit()
                and int(period[:4]) < session.year
                and source_date is None
            )
            novelty = (
                "routine_schedule"
                if kind == "routine_schedule"
                else "long_term_background"
                if old or old_period
                else "repeated_content"
                if same
                else "possible_update_requires_verification"
                if prior
                else "unseen_history_unknown"
            )
            row = {
                "record_id": "event-" + content_key[:20],
                "event_key": key,
                "body_hash": body_hash,
                "document_identity": document_identity,
                "identity_policy": "index_document_v2" if is_index else "full_content_v1",
                "instrument_id": code,
                "event_type": kind,
                "event_period": period,
                "source_ids": sorted(
                    {item.evidence_id, *(e.evidence_id for e in linked_indexes[item.evidence_id])}
                ),
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at,
                "publication_precision": "timestamp"
                if item.published_at
                else "date_only"
                if source_date
                else "unknown",
                "source_date": source_date.isoformat() if source_date else None,
                "first_seen_at": same["first_seen_at"]
                if same
                else min(
                    [
                        item.retrieved_at,
                        *(e.retrieved_at for e in linked_indexes[item.evidence_id]),
                    ],
                    key=timestamp,
                ),
                "retrieved_at": item.retrieved_at,
                "novelty": novelty,
                "previous_record_id": prior["record_id"] if prior else None,
                "previous_content_excerpt": prior["content_excerpt"][:500] if prior else None,
                "change": "content_changed_materiality_unknown"
                if prior and not same
                else "no_observed_increment"
                if same
                else "history_coverage_insufficient",
                "content_excerpt": item.body[:900],
                "content_length": len(body),
                "title_only": item.kind == "official_announcement_index_unverified",
                "relation": "direct_subject"
                if len(item.instrument_ids) == 1
                and item.kind
                in {
                    "official_pdf_text_unverified",
                    "official_announcement_index_unverified",
                    "company_event_date_only",
                }
                else "economic_link_unverified",
                "economic_effect": "unknown",
                "materiality": "unknown",
                "denominator": None,
                "h5_transmission": "model_hypothesis_required",
                "nominal_scale": nominal_contract_scale(raw),
                "note": (
                    "First system ingestion is not first market knowledge; "
                    "content change is not proven material change."
                ),
            }
            records[content_key] = row
    return list(records.values()), excluded


def append_event_snapshot(root: Path, events: list[dict], cutoff: datetime) -> Path:
    value = {
        "version": VERSION,
        "cutoff": cutoff.isoformat(),
        "indexed_at": datetime.now(SHANGHAI).isoformat(),
        "events": events,
    }
    path = root / f"{cutoff:%Y%m%dT%H%M%S%f}-{fingerprint(value)[:12]}.json"
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Event snapshot identity collision")
    else:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return path


def event_rank(row: dict) -> tuple:
    """Engineering ordering: update/body/direct/recent. No price momentum tie-break."""
    return (
        row["novelty"] not in {"possible_update_requires_verification", "unseen_history_unknown"},
        row["novelty"] != "possible_update_requires_verification",
        row["title_only"],
        row["relation"] != "direct_subject",
        row["event_type"] in {"routine_schedule", "unknown"},
        row["source_date"] is None,
        -(date.fromisoformat(row["source_date"]).toordinal()) if row["source_date"] else 0,
        row["record_id"],
    )


def annotate_universe(
    universe: dict[str, Candidate], events: list[dict], memberships: dict[str, str]
) -> dict:
    groups = defaultdict(list)
    for code, candidate in universe.items():
        if memberships.get(code):
            groups[memberships[code]].append(candidate)
    sectors = {}
    for sector, members in sorted(groups.items()):
        sectors[sector] = {
            "eligible_count": len(members),
            **{
                f"mean_return_{h}d": mean(x.metrics[f"return_{h}d"] for x in members)
                for h in (1, 5, 20)
            },
            **{
                f"positive_fraction_{h}d": mean(x.metrics[f"return_{h}d"] > 0 for x in members)
                for h in (1, 5)
            },
        }
    by_code = defaultdict(list)
    for row in events:
        by_code[row["instrument_id"]].append(row)
    for code, candidate in universe.items():
        rows = sorted(by_code[code], key=event_rank)
        m = candidate.metrics
        sector = sectors.get(memberships.get(code))
        for h in (1, 5, 20):
            m[f"relative_return_{h}d"] = (
                m[f"return_{h}d"] - sector[f"mean_return_{h}d"] if sector else None
            )
        improvement = (
            m.get("prior_return_19d", 0) > 0
            and m["return_5d"] <= 0.08
            and m.get("previous_return_1d") is not None
            and m["previous_return_1d"] < 0
            and m["return_1d"] > m["previous_return_1d"]
            and (m.get("close_location") or 0) >= 0.55
            and m["amount_ratio_5d"] >= 1.1
        )
        hints = []
        if any(
            r["novelty"] in {"unseen_history_unknown", "possible_update_requires_verification"}
            and r["event_type"] not in {"routine_schedule", "unknown"}
            for r in rows
        ):
            hints.append("event_update")
        if m["return_20d"] > 0 and m["return_5d"] > 0:
            hints.append("trend_continuation")
        if improvement:
            hints.append("pullback_improvement")
        candidate.context["opportunity"] = {
            "version": VERSION,
            "events": rows,
            "type_hints": hints or ["insufficient_evidence"],
            "pullback_qualified": improvement,
            "event_recall_key": list(event_rank(rows[0])) if rows else None,
            "new_unresolved_lead": any(
                r["novelty"] in {"unseen_history_unknown", "possible_update_requires_verification"}
                for r in rows
            ),
            "industry_context": sector,
            "heuristic_note": (
                "Type hints and route priorities are engineering heuristics, "
                "not calibrated forecasts."
            ),
        }
    return {"scope": "current_eligible_pool_current_membership", "industries": sectors}


def historical_limit_context(universe: dict[str, Candidate], sessions: list[date]) -> dict:
    pairs = []
    for code, candidate in universe.items():
        rows = candidate.context.get("tushare_upgrade", {}).get("limit_history", [])
        by_day = defaultdict(set)
        for row in rows:
            by_day[str(row.get("trade_date"))].add(row.get("limit"))
        for first, second in zip(sessions[:-1], sessions[1:], strict=True):
            prior = by_day.get(first.strftime("%Y%m%d"), set())
            following = by_day.get(second.strftime("%Y%m%d"), set())
            if prior == {"U"}:
                pairs.append(
                    {
                        "instrument_id": code,
                        "prior_session": first.isoformat(),
                        "next_session": second.isoformat(),
                        "state": "next_u_record"
                        if following == {"U"}
                        else "next_other_record"
                        if len(following) == 1 and following <= {"D", "Z"}
                        else "next_record_unknown_or_conflicting",
                    }
                )
    counts = Counter(p["state"] for p in pairs)
    observed = counts["next_u_record"] + counts["next_other_record"]
    return {
        "scope": "frozen_eligible_stocks_provider_limit_records_adjacent_calendar_sessions",
        "prior_u_pairs": len(pairs),
        "counts": dict(counts),
        "conditional_fraction_among_observed_records": counts["next_u_record"] / observed
        if observed
        else None,
        "pairs": pairs,
        "note": (
            "Absent records remain unknown; this is not a full-market "
            "continuation rate or execution probability."
        ),
    }


def price_reactions(
    events: list[dict], codes: set[str], storage: ParquetStorage, session: date
) -> list[dict]:
    """Date-bounded observable reaction; acquisition time never anchors market reaction."""
    days = sorted(
        {
            x.trade_date
            for x in storage.load_trading_calendar()
            if x.exchange == "SSE" and x.is_open and x.trade_date <= session
        }
    )
    cache = {}

    def close(code: str, day: date) -> float | None:
        if day not in cache:
            bars = {x.instrument_id: x.close for x in storage.load_daily_bars_by_date(day)}
            factors = {x.instrument_id: x.adj_factor for x in storage.load_adj_factors_by_date(day)}
            cache[day] = {
                key: value * factors[key]
                for key, value in bars.items()
                if key in factors and value > 0 and factors[key] > 0
            }
        return cache[day].get(code)

    output = []
    for row in events:
        if row["instrument_id"] not in codes:
            continue
        source_day = date.fromisoformat(row["source_date"]) if row["source_date"] else None
        prior = [d for d in days if source_day and d < source_day]
        anchor = prior[-1] if prior and source_day <= session else None
        start = close(row["instrument_id"], anchor) if anchor else None
        end = close(row["instrument_id"], session) if anchor else None
        output.append(
            {
                "event_id": row["record_id"],
                "instrument_id": row["instrument_id"],
                "start_session": anchor.isoformat() if anchor else None,
                "end_session": session.isoformat(),
                "adjusted_close_change": end / start - 1 if start and end else None,
                "status": "date_bounded_observation_timing_uncertain"
                if start and end
                else "window_or_prices_unknown",
                "note": (
                    "Previous exchange close before source date to as-of close; "
                    "not causal reaction or evidence of unpriced news. "
                    "Publication time may be unknown."
                ),
            }
        )
    return output


def model_record(
    record: dict, visible_ids: set[str] | None = None, visible_evidence: list[dict] | None = None
) -> dict:
    rows = [
        r for r in record["events"] if visible_ids is None or set(r["source_ids"]) & visible_ids
    ]
    keys = (
        "record_id",
        "instrument_id",
        "event_type",
        "event_period",
        "source_ids",
        "published_at",
        "publication_precision",
        "source_date",
        "first_seen_at",
        "retrieved_at",
        "novelty",
        "previous_record_id",
        "previous_content_excerpt",
        "change",
        "content_excerpt",
        "relation",
        "title_only",
        "nominal_scale",
    )
    events = [{k: r[k] for k in keys} for r in rows[:3]]
    if visible_evidence is not None:
        for event in events:
            shown = [e for e in visible_evidence if e["evidence_id"] in event["source_ids"]]
            event["source_ids"] = [e["evidence_id"] for e in shown]
            event["title_only"] = all(
                e["kind"] == "official_announcement_index_unverified" for e in shown
            )
            best = sorted(
                shown, key=lambda e: e["kind"] == "official_announcement_index_unverified"
            )
            event["content_excerpt"] = best[0]["body"][:900] if best else ""
            if event["title_only"]:
                event["nominal_scale"] = nominal_contract_scale({})
    return {
        "version": VERSION,
        "type_hints": record["type_hints"],
        "pullback_qualified": record["pullback_qualified"],
        "industry_context": record["industry_context"],
        "events": events,
        "omitted_event_ids": [r["record_id"] for r in record["events"] if r not in rows[:3]],
        "note": (
            "Hints are unvalidated; first_seen is ingestion, not market novelty; "
            "materiality requires explanation."
        ),
    }


def type_counts(codes: list[str], universe: dict) -> dict:
    return dict(
        Counter(
            label
            for code in codes
            for label in universe[code]
            .context.get("opportunity", {})
            .get("type_hints", ["legacy_unknown"])
        )
    )
