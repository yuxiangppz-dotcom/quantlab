"""Minimal point-in-time delisting fact accessors.

Facts are stored keyed by ``instrument_id``. Each fact separates:

- ``content_verified``: the fact's content has been verified against a source;
- ``public_time_verified``: the fact's historical public time has independent
  traceable evidence (not just a document signature date or a URL path date);
- ``available_from``: the first session the strategy may use the fact, derived
  only under the verified "next trading day open" convention when the public
  date is verified but has no intraday time;
- ``effective_date``: when the market event takes effect.

Decision entry (``trusted_facts_available_as_of``) requires BOTH content and
public-time verification; ``available_from`` alone never admits an unknown fact.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def load_delisting_facts(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def facts_available_as_of(
    facts: dict,
    instrument_id: str,
    as_of: date,
) -> list[dict]:
    """Return facts whose ``available_from`` is known and ``<= as_of`` (audit view).

    This is a non-strict view; use :func:`trusted_facts_available_as_of` for
    decision input.
    """
    entry = facts.get(instrument_id)
    if entry is None:
        return []
    out: list[dict] = []
    for fact in entry.get("facts", []):
        available = fact.get("available_from")
        if available is None:
            continue
        try:
            available_date = date.fromisoformat(available)
        except (TypeError, ValueError):
            continue
        if available_date <= as_of:
            out.append(fact)
    return out


def trusted_facts_available_as_of(
    facts: dict,
    instrument_id: str,
    as_of: date,
) -> list[dict]:
    """Return decision-usable facts available at ``as_of``.

    A fact is decision-usable only when its content is verified, its public
    time is verified, and its ``available_from`` is ``<= as_of``.
    """
    entry = facts.get(instrument_id)
    if entry is None:
        return []
    out: list[dict] = []
    for fact in entry.get("facts", []):
        if not fact.get("content_verified"):
            continue
        if not fact.get("public_time_verified"):
            continue
        available = fact.get("available_from")
        if available is None:
            continue
        try:
            available_date = date.fromisoformat(available)
        except (TypeError, ValueError):
            continue
        if available_date <= as_of:
            out.append(fact)
    return out


def source_coverage(facts: dict, instrument_id: str) -> str:
    """Return ``"verified"`` only when at least one fact content is verified.

    Presence in the facts dictionary is not itself evidence of verification.
    """
    entry = facts.get(instrument_id)
    if entry is None:
        return "unknown"
    if any(f.get("content_verified") for f in entry.get("facts", [])):
        return "verified"
    return "unknown"
