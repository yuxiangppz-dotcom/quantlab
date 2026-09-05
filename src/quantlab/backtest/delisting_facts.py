"""Minimal point-in-time delisting fact accessors.

Facts are stored keyed by ``instrument_id``. Each fact distinguishes the
document signature date (``document_date``) from the public disclosure date
(``publication_date``, only filled when independent disclosure evidence
exists), and carries a conservative ``available_from`` (the first session the
research system may use the fact) together with its derivation basis.
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
    """Return the facts for ``instrument_id`` available at ``as_of``.

    A fact is available only when ``available_from`` is known and ``<= as_of``.
    Facts with an unknown date never auto-derive an ``available_from`` and are
    therefore never returned.
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


def source_coverage(facts: dict, instrument_id: str) -> str:
    """Return ``"verified"`` only when at least one fact is actually verified.

    Presence in the facts dictionary is not itself evidence of verification.
    """
    entry = facts.get(instrument_id)
    if entry is None:
        return "unknown"
    statuses = {f.get("verification_status") for f in entry.get("facts", [])}
    if "verified" in statuses:
        return "verified"
    return "unknown"
