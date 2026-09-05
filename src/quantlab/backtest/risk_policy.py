"""Point-in-time lifecycle risk-policy decisions.

The exit policy is deliberately narrower than a corporate-action engine. It
uses only trusted termination-decision facts that were available by the current
session and never invents a terminal settlement value.
"""

from __future__ import annotations

from datetime import date

from quantlab.backtest.delisting_facts import trusted_facts_available_as_of

EXIT_POLICY_NAME = "exit_after_termination_decision"
EXIT_POLICY_VERSION = "v1"
EXIT_POLICY_ID = f"{EXIT_POLICY_NAME}_{EXIT_POLICY_VERSION}"


def termination_decisions_available_as_of(
    facts: dict,
    as_of: date,
) -> dict[str, dict]:
    """Return one deterministic trusted termination fact per instrument.

    The returned mapping is derived from the supplied immutable snapshot on
    every session. Consequently a decision is persistent after available_from
    without carrying future knowledge into earlier dates.
    """
    decisions: dict[str, dict] = {}
    for instrument_id in sorted(facts):
        candidates = [
            fact
            for fact in trusted_facts_available_as_of(facts, instrument_id, as_of)
            if fact.get("fact_type") == "termination_decision"
        ]
        if not candidates:
            continue
        decisions[instrument_id] = min(
            candidates,
            key=lambda fact: (
                fact.get("available_from") or "",
                fact.get("fact_id") or "",
            ),
        )
    return decisions


def risk_policy_statistics(records) -> dict:
    """Summarize risk-policy audit records overall and per book."""

    def summarize(rows) -> dict:
        rows = list(rows)
        occurrence_keys = {(r.decision_date, r.instrument_id) for r in rows}
        return {
            "unique_exit_required_instruments": len(
                {r.instrument_id for r in rows}
            ),
            "exit_required_occurrences": len(occurrence_keys),
            "successful_forced_exits": sum(
                1 for r in rows if r.forced_sell_value > 0
            ),
            "pending_no_price_occurrences": sum(
                1 for r in rows if r.risk_state == "pending_no_price"
            ),
            "prevented_new_entries": sum(1 for r in rows if r.prevented_new_entry),
            "prevented_refills": sum(1 for r in rows if r.prevented_refill),
            "blocked_before_exit": sum(
                1 for r in rows if r.risk_state == "blocked_before_exit"
            ),
        }

    rows = list(records)
    overall_by_key = {}
    for row in rows:
        overall_by_key.setdefault((row.decision_date, row.instrument_id), row)
    overall = summarize(overall_by_key.values())
    for key, predicate in {
        "successful_forced_exits": lambda r: r.forced_sell_value > 0,
        "prevented_new_entries": lambda r: r.prevented_new_entry,
        "prevented_refills": lambda r: r.prevented_refill,
        "blocked_before_exit": lambda r: r.risk_state == "blocked_before_exit",
    }.items():
        overall[key] = len(
            {
                (r.decision_date, r.instrument_id)
                for r in rows
                if predicate(r)
            }
        )
    overall["per_book"] = {
        book: summarize([r for r in rows if r.book == book])
        for book in ("gross", "net")
    }
    return overall
