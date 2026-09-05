"""Shadow lifecycle admission policy (no_new_exposure_after_termination_decision_v1).

This is a **shadow** decision only: it never filters targets, reallocates
weights, or changes the actual execution path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from quantlab.backtest.delisting_facts import trusted_facts_available_as_of

POLICY_NAME = "no_new_exposure_after_termination_decision"
POLICY_VERSION = "v1"
ENFORCEMENT_VERSION = "v1"
LIMITED_FACT_COVERAGE = True


@dataclass(frozen=True)
class AdmissionDecision:
    instrument_id: str
    signal_as_of: date
    status: str  # "restricted" | "unknown"
    reason: str
    policy_version: str
    fact_id: str | None
    available_from: date | None
    source: str | None


def shadow_admission(
    instrument_id: str,
    signal_as_of: date,
    trusted_facts: list[dict],
) -> AdmissionDecision:
    """Return the shadow admission decision for one positive target.

    ``trusted_facts`` must be the result of
    :func:`quantlab.backtest.delisting_facts.trusted_facts_available_as_of`
    (content-verified and public-time-verified facts available at ``as_of``).
    """
    termination = [
        f for f in trusted_facts if f.get("fact_type") == "termination_decision"
    ]
    if termination:
        fact = termination[0]
        available = fact.get("available_from")
        return AdmissionDecision(
            instrument_id=instrument_id,
            signal_as_of=signal_as_of,
            status="restricted",
            reason="trusted termination decision available at as_of",
            policy_version=f"{POLICY_NAME}_{POLICY_VERSION}",
            fact_id=fact.get("fact_id"),
            available_from=date.fromisoformat(available) if available else None,
            source=fact.get("source"),
        )
    return AdmissionDecision(
        instrument_id=instrument_id,
        signal_as_of=signal_as_of,
        status="unknown",
        reason="insufficient trusted fact coverage",
        policy_version=f"{POLICY_NAME}_{POLICY_VERSION}",
        fact_id=None,
        available_from=None,
        source=None,
    )


def compute_restricted_by_signal(
    targets: dict,
    facts: dict,
) -> dict[date, frozenset[str]]:
    """Compute the immutable restricted-instrument set per signal date.

    Only trusted facts available at ``signal_date`` are used; no execution-date
    re-check of later announcements is performed.
    """
    restricted_by_signal: dict[date, frozenset[str]] = {}
    for signal_date, target in targets.items():
        restricted: set[str] = set()
        for pos in target.positions:
            if pos.target_weight <= 0:
                continue
            trusted = trusted_facts_available_as_of(
                facts, pos.instrument_id, signal_date
            )
            decision = shadow_admission(pos.instrument_id, signal_date, trusted)
            if decision.status == "restricted":
                restricted.add(pos.instrument_id)
        if restricted:
            restricted_by_signal[signal_date] = frozenset(restricted)
    return restricted_by_signal
