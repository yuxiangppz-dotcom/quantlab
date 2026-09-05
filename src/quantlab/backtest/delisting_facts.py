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

from quantlab.backtest.provenance import sha256_bytes


def load_delisting_facts(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _first_open_session_after(open_dates: list[date], d: date) -> date | None:
    """Return the first open session strictly after ``d``, or None if uncovered."""
    for session in open_dates:
        if session > d:
            return session
    return None


def validate_facts(facts: dict, open_dates: list[date]) -> list[str]:
    """Validate fact flags and resolve ``available_from`` from the calendar.

    Mutates ``facts`` in place: for a fact with ``public_time_verified`` true,
    ``available_from`` is set to the first open session after
    ``publication_date``. A stored ``available_from`` must match the computed
    value. Returns a list of error messages (empty if valid).
    """
    errors: list[str] = []
    sessions = sorted(set(open_dates))
    for instrument, entry in facts.items():
        for fact in entry.get("facts", []):
            key = f"{instrument}:{fact.get('fact_type')}"
            content_verified = fact.get("content_verified")
            public_time_verified = fact.get("public_time_verified")
            if not isinstance(content_verified, bool) or not isinstance(
                public_time_verified, bool
            ):
                errors.append(
                    f"{key}: content_verified/public_time_verified must be bool"
                )
                continue

            if not public_time_verified:
                fact["available_from"] = None
                fact["available_from_kind"] = None
                continue

            publication = fact.get("publication_date")
            if not publication:
                errors.append(
                    f"{key}: public_time_verified but no publication_date"
                )
                continue
            try:
                publication_date = date.fromisoformat(publication)
            except (TypeError, ValueError):
                errors.append(f"{key}: invalid publication_date {publication!r}")
                continue

            computed = _first_open_session_after(sessions, publication_date)
            if computed is None:
                errors.append(
                    f"{key}: insufficient calendar coverage after {publication}"
                )
                continue

            stored = fact.get("available_from")
            if stored is not None and stored != computed.isoformat():
                errors.append(
                    f"{key}: stored available_from {stored} != computed {computed}"
                )
            fact["available_from"] = computed.isoformat()
            fact["available_from_kind"] = "verified_next_trading_day_open"

            if computed <= publication_date:
                errors.append(
                    f"{key}: available_from {computed} not after publication_date "
                    f"{publication}"
                )

    return errors


def load_validated_facts(
    path: str | Path, open_dates: list[date]
) -> tuple[dict, str, list[str]]:
    """Read fact bytes once, hash, parse and validate from the same bytes.

    Returns ``(facts, sha256, errors)``.
    """
    data = Path(path).read_bytes()
    sha = sha256_bytes(data)
    facts = json.loads(data)
    errors = validate_facts(facts, open_dates)
    return facts, sha, errors


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
