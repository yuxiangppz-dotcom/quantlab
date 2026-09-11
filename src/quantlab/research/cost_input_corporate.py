"""Observation quality and candidate event windows; no entitlement or tax ledger."""

from __future__ import annotations

import numpy as np
import pandas as pd

DATE_FIELDS = [
    "period_end",
    "announcement_date",
    "implementation_announcement_date",
    "record_date",
    "ex_date",
    "pay_date",
    "share_listing_date",
    "available_from",
]
MEASURES = [
    "stock_dividend_per_share",
    "stock_bonus_rate",
    "stock_conversion_rate",
    "cash_dividend_after_tax",
    "cash_dividend_before_tax",
]
EVENT_DATES = ["record_date", "ex_date", "pay_date", "share_listing_date"]


def prepare_events(frame):
    """Repeated observations stay counted; conflicting versions never become certain."""
    data = frame.copy()
    for field in [
        "instrument_id",
        "source_record_id",
        "source",
        "process_status",
        "observed_at",
        *DATE_FIELDS,
        *MEASURES,
    ]:
        if field not in data:
            data[field] = None
    malformed = {}
    malformed_rows = pd.Series(False, index=data.index)
    for field in DATE_FIELDS:
        original = data[field]
        parsed = pd.to_datetime(original, errors="coerce")
        malformed[field] = int((original.notna() & parsed.isna()).sum())
        malformed_rows |= original.notna() & parsed.isna()
        data[field] = parsed
    for field in MEASURES:
        original = data[field]
        parsed = pd.to_numeric(original, errors="coerce")
        malformed[field] = int((original.notna() & ~np.isfinite(parsed)).sum())
        malformed_rows |= original.notna() & ~np.isfinite(parsed)
        data[field] = parsed

    def aware(value):
        try:
            return pd.Timestamp(value).tzinfo is not None
        except (ValueError, TypeError):
            return False

    naive_observation = ~data.observed_at.map(aware).astype(bool)
    observed = pd.to_datetime(data.observed_at, utc=True, errors="coerce")
    local_dates = observed.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    data["observed_at"] = observed
    data["observation_day"] = local_dates
    flags = {
        "malformed_values": malformed_rows,
        "observation_timezone_unknown": naive_observation,
        "missing_identity": data[["instrument_id", "source_record_id", "source"]]
        .isna()
        .any(axis=1),
        "missing_observation_or_availability": observed.isna() | data.available_from.isna(),
        "availability_before_observation": data.available_from.lt(local_dates),
        "availability_before_announcement": data.available_from.lt(data.announcement_date),
        "availability_before_implementation_notice": data.available_from.lt(
            data.implementation_announcement_date
        ),
        "record_after_ex": data.record_date.gt(data.ex_date),
        "pay_before_record": data.pay_date.lt(data.record_date),
        "negative_or_nonfinite_measure": (
            (data[MEASURES] < 0) | (data[MEASURES].notna() & ~np.isfinite(data[MEASURES]))
        ).any(axis=1),
    }
    payload = ["instrument_id", "source", "process_status", *DATE_FIELDS[:-1], *MEASURES]
    # A content-id may repeat across observations; compare mapped payload, not observation time.
    counts = data.groupby("source_record_id", dropna=False)[payload].nunique(dropna=False)
    conflict_ids = set(counts.index[counts.gt(1).any(axis=1)])
    flags["conflicting_source_identity"] = data.source_record_id.isin(conflict_ids)
    data["quality_unknown"] = pd.DataFrame(flags, index=data.index).any(axis=1)
    known_shares = data[["stock_dividend_per_share", "stock_bonus_rate", "stock_conversion_rate"]]
    share_mismatch = known_shares.notna().all(axis=1) & ~np.isclose(
        known_shares.stock_dividend_per_share,
        known_shares.stock_bonus_rate + known_shares.stock_conversion_rate,
        rtol=0,
        atol=1e-8,
    )
    flags["share_components_disagree"] = share_mismatch
    data["quality_unknown"] |= share_mismatch
    flags["positive_cash_missing_pay_date"] = (
        data.cash_dividend_before_tax.gt(0) & data.pay_date.isna()
    )
    flags["positive_shares_missing_listing_date"] = (
        data.stock_dividend_per_share.gt(0) & data.share_listing_date.isna()
    )
    flags["implemented_missing_record_or_ex"] = data.process_status.eq("实施") & data[
        ["record_date", "ex_date"]
    ].isna().any(axis=1)
    # Missing required conditional fields cannot certify a usable cash/share event.
    for name in (
        "positive_cash_missing_pay_date",
        "positive_shares_missing_listing_date",
        "implemented_missing_record_or_ex",
    ):
        data["quality_unknown"] |= flags[name]
    profiles = {
        "rows": len(data),
        "instruments": int(data.instrument_id.nunique()),
        "instrument_ids": sorted(data.instrument_id.dropna().unique().tolist()),
        "unique_source_records": int(data.source_record_id.nunique()),
        "repeated_source_observations": int(data.source_record_id.duplicated().sum()),
        "null_counts": {c: int(data[c].isna().sum()) for c in [*DATE_FIELDS, *MEASURES]},
        "malformed_counts": malformed,
        "quality_counts": {k: int(v.sum()) for k, v in flags.items()},
        "process_status_counts": data.process_status.fillna("unknown").value_counts().to_dict(),
        "date_ranges": {
            c: [str(data[c].min().date()), str(data[c].max().date())]
            if data[c].notna().any()
            else [None, None]
            for c in DATE_FIELDS
        },
        "observed_at_range": [str(observed.min()), str(observed.max())]
        if len(data)
        else [None, None],
        "missing_canonical_fields": [
            c for c in ("base_date", "base_share", "raw_payload", "request_id") if c not in frame
        ],
        "complete_event_history_certified": False,
        "cashflow_eligible": False,
    }
    for name, mask in flags.items():
        data["issue_" + name] = mask
    events = pd.concat(
        [
            data.loc[data.source_record_id.notna()]
            .sort_values("observed_at", na_position="last")
            .drop_duplicates("source_record_id"),
            data.loc[data.source_record_id.isna()],
        ]
    )
    return events, profiles


