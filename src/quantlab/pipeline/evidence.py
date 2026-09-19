"""Evidence builders for the historical CSI800 main line.

Every builder reads archived raw observations or sealed canonical state and
writes one versioned evidence artifact plus a source receipt. Builders never
backdate observations: ``known_at`` values come from the documented publication
semantics of each source (scheduled effective dates, month-end index weight
publications, regulation announcement dates), never from the later time a file
was downloaded. Where a historical publication time cannot be established, the
artifact records the limitation instead of inventing completeness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pandas as pd

MEMBERSHIP_SCHEMA = "quantlab_index_membership_v1"
EVENT_COVERAGE_SCHEMA = "quantlab_event_coverage_v1"
EXECUTION_POLICY_SCHEMA = "quantlab_execution_evidence_v1"
INDEX = "000906.SH"
SNAPSHOT_SIZE = 800


def second_friday(year: int, month: int) -> date:
    """The published CSI review rule: the second Friday of June and December."""
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 7)


def scheduled_effective(calendar: list[date], year: int, month: int) -> date | None:
    """First session after the second-Friday close, from the stored calendar."""
    if month not in (6, 12):
        return None
    cutoff = second_friday(year, month)
    after = [d for d in calendar if d > cutoff and (d.year, d.month) == (year, month)]
    return after[0] if after else None


@dataclass(frozen=True)
class MembershipChange:
    previous_date: date
    observed_date: date
    effective: date
    dating_basis: str
    added: tuple[str, ...]
    removed: tuple[str, ...]


def membership_changes(
    observations: list[tuple[date, frozenset[str]]],
    calendar: list[date],
) -> tuple[list[tuple[date, date, str, frozenset[str]]], list[MembershipChange]]:
    """Summarize observations, never infer effective membership from month ends.

    Even in June/December a change may include extraordinary adjustments.
    Neither a calendar rule nor an unchanged pair proves intervening identity.
    The intervals below describe observations only and cannot enter a PIT universe.
    """
    if not observations:
        raise ValueError("no index observations")
    observations = sorted(observations, key=lambda item: item[0])
    if len({d for d, _ in observations}) != len(observations):
        raise ValueError("duplicate membership observation date")
    intervals: list[tuple[date, date, str, frozenset[str]]] = []
    changes: list[MembershipChange] = []
    current_start, current_members, current_basis = observations[0][0], observations[0][1], (
        "first_observation"
    )
    previous_date = observations[0][0]
    for observed_date, members in observations[1:]:
        if members == current_members:
            previous_date = observed_date
            continue
        effective, basis = observed_date, "observation_bounded"
        intervals.append(
            (current_start, effective - timedelta(days=1), current_basis, current_members)
        )
        changes.append(
            MembershipChange(
                previous_date=previous_date,
                observed_date=observed_date,
                effective=effective,
                dating_basis=basis,
                added=tuple(sorted(members - current_members)),
                removed=tuple(sorted(current_members - members)),
            )
        )
        current_start, current_members, current_basis = effective, members, basis
        previous_date = observed_date
    intervals.append((current_start, observations[-1][0], current_basis, current_members))
    return intervals, changes


def membership_document(
    intervals: list[tuple[date, date, str, frozenset[str]]],
    *,
    observation_dates: list[date],
    revision_id: str,
) -> dict:
    """Schema-conformant membership evidence; every interval keeps 800 members."""
    snapshots = []
    for start, end, basis, members in intervals:
        if len(members) != SNAPSHOT_SIZE or len(set(members)) != SNAPSHOT_SIZE:
            raise ValueError(
                f"CSI800 interval requires exactly {SNAPSHOT_SIZE} distinct members:{start}"
            )
        snapshots.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat() if end != date.max else "9999-12-31",
                "known_at": None,
                "source_id": "csi_index_weight_monthly_observation",
                "revision_id": revision_id,
                "complete": False,
                "historical_publication_certified": False,
                "members": sorted(members),
                "dating_basis": basis,
            }
        )
    ordered = sorted({d for d in observation_dates})
    covered = [
        d
        for d in ordered
        if any(
            pd.Timestamp(s["start"]).date() <= d <= pd.Timestamp(s["end"]).date()
            for s in snapshots
        )
    ]
    if covered != ordered:
        raise ValueError("membership intervals must confirm every observation snapshot")
    return {
        "schema": MEMBERSHIP_SCHEMA,
        "index": INDEX,
        "semantics": "monthly_observations_not_effective_membership",
        "snapshots": snapshots,
        "known_limitations": [
            "Monthly observations do not establish effective or publication dates. "
            "Official constituent identities and announcement/effective dates are required "
            "for each regular and extraordinary adjustment before PIT admission.",
            "Interval membership before the first monthly observation is unknown "
            "and intentionally not reconstructed.",
        ],
    }


# ---------------------------------------------------------------------------
# Source availability: a declared same-day publication bound per sealed session.


def availability_frame(
    receipts_sessions: list[tuple[date, str]],
    *,
    publication_hour: int = 17,
) -> pd.DataFrame:
    """One row per sealed session with a declared post-close publication time.

    Daily exchange OHLCV/basic data is published after the 15:00 close; the
    declared bound is 17:00 Asia/Shanghai on the session itself. The actual
    local download time stays in each ingestion receipt and is used only as the
    forward lower bound, never as historical publication evidence.
    """
    rows = []
    for session, revision_id in sorted(receipts_sessions):
        known = pd.Timestamp(session).tz_localize("Asia/Shanghai") + pd.Timedelta(
            hours=publication_hour
        )
        rows.append(
            {
                "trade_date": pd.Timestamp(session),
                "known_at": known,
                "source_id": "declared_same_day_close_publication_v1",
                "revision_id": revision_id,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("no sealed sessions for availability evidence")
    if frame.trade_date.duplicated().any():
        raise ValueError("duplicate availability sessions")
    return frame


# ---------------------------------------------------------------------------
# Dated fee/quantity execution policies.


@dataclass(frozen=True)
class FeeEra:
    start: date
    end: date
    known_at: date
    sell_stamp_rate: str
    additional_fee_rate: str
    transfer_fee_source: str
    stamp_source: str


def fee_eras(start: date, end: date) -> list[FeeEra]:
    """Sourced stamp-tax and transfer-fee eras inside the research interval.

    - Transfer fee (China Securities Depository and Clearing): 0.02%o of value
      both sides from 2015-08-01 (announced 2015-07-10), halved to 0.01%o from
      2022-04-29 (announced 2022-04-28).
    - Stamp tax (Ministry of Finance / STA): 0.1% sell side since 2008-09-19,
      halved to 0.05% from 2023-08-28 (announcement 2023-08-27, No.39/2023).
    """
    eras = [
        FeeEra(
            start=date(2017, 1, 1),
            end=date(2022, 4, 28),
            known_at=date(2015, 7, 10),
            sell_stamp_rate="0.001",
            additional_fee_rate="0.00002",
            transfer_fee_source="chinaclear_transfer_fee_2015-08-01_0.02permille",
            stamp_source="stamp_tax_sell_0.001_since_2008-09-19",
        ),
        FeeEra(
            start=date(2022, 4, 29),
            end=date(2023, 8, 27),
            known_at=date(2022, 4, 28),
            sell_stamp_rate="0.001",
            additional_fee_rate="0.00001",
            transfer_fee_source="chinaclear_transfer_fee_2022-04-29_0.01permille",
            stamp_source="stamp_tax_sell_0.001_since_2008-09-19",
        ),
        FeeEra(
            start=date(2023, 8, 28),
            end=date(9999, 12, 31),
            known_at=date(2023, 8, 27),
            sell_stamp_rate="0.0005",
            additional_fee_rate="0.00001",
            transfer_fee_source="chinaclear_transfer_fee_2022-04-29_0.01permille",
            stamp_source="moft_sta_announcement_2023_no39_halved_2023-08-28",
        ),
    ]
    return [
        FeeEra(
            start=max(e.start, start),
            end=e.end,
            known_at=e.known_at,
            sell_stamp_rate=e.sell_stamp_rate,
            additional_fee_rate=e.additional_fee_rate,
            transfer_fee_source=e.transfer_fee_source,
            stamp_source=e.stamp_source,
        )
        for e in eras
        if e.start <= end and e.end >= start
    ]


def board_group(instrument_id: str) -> str:
    """Prefix-based board grouping; BJ codes never enter the CSI800 universe."""
    code = instrument_id.split(".")[0]
    if code.startswith("688") or code.startswith("689"):
        return "star_sh"
    if code.startswith(("300", "301", "302")):
        return "chinext_sz"
    if instrument_id.endswith(".SH"):
        return "main_sh"
    if instrument_id.endswith(".SZ"):
        return "main_sz"
    raise ValueError(f"unsupported board for CSI800 instrument:{instrument_id}")


RULES_BY_GROUP = {
    "main_sh": {
        "buy_minimum": 100,
        "buy_increment": 100,
        "sell_minimum": 1,
        "sell_increment": 1,
        "max_order_quantity": 1000000,
        "full_position_odd_exit": True,
        "source": "sse_trading_rules_lot_100_sell_odd_allowed",
    },
    "main_sz": {
        "buy_minimum": 100,
        "buy_increment": 100,
        "sell_minimum": 1,
        "sell_increment": 1,
        "max_order_quantity": 1000000,
        "full_position_odd_exit": True,
        "source": "szse_trading_rules_lot_100_sell_odd_allowed",
    },
    "chinext_sz": {
        "buy_minimum": 100,
        "buy_increment": 100,
        "sell_minimum": 1,
        "sell_increment": 1,
        "max_order_quantity": 100000,
        "full_position_odd_exit": True,
        "source": "szse_chinext_limit_order_cap_100k_shares_conservative",
    },
    "star_sh": {
        "buy_minimum": 200,
        "buy_increment": 1,
        "sell_minimum": 1,
        "sell_increment": 1,
        "max_order_quantity": 100000,
        "full_position_odd_exit": True,
        "source": "sse_star_special_provisions_200_min_1_increment_100k_cap",
    },
}


def execution_policy_document(
    instruments: list[str],
    eras: list[FeeEra],
    *,
    start: date,
    end: date,
    commission_rate: str = "0.000086",
    minimum_commission_fen: int = 500,
    participation: str = "0.1",
    stale_valuation: bool = True,
) -> dict:
    """Expand dated fee eras over board groups; one policy covers each day.

    The commission is a USER-DECLARED schedule (万0.86 = 0.0086% = 0.000086,
    all-in assumed including exchange/regulatory surcharges, stock minimum
    CNY 5; declared 2026-09-18; ETF minimum CNY 0.1 is recorded but this
    strategy trades only CSI800 stocks). It is NOT verified against a
    brokerage fee table, and applying a 2026 declaration to a historical
    replay makes it a DECLARED HISTORICAL FEE SCENARIO, not the actually
    applicable historical rate. Stamps and the transfer fee are separate
    sourced rates; if 万0.86 excludes regulatory surcharges the modeled cost
    understates by about 0.0054% per side.
    """
    policies = []
    grouped: dict[str, list[str]] = {}
    for code in instruments:
        grouped.setdefault(board_group(code), []).append(code)
    for era in eras:
        era_start, era_end = max(era.start, start), min(era.end, end)
        if era_start > era_end:
            continue
        for group, codes in sorted(grouped.items()):
            rules = RULES_BY_GROUP[group]
            policies.append(
                {
                    "instruments": sorted(codes),
                    "start": era_start.isoformat(),
                    "end": era_end.isoformat(),
                    "known_at": f"{era.known_at.isoformat()}T00:00:00+08:00",
                    "source_id": (
                        f"user_declared_fee_scenario_2026-09-18_v1|{era.transfer_fee_source}|{era.stamp_source}"
                    ),
                    "participation": participation,
                    "rules": {
                        "scenario_id": f"{group}_rules_v1",
                        "effective_from": era_start.isoformat(),
                        "effective_through": era_end.isoformat(),
                        **{k: v for k, v in rules.items() if k != "source"},
                        # scenario_id already encodes the rule source; extra keys
                        # would break the typed kernel decode.
                    },
                    "fees": {
                        "scenario_id": "user_declared_wan0p86_stock_min5_v1",
                        "effective_from": era_start.isoformat(),
                        "effective_through": era_end.isoformat(),
                        "commission_rate": commission_rate,
                        "minimum_commission_fen": minimum_commission_fen,
                        "buy_stamp_rate": "0",
                        "sell_stamp_rate": era.sell_stamp_rate,
                        "additional_fee_rate": era.additional_fee_rate,
                        "additional_fee_fixed_fen": 0,
                        "adverse_slippage_rate": "0",
                    },
                }
            )
    document = {
        "schema": EXECUTION_POLICY_SCHEMA,
        "policies": policies,
        "known_limitations": [
            "commission 0.0086% (万0.86 = 0.000086, stock minimum CNY 5) is a "
            "USER-DECLARED schedule, not verified against a brokerage fee "
            "table; used in a historical replay it is a declared historical "
            "fee scenario, not the actually applicable historical rate. The "
            "all-in-of-surcharges assumption is unconfirmed (if excluded, "
            "costs understate ~0.0054% per side). ETF minimum CNY 0.1 is "
            "recorded but this strategy trades only CSI800 stocks.",
            "adverse_slippage_rate is zero at base; stress scenarios replace it "
            "with explicit per-side slippage assumptions.",
            "participation 0.1 is a conservative research cap, not a measured "
            "market-impact calibration.",
        ],
    }
    if stale_valuation:
        # Declared research valuation convention, frozen with the strategy:
        # a CONFIRMED full-session halt with no original price may carry the
        # most recent raw close for at most 20 sessions; every bridged day
        # requires halt evidence, and no bridge crosses a corporate event.
        document["stale_valuation"] = {
            "mode": "last_raw_close_known_halt",
            "max_sessions": 20,
            "source_id": "declared_research_valuation_policy_v1",
            "known_at": f"{date(2018, 1, 1).isoformat()}T00:00:00+08:00",
        }
        document["known_limitations"].append(
            "stale_valuation bridges confirmed halts for at most 20 sessions "
            "at the last raw close; longer suspensions block the account "
            "explicitly, and delisting settlements remain unsupported."
        )
    return document


# ---------------------------------------------------------------------------
# Industry intervals from vendor SW membership compilations.


def _resolve_industry_overlaps(rows: list[dict], *, end: date) -> tuple[list[dict], list[str]]:
    """Split at every boundary; conflicting evidence occupies an UNKNOWN span.

    Unknown spans remain primary coverage, so a fallback cannot erase them.
    This handles nested and open-ended stints, not just adjacent pairs.
    """
    by_code: dict[str, list[dict]] = {}
    for row in rows:
        start = date.fromisoformat(row["start"])
        stop = min(date.fromisoformat(row["end"]), end)
        if start <= stop:
            by_code.setdefault(row["instrument_id"], []).append(
                {**row, "end": stop.isoformat()}
            )
    result, issues = [], []
    for code, spans in sorted(by_code.items()):
        points = sorted({
            day
            for row in spans
            for day in (date.fromisoformat(row["start"]),
                        date.fromisoformat(row["end"]) + timedelta(days=1))
        })
        for start, right in zip(points, points[1:], strict=False):
            stop = right - timedelta(days=1)
            active = [r for r in spans if r["start"] <= str(start) <= r["end"]]
            if not active:
                continue
            # Stable selection among compatible records, using the latest
            # declared availability; this does not certify historical publication.
            base = max(active, key=lambda r: (r.get("known_at", ""), json.dumps(r, sort_keys=True)))
            row = {**base, "start": str(start), "end": str(stop)}
            labels = {r.get("industry") for r in active}
            if len(labels) != 1 or None in labels or "" in labels:
                row.update(industry=None, unknown_reason="conflicting_or_unknown_industry")
                row["conflicting_evidence"] = sorted(
                    active, key=lambda r: json.dumps(r, sort_keys=True)
                )
                issues.append(f"{code}:conflict_overlap_or_unknown:{start}:{stop}")
            result.append(row)
    return result, issues


def industry_intervals(
    rows: pd.DataFrame,
    codes: set[str],
    *,
    end: date,
    taxonomy: str,
) -> tuple[list[dict], list[str]]:
    """Apply the declared [in_date, out_date) convention, without resolving conflicts.

    The vendor's boundary/publication semantics are not independently certified.
    Recorded exits leave gaps; disagreeing active stints produce explicit UNKNOWN
    intervals. A missing exit is open-ended, not an inferred exit at the next row.
    """
    intervals, issues = [], []
    for raw in rows.to_dict("records"):
        code = str(raw["con_code"]).strip()
        if code not in codes:
            continue
        start_ts = pd.to_datetime(raw["in_date"], format="%Y%m%d", errors="coerce")
        out_raw = raw["out_date"]
        missing_out = pd.isna(out_raw) or str(out_raw).strip() == ""
        out_ts = pd.to_datetime(out_raw, format="%Y%m%d", errors="coerce")
        if pd.isna(start_ts) or (not missing_out and pd.isna(out_ts)):
            raise ValueError(f"{code}:invalid industry boundary")
        start = start_ts.date()
        if missing_out and start > end:
            continue
        stop = end if missing_out else out_ts.date() - timedelta(days=1)
        if not missing_out and out_ts.date() == start:
            issues.append(f"{code}:{taxonomy} empty_half_open_interval:{start}")
            continue
        if stop < start:
            raise ValueError(f"{code}:reversed industry interval:{start}")
        label = raw["l1_name"]
        label = (None if pd.isna(label) or not str(label).strip()
                 else f"{taxonomy}:{str(label).strip()}")
        intervals.append({
            "instrument_id": code, "start": str(start), "end": str(stop),
            "known_at": f"{start}T00:00:00+08:00",
            "source_id": f"tushare_sw_member_{taxonomy}_compilation",
            "revision_id": taxonomy, "industry": label,
        })
    resolved, conflicts = _resolve_industry_overlaps(intervals, end=end)
    issues.extend(conflicts)
    for code in sorted(codes):
        spans = [r for r in resolved if r["instrument_id"] == code]
        if not spans:
            issues.append(f"{code}:no {taxonomy} membership row")
        for left, right in zip(spans, spans[1:], strict=False):
            gap = date.fromisoformat(left["end"]) + timedelta(days=1)
            if str(gap) < right["start"]:
                stop = date.fromisoformat(right["start"]) - timedelta(days=1)
                issues.append(f"{code}:{taxonomy} vacancy_unknown:{gap}:{stop}")
    return resolved, issues


def merge_industry_sources(
    primary: list[dict], fallback: list[dict], *, end: date,
) -> tuple[list[dict], list[str]]:
    """Fallback fills uncovered spans, never primary UNKNOWN/conflicting spans."""
    primary, issues = _resolve_industry_overlaps(primary, end=end)
    fallback, fallback_issues = _resolve_industry_overlaps(fallback, end=end)
    issues.extend(fallback_issues)
    merged = list(primary)
    by_code: dict[str, list[dict]] = {}
    for row in primary:
        by_code.setdefault(row["instrument_id"], []).append(row)
    for fill in fallback:
        pieces = [fill]
        for row in by_code.get(fill["instrument_id"], []):
            remaining = []
            for piece in pieces:
                if row["end"] < piece["start"] or row["start"] > piece["end"]:
                    remaining.append(piece)
                    continue
                if piece["start"] < row["start"]:
                    boundary = date.fromisoformat(row["start"]) - timedelta(days=1)
                    remaining.append({**piece, "end": str(boundary)})
                if piece["end"] > row["end"]:
                    boundary = date.fromisoformat(row["end"]) + timedelta(days=1)
                    remaining.append({**piece, "start": str(boundary)})
            pieces = remaining
        merged.extend(pieces)
    return sorted(merged, key=lambda r: (r["instrument_id"], r["start"])), issues


def coverage_intervals(
    sessions: list[date],
    *,
    chunk_years: int = 2,
    source_id: str,
    revision_id: str,
    complete: bool,
) -> list[dict]:
    """Contiguous calendar-day coverage, without bridging unobserved sessions.

    ``complete`` must reflect an independent verification result, not a wish:
    an empty ST response is never proof that no stock was designated ST.
    """
    if not sessions:
        raise ValueError("no sealed sessions for event coverage")
    sessions = sorted(sessions)
    bounds = []
    run_start = previous = sessions[0]
    for day in sessions[1:]:
        if (day - previous).days != 1:
            bounds.append((run_start, previous))
            run_start = day
        previous = day
    bounds.append((run_start, previous))
    coverage = []
    for start, stop in bounds:
        known = start
        coverage.append(
            {
                "start": start.isoformat(),
                "end": stop.isoformat(),
                "known_at": f"{known.isoformat()}T00:00:00+08:00",
                "source_id": source_id,
                "revision_id": revision_id,
                "complete": complete,
            }
        )
    return coverage


def _exact_fraction(ratio: Decimal) -> tuple[int, int]:
    value = Fraction(ratio)
    return value.numerator, value.denominator


def corporate_events(
    rows: pd.DataFrame,
    instrument_id: str,
    *,
    start: date,
    end: date,
    source_id: str,
) -> tuple[list[dict], list[str], list[dict]]:
    """Supported corporate events from one instrument's dividend observations.

    Vendor field semantics (TuShare dividend documentation): ``cash_div`` is
    the per-share AFTER-TAX amount the holder receives and ``cash_div_tax``
    is the pre-tax equivalent; the net rate therefore comes from
    ``cash_div``. The cash leg and the share leg are judged independently:
    a row can yield two events, one event plus one unresolved record, or
    unresolved records only. Unresolved records carry every locatable
    identifier (``ex_date``/``record_date`` may be None when the vendor row
    itself lacks them) so downstream blocking can work per-day when the date
    is known and per-instrument when it is not. Exact duplicates are
    dropped; conflicting duplicates invalidate all candidate legs in their
    distribution group and become unresolved. Rights issues,
    merger consideration, delisting settlements and the holding-period
    differential dividend tax charged at disposal are not implemented.
    """
    events: list[dict] = []
    skipped: list[str] = []
    unresolved: list[dict] = []
    seen: dict[tuple, str] = {}

    def _unresolved(kind, ex_iso, record_iso, reason):
        unresolved.append(
            {
                "instrument_id": instrument_id,
                "ex_date": ex_iso,
                "record_date": record_iso,
                "kind": kind,
                "reason": reason,
            }
        )

    for row in rows.to_dict("records"):
        ex = row.get("ex_date")
        record = row.get("record_date")
        pay = row.get("pay_date")
        listing = row.get("div_listdate")
        cash = row.get("cash_div")
        cash_pre = row.get("cash_div_tax")
        stk = row.get("stk_div")
        share_ratio = Decimal(str(stk)) if pd.notna(stk) else Decimal(0)
        ex_iso = None if pd.isna(ex) else str(ex)
        record_iso = None if pd.isna(record) else str(record)
        ex_date = record_date = None
        if pd.isna(ex):
            _unresolved("unknown", None, record_iso, "missing_ex_date")
            skipped.append(f"{instrument_id}:missing ex_date:{ex}")
            continue
        try:
            ex_date = pd.Timestamp(ex).date()
            ex_iso = ex_date.isoformat()
        except Exception:
            _unresolved("unknown", None, record_iso, "invalid_ex_date")
            skipped.append(f"{instrument_id}:invalid ex_date:{ex}")
            continue
        if not (start <= ex_date <= end):
            continue
        if pd.isna(record):
            _unresolved("unknown", ex_iso, None, "missing_record_date")
            skipped.append(f"{instrument_id}:missing record date:{ex_date}")
            continue
        try:
            record_date = pd.Timestamp(record).date()
            record_iso = record_date.isoformat()
        except Exception:
            _unresolved("unknown", ex_iso, None, "invalid_record_date")
            skipped.append(f"{instrument_id}:bad record date:{record}")
            continue
        key = (ex_date, record_date)
        fingerprint = json.dumps(
            [str(cash), str(cash_pre), str(stk), str(pay), str(listing)],
            sort_keys=True,
        )
        if key in seen:
            if seen[key] == fingerprint:
                skipped.append(f"{instrument_id}:exact duplicate ex_date {ex_date}")
            else:
                # A later conflicting row invalidates the entire distribution,
                # including any cash/share legs emitted from the first row.
                # Keeping that first candidate would make artifact consumers
                # depend on vendor row order despite unresolved coverage.
                events[:] = [
                    event for event in events
                    if (event["ex_date"], event["record_date"]) != (ex_iso, record_iso)
                ]
                _unresolved("conflicting", ex_iso, record_iso, "conflicting_duplicate")
                skipped.append(f"{instrument_id}:conflicting duplicate ex_date {ex_date}")
            continue
        seen[key] = fingerprint
        if record_date >= ex_date:
            _unresolved("unknown", ex_iso, record_iso, "invalid_record_ex_chronology")
            skipped.append(f"{instrument_id}:invalid record/ex chronology:{ex_date}")
            continue
        net = None
        if pd.notna(cash):
            net = Decimal(str(cash))
        elif pd.notna(cash_pre):
            _unresolved("cash_dividend", ex_iso, record_iso, "after_tax_cash_missing_pretax_only")
            skipped.append(
                f"{instrument_id}:after-tax cash_div missing; the pre-tax "
                f"cash_div_tax value is not used as net:{ex_date}"
            )
        if net is not None and net > 0:
            if pd.isna(pay):
                _unresolved("cash_dividend", ex_iso, record_iso, "missing_pay_date")
                skipped.append(f"{instrument_id}:cash event without pay_date:{ex_date}")
            else:
                try:
                    cash_settlement = pd.Timestamp(pay).date()
                except Exception:
                    cash_settlement = None
                if cash_settlement is None:
                    _unresolved("cash_dividend", ex_iso, record_iso, "invalid_pay_date")
                    skipped.append(f"{instrument_id}:bad pay_date:{pay}")
                elif cash_settlement < ex_date:
                    _unresolved("cash_dividend", ex_iso, record_iso, "pay_before_ex")
                    skipped.append(f"{instrument_id}:cash settlement before ex:{ex_date}")
                else:
                    events.append(
                        {
                            "event_id": f"div:{instrument_id}:{ex_date.isoformat()}",
                            "instrument_id": instrument_id,
                            "kind": "cash_dividend",
                            "record_date": record_date.isoformat(),
                            "ex_date": ex_date.isoformat(),
                            "settlement_date": cash_settlement.isoformat(),
                            "source_id": source_id,
                            "net_cash_per_share_fen": str(net * 100),
                        }
                    )
        if share_ratio > 0:
            numerator, denominator = _exact_fraction(share_ratio)
            if pd.isna(listing):
                _unresolved("bonus_shares", ex_iso, record_iso, "missing_div_listdate")
                skipped.append(
                    f"{instrument_id}:share event without div_listdate:{ex_date}"
                )
                continue
            try:
                listing_date = pd.Timestamp(listing).date()
            except Exception:
                _unresolved("bonus_shares", ex_iso, record_iso, "invalid_div_listdate")
                skipped.append(f"{instrument_id}:bad div_listdate:{listing}")
                continue
            if listing_date < ex_date:
                _unresolved("bonus_shares", ex_iso, record_iso, "listing_before_ex")
                skipped.append(f"{instrument_id}:share listing before ex:{ex_date}")
                continue
            events.append(
                {
                    "event_id": f"shr:{instrument_id}:{ex_date.isoformat()}",
                    "instrument_id": instrument_id,
                    "kind": "bonus_shares",
                    "record_date": record_date.isoformat(),
                    "ex_date": ex_date.isoformat(),
                    "settlement_date": listing_date.isoformat(),
                    "source_id": source_id,
                    "share_numerator": numerator,
                    "share_denominator": denominator,
                }
            )
        if (net is None or net <= 0) and share_ratio <= 0:
            skipped.append(f"{instrument_id}:no supported distribution:{ex_date}")
    return events, skipped, unresolved


def corporate_coverage(
    instrument_ids: list[str],
    *,
    start: date,
    end: date,
    source_id: str,
    unresolved: list[dict] | None = None,
    unresolved_instruments: list[dict] | None = None,
) -> dict:
    return {
        "source_id": source_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "instruments": len(set(instrument_ids)),
        # Canonical location of detected-but-unusable distributions and
        # instruments whose dividend data could not be obtained at all.
        # market_day rejects sessions/instruments listed here; a missing
        # section is rejected as an unknown-coverage document.
        "unresolved": list(unresolved or []),
        "unresolved_instruments": list(unresolved_instruments or []),
        "semantics": (
            "cash dividends carry the vendor AFTER-TAX per-share amount "
            "(cash_div; cash_div_tax is the pre-tax equivalent and is not used "
            "as the net rate); bonus and conversion share events use the "
            "integer distribution ratio and settle on the vendor listing date "
            "(div_listdate); fractional entitlements follow a DECLARED "
            "holder-level truncation scenario, not a reproduced depository "
            "allocation. The holding-period differential dividend tax charged "
            "at disposal is unimplemented and unquantified. Rights issues, "
            "merger consideration and delisting settlements are unsupported "
            "kinds and must surface as explicit blocks."
        ),
        "unsupported_kinds_watchlist": ["rights_issue", "merger_exchange", "delisting_settlement"],
    }
