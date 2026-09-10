"""Read-only economic restatements; never historical knowledge or performance claims."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from quantlab.daily.service import PROJECT_ROOT, SHANGHAI
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import DEFAULT_ACCOUNT_ROOT, load_account_basis
from quantlab.personal.tracking_core import (
    _calendar,
    _decode_journal,
    _economic_time,
    _load_journal,
    _replay_tracking,
    _reported_time,
)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def replay_account_at(
    account_id: str,
    as_of: datetime,
    *,
    opening_fingerprint: str | None = None,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Restate recorded economic facts through an inclusive cutoff using the same ledger.

    Late-reported facts are included by economic time and disclosed. The opening
    snapshot has no verified knowledge timestamp, so this API does not offer a
    historical knowledge cutoff. Legacy timing cannot be silently filtered out.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("historical cutoff must be a timezone-aware datetime")
    as_of = as_of.astimezone(SHANGHAI)
    if as_of > datetime.now(SHANGHAI):
        raise ValueError("historical cutoff cannot be in the future")
    account = load_account_basis(account_id, opening_fingerprint, account_root=account_root)
    opening_at = datetime.fromisoformat(account["as_of"])
    if opening_at.tzinfo is None or opening_at.utcoffset() is None:
        raise ValueError("opening basis must have an explicit timezone")
    opening_at = opening_at.astimezone(SHANGHAI)
    journal = _load_journal(account, account_root)
    fills, flows = _decode_journal(journal)
    if any(event.account_id != account_id for event in (*fills, *flows)):
        raise ValueError("historical journal event account binding mismatch")
    storage = storage or ParquetStorage(PROJECT_ROOT / "data/canonical")
    entries = storage.load_trading_calendar()
    result = {
        "schema": "quantlab_historical_account_v1",
        "account_id": account_id,
        "account_mode": account["account_mode"],
        "requested_effective_at": as_of.isoformat(),
        "opening_as_of": account["as_of"],
        "opening_account_fingerprint": account["account_fingerprint"],
        "view_kind": "economic_restated_not_known_at_time",
        "knowledge_cutoff_supported": False,
        "performance_eligible": False,
        "execution_ready": False,
        "status": "complete_economic_replay",
        "blocked_reasons": [],
        "cash_fen": None,
        "positions": None,
        "included_event_ids": None,
        "excluded_later_event_count": None,
        "late_reported_event_count": None,
        "latest_included_economic_at": None,
        "latest_included_reported_at": None,
        "evidence": {
            "journal_fingerprint": journal["journal_fingerprint"] if journal else None,
            "calendar_rows_sha256": _hash(
                sorted((row.exchange, row.trade_date.isoformat(), row.is_open) for row in entries)
            ),
        },
        "limitations": [
            "restates currently recorded facts, not information known at the historical time",
            "opening knowledge time and corporate-action completeness remain unverified",
            "no valuation, return calculation, broker reconciliation or order authority",
        ],
    }
    reasons = result["blocked_reasons"]
    if as_of < opening_at:
        reasons.append("cutoff_precedes_opening_basis")
    # An old fill has a known trade date but no known intraday execution time.
    if any(
        not fill.is_performance_timing_eligible and fill.trade_date <= as_of.date()
        for fill in fills
    ):
        reasons.append("legacy_fill_execution_time_unknown")
    # Even a later legacy report can describe an earlier economic cash flow.
    if any(not flow.is_performance_timing_eligible for flow in flows):
        reasons.append("legacy_cash_flow_effective_time_unknown")
    calendar = _calendar(storage, entries=entries) if entries else None
    if calendar is None:
        reasons.append("calendar_unavailable")
    elif not (
        calendar.coverage_start <= opening_at.date() <= as_of.date() <= calendar.coverage_end
    ):
        reasons.append("calendar_does_not_cover_basis_to_cutoff")
    else:
        observed_dates = {row.trade_date for row in entries}
        expected_dates = {
            opening_at.date() + timedelta(days=index)
            for index in range((as_of.date() - opening_at.date()).days + 1)
        }
        if not expected_dates.issubset(observed_dates):
            reasons.append("calendar_has_unverified_days_inside_replay_interval")
    if reasons:
        result["status"] = "blocked"
    else:
        selected_fills = [
            fill
            for fill in fills
            if fill.is_performance_timing_eligible and _economic_time(fill) <= as_of
        ]
        selected_flows = [flow for flow in flows if _economic_time(flow) <= as_of]
        selected = sorted(
            (*selected_fills, *selected_flows),
            key=lambda event: (_economic_time(event), event.event_id),
        )
        replay = _replay_tracking(account, calendar, selected_fills, selected_flows)
        instruments = sorted({lot.instrument_id for lot in replay.ledger.lots})
        result.update(
            {
                "cash_fen": replay.ledger.cash_fen,
                "positions": [
                    {
                        "instrument_id": instrument,
                        "quantity": replay.ledger.position_quantity(instrument),
                        "sellable_quantity": replay.ledger.sellable_quantity(
                            instrument, as_of.date()
                        ),
                    }
                    for instrument in instruments
                    if replay.ledger.position_quantity(instrument)
                ],
                "included_event_ids": [event.event_id for event in selected],
                "excluded_later_event_count": len(fills) + len(flows) - len(selected),
                "late_reported_event_count": sum(
                    _reported_time(event) > as_of for event in selected
                ),
                "latest_included_economic_at": replay.latest_economic_at.isoformat(),
                "latest_included_reported_at": replay.latest_reported_at.isoformat(),
            }
        )
    result["state_fingerprint"] = _hash(result)
    return result