def window_events(events, instrument, signal, entry, exit_day, cutoff):
    """Inclusive overlaps flag review candidates; record-date boundaries are not fills."""
    same = events.loc[events.instrument_id.eq(instrument)]
    if same.empty or entry is None:
        return {
            "code_has_any_local_event": not same.empty,
            "candidate_event_rows": 0,
            "implemented_candidate_rows": 0,
            "candidate_rows_observed_by_signal_close": 0,
            "candidate_rows_with_quality_gaps": 0,
            "candidate_payments_after_intended_exit": 0,
            "unknown_event_history": True,
            "entitlement_amount": None,
            "dividend_tax_fen": None,
        }
    end = cutoff if exit_day is None else exit_day
    selected = same.iloc[:0]
    if entry is not None:
        mask = pd.Series(False, index=same.index)
        for field in EVENT_DATES:
            mask |= same[field].between(entry, end)
        selected = same.loc[mask]
    implemented = selected.loc[selected.process_status.eq("实施")]
    close = pd.Timestamp(signal).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
    observed = selected.observed_at.le(close) & selected.available_from.le(signal)
    observed &= ~selected[
        [
            "issue_observation_timezone_unknown",
            "issue_missing_observation_or_availability",
            "issue_availability_before_observation",
            "issue_availability_before_announcement",
            "issue_availability_before_implementation_notice",
        ]
    ].any(axis=1)
    return {
        "code_has_any_local_event": not same.empty,
        "candidate_event_rows": len(selected),
        "implemented_candidate_rows": len(implemented),
        "candidate_rows_observed_by_signal_close": int(observed.sum()),
        "candidate_rows_with_quality_gaps": int(selected.quality_unknown.sum()),
        "candidate_payments_after_intended_exit": int(
            (implemented.record_date.between(entry, end) & implemented.pay_date.gt(end)).sum()
        )
        if entry is not None
        else 0,
        "unknown_event_history": True,
        "entitlement_amount": None,
        "dividend_tax_fen": None,
    }
