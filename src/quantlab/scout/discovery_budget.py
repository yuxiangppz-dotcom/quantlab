"""Versioned evidence research budget, not a price forecast or probability."""

from __future__ import annotations

from collections import defaultdict

from quantlab.scout.models import fingerprint, finite

VERSION = "evidence_marginal_v3_source_linked"
FORMULA = (
    "priority=max(60*direct_official_body_event, 40*qualified_market_anomaly, "
    "45*source_bound_non_sentiment_hypothesis) + "
    "2*body + 2*direct + 2*official + possible_update + timely; "
    "duplicate_hypothesis_overlap costs 1 per already allocated member; "
    "remaining attention/title/indirect leads compete for at most floor(limit/4) "
    "exploration slots; source-bound hypotheses require admitted news/official-PDF body refs, "
    "summary and counterargument, "
    "and remain unverified model linkages. No exploration seats are forced. "
    "Coefficients are engineering parameters, not probabilities."
)
MARKET_ROUTES = {"量价异动", "趋势突破", "回撤放量", "温和放量"}
OFFICIAL_RELATIONS = {
    "direct_subject",
    "direct",
    "official_pdf_text_unverified",
    "announcement_index_unverified",
    "new_company_event_date_only",
}


def build_dynamic_pool(
    universe: dict,
    hypotheses: list[dict],
    sector_codes: list[str],
    limit: int,
    attention_codes: list[str] | None = None,
    diagnostics: dict | None = None,
    *,
    asof: str | None = None,
    hypothesis_sources: dict[str, dict] | None = None,
) -> list[dict]:
    """Recall independently of momentum; deduplicate stocks and event clusters.

    Official body/direct material-looking events are research priorities even
    when market novelty or positive economic effect remain unknown. Title-only
    and attention evidence are exploration, never validated catalysts.
    """
    if limit < 0:
        raise ValueError("Research limit must be nonnegative")
    route_members = defaultdict(set)
    clues = defaultdict(list)
    clusters = {}
    for hypothesis in hypotheses:
        route = hypothesis.get("route_type")
        relation = hypothesis.get("relation", "unknown")
        route = route or ("event" if relation in OFFICIAL_RELATIONS else "sector")
        codes = sorted(set(hypothesis.get("instrument_ids", [])) & set(universe))
        if not codes:
            continue
        refs = sorted(set(hypothesis.get("evidence_ids", [])))
        summary = str(hypothesis.get("summary") or "").strip()
        counterargument = str(hypothesis.get("counterargument") or "").strip()
        # Caller supplies exact evidence admitted by the time/source boundary.
        # A string ev-id or a candidate's own unioned references is not proof.
        # Neither a source binding nor a body validates the economic linkage.
        eligible_refs = [
            ref
            for ref in refs
            if (source := (hypothesis_sources or {}).get(ref))
            and source.get("kind") in {"news", "official_pdf_text_unverified"}
            and str(source.get("body") or "").strip()
            and str(source.get("body") or "").strip() != str(source.get("title") or "").strip()
        ]
        source_bound = bool(
            eligible_refs
            and summary
            and counterargument
            and relation in {"direct", "supply_chain", "theme"}
            and route not in {"attention", "sentiment"}
        )
        priority_codes = [
            code
            for code in codes
            if source_bound
            and any(
                not hypothesis_sources[ref].get("instrument_ids")
                or code in hypothesis_sources[ref]["instrument_ids"]
                for ref in eligible_refs
            )
        ]
        overlap_group = "hyp-source-" + fingerprint([relation, route, refs or summary])[:20]
        cluster_id = (
            "hyp-linked-" + fingerprint([relation, route, refs, summary, counterargument])[:20]
        )
        cluster = clusters.setdefault(
            cluster_id,
            {
                "hypothesis_id": cluster_id,
                "allocation_overlap_group_id": overlap_group,
                "opportunity_type": "source_linked_hypothesis",
                "scope": "company_or_policy_industry_chain",
                "relation": relation,
                "route_type": route,
                "source_ids": refs,
                "instrument_ids": [],
                "first_known_at": None,
                "first_seen_at": asof,
                "recent_update": None,
                "increment": "model-proposed linkage; source increment is unverified",
                "support": summary,
                "counterevidence": counterargument or "linkage and relevance remain unknown",
                "official_source": False,
                "verification_state": "hypothesis_requires_source_and_economic_link_review",
                "source_bound_research_priority": False,
                "research_priority_instrument_ids": [],
                "research_priority_source_ids": eligible_refs,
                "timeliness_window": "source publication time and D1 mechanism require review",
                "coverage_limits": "bound source references are not validated catalysts",
            },
        )
        cluster["instrument_ids"] = sorted(set(cluster["instrument_ids"]) | set(codes))
        cluster["research_priority_instrument_ids"] = sorted(
            set(cluster["research_priority_instrument_ids"]) | set(priority_codes)
        )
        cluster["source_bound_research_priority"] = bool(
            cluster["research_priority_instrument_ids"]
        )
        for code in codes:
            clues[code].append((cluster_id, code in priority_codes))
            route_members[route].add(code)
            candidate = universe[code]
            label = f"信息关联:{relation}"
            if label not in candidate.routes:
                candidate.routes.append(label)
            candidate.evidence_ids = sorted(
                set(candidate.evidence_ids) | set(hypothesis.get("evidence_ids", []))
            )
    for code in set(sector_codes) & set(universe):
        route_members["sector"].add(code)
    for code in set(attention_codes or []) & set(universe):
        route_members["attention"].add(code)
        if "热度观察" not in universe[code].routes:
            universe[code].routes.append("热度观察")

    records = {}
    for code, candidate in sorted(universe.items()):
        opportunity = candidate.context.get("opportunity", {})
        events = opportunity.get("events", [])
        usable = [
            event
            for event in events
            if event.get("novelty")
            not in {"routine_schedule", "long_term_background", "repeated_content"}
            and event.get("event_type") not in {"unknown", "routine_schedule"}
        ]
        strong = [
            event
            for event in usable
            if not event.get("title_only", True)
            and event.get("relation") == "direct_subject"
            and event.get("official_source", False)
        ]
        qualified_market = bool(set(candidate.routes) & {"量价异动", "趋势突破"}) or (
            bool(set(candidate.routes) & {"回撤放量", "温和放量"})
            and opportunity.get("pullback_qualified", not bool(opportunity))
        )
        if qualified_market:
            route_members["momentum"].add(code)
            if set(candidate.routes) & {"回撤放量", "温和放量"}:
                route_members["pullback"].add(code)
        if usable:
            route_members["event"].add(code)
        memberships = sorted(route for route, codes in route_members.items() if code in codes)
        recalled = bool(memberships or usable)
        official = any(event.get("official_source", False) for event in usable)
        body = any(not event.get("title_only", True) for event in usable)
        direct = any(event.get("relation") == "direct_subject" for event in usable)
        update = any(
            event.get("novelty") == "possible_update_requires_verification" for event in usable
        )
        timely = any(event.get("next_session_timely", False) for event in usable)
        cluster_ids = [cluster_id for cluster_id, _ in clues[code]]
        linked = any(qualified for _, qualified in clues[code])
        for event in usable:
            cluster_id = (
                "hyp-"
                + fingerprint(
                    [
                        event.get("republication_cluster")
                        or event.get("document_identity")
                        or event.get("event_key")
                        or event["record_id"],
                        event.get("event_type"),
                    ]
                )[:20]
            )
            cluster_ids.append(cluster_id)
            cluster = clusters.setdefault(
                cluster_id,
                {
                    "hypothesis_id": cluster_id,
                    "opportunity_type": "event_update",
                    "event_type": event.get("event_type"),
                    "scope": "direct_company_or_linked_group",
                    "first_known_at": event.get("published_at"),
                    "first_seen_at": event.get("first_seen_at"),
                    "recent_update": event.get("retrieved_at"),
                    "novelty": event.get("novelty"),
                    "increment": event.get("change"),
                    "source_ids": [],
                    "instrument_ids": [],
                    "support": "source content requires economic-effect verification",
                    "counterevidence": "unknown market novelty/materiality; see full source",
                    "timeliness_window": (
                        "D0 close to information cutoff; exact time may be unknown"
                    ),
                    "coverage_limits": "bounded acquired sources, not all market information",
                },
            )
            cluster["source_ids"] = sorted(set(cluster["source_ids"]) | set(event["source_ids"]))
            cluster["instrument_ids"] = sorted(set(cluster["instrument_ids"]) | {code})
        if qualified_market:
            cluster_id = "hyp-market-" + fingerprint([code, asof])[:16]
            cluster_ids.append(cluster_id)
            clusters[cluster_id] = {
                "hypothesis_id": cluster_id,
                "opportunity_type": "market_anomaly",
                "scope": "company",
                "instrument_ids": [code],
                "source_ids": [],
                "first_known_at": asof,
                "first_seen_at": asof,
                "recent_update": asof,
                "increment": "observed price/amount state; catalyst unknown",
                "support": "deterministic market routes",
                "counterevidence": candidate.cautions,
                "timeliness_window": "latest completed trading session",
                "coverage_limits": "daily prices, not orderbook or future participation",
            }
        exploration = recalled and not strong and not qualified_market
        value = (
            max(60 * bool(strong), 40 * qualified_market, 45 * linked)
            + 2 * body
            + 2 * direct
            + 2 * official
            + update
            + timely
        )
        records[code] = {
            "instrument_id": code,
            "recalled": recalled,
            "exploration": exploration,
            "priority": value,
            "strong_direct_event": bool(strong),
            "facts": {
                "official": official,
                "body": body,
                "direct": direct,
                "possible_update_not_proven_increment": update,
                "timely": timely,
                "qualified_market_anomaly": qualified_market,
                "source_bound_non_sentiment_hypothesis": linked,
            },
            "hypothesis_ids": sorted(set(cluster_ids)),
            "allocation_overlap_group_ids": sorted(
                {clusters[key].get("allocation_overlap_group_id", key) for key in cluster_ids}
            ),
            "recall_routes": memberships,
            "research_cost": 1,
            "unresolved_question": (
                "verify event novelty/economic linkage and strongest restriction"
                if usable or linked
                else "test next-session price hypothesis; catalyst unknown"
            ),
        }
    selected, exposure = [], defaultdict(int)
    exploration_used = 0
    exploration_cap = limit // 4
    remaining = {code for code, record in records.items() if record["recalled"]}
    while remaining and len(selected) < limit:
        eligible = [
            code
            for code in remaining
            if not records[code]["exploration"] or exploration_used < exploration_cap
        ]
        if not eligible:
            break

        def ordering(code):
            record = records[code]
            overlap = max(
                (exposure[key] for key in record["allocation_overlap_group_ids"]), default=0
            )
            score = universe[code].score if finite(universe[code].score) else -1
            return (
                -(record["priority"] - overlap),
                -score if not record["strong_direct_event"] else 0,
                code,
            )

        code = min(eligible, key=ordering)
        record = records[code]
        record["marginal_priority_at_allocation"] = record["priority"] - max(
            (exposure[key] for key in record["allocation_overlap_group_ids"]), default=0
        )
        selected.append(code)
        remaining.remove(code)
        exploration_used += int(record["exploration"])
        for key in record["allocation_overlap_group_ids"]:
            exposure[key] += 1
    funnel, exclusions = [], []
    for code, record in records.items():
        chosen = code in selected
        reason = (
            None
            if chosen
            else "evidence_weak_or_no_relevant_anomaly"
            if not record["recalled"]
            else "exploration_budget_excluded"
            if record["exploration"] and exploration_used >= exploration_cap
            else "research_budget_excluded"
        )
        funnel.append(
            {
                "instrument_id": code,
                "eligible": True,
                "recalled": record["recalled"],
                "shortlist": chosen if limit > 24 else None,
                "deep": chosen if limit <= 24 else None,
                "ranked": None,
                "selected": None,
                "stage_at": asof,
                "exclusion_reason": reason,
                "budget_record": record,
            }
        )
        if reason and record["recalled"]:
            exclusions.append(
                {
                    "instrument_id": code,
                    "reason": reason,
                    "strong_direct_event": record["strong_direct_event"],
                    "priority": record["priority"],
                    "hypothesis_ids": record["hypothesis_ids"],
                    "meaning": "not deeply researched, not a negative judgment",
                }
            )
    if diagnostics is not None:
        diagnostics.update(
            {
                "version": VERSION,
                "formula": FORMULA,
                "exploration_cap": exploration_cap,
                "exploration_used": exploration_used,
                "opportunity_hypotheses": list(clusters.values()),
                "funnel": funnel,
                "budget_exclusions": exclusions,
            }
        )
        for route, codes in route_members.items():
            diagnostics[route] = {
                "raw_unique": len(codes),
                "allocated": len(set(selected) & codes),
                "not_selected": len(codes - set(selected)),
            }
    return [
        {
            **universe[code].to_dict(),
            "allocation_route": "evidence_marginal",
            "route_rank": rank,
            "recall_routes": records[code]["recall_routes"],
            "research_budget": records[code],
        }
        for rank, code in enumerate(selected, 1)
    ]
