"""One prospective, size-stratified value signal. No historical PIT or returns."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from numbers import Real
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quantlab.data.dividend_raw import strict_json
from quantlab.data.models import DataValidationError

CONFIG = "config/s2_prospective_valuation_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/s2_prospective_20260913"
IDENTITY = "S2-A:prospective-value-20260913-v1"
ASOF = date(2026, 9, 11)
FIELDS = ("pe_ttm", "pb", "circ_mv")
SELECTION_FINGERPRINT = "04a36754511be4136685a3ce378d60aa8c746ca218da7fa66d62df23b2e5840e"
BASE = "data/products/research_program/launch_20260912/"
RAW_BASE = BASE + "context_evidence_intake/attempts/basic_20260911/"
CALENDAR = "data/canonical/calendar/calendar.parquet"


def validate_config(c):
    fixed = {
        "identity": IDENTITY,
        "output": OUTPUT,
        "price_as_of": "2026-09-11",
        "registration_china_date": "2026-09-13",
        "earliest_entry_session": "2026-09-14",
        "exit_subsequent_sessions": 20,
        "required_consecutive_price_sessions": 21,
        "minimum_future_pairs": 30,
        "future_exit_date": None,
        "future_calendar_complete": False,
        "rule_candidates_before": 3,
        "rule_candidates_after": 4,
        "rule_candidate_cap": 15,
        "economic_paths": 0,
        "model_fits": 0,
        "provider_requests": 0,
        "historical_pit_certified": False,
        "performance_evidence": False,
        "execution_authority": False,
        "selection_fingerprint": SELECTION_FINGERPRINT,
        "raw_path": RAW_BASE + "response.body",
        "result_path": RAW_BASE + "result.json",
        "intent_path": RAW_BASE + "intent.json",
        "selection_path": BASE + "selection.json",
        "source_received_at": "2026-09-12T17:36:08.992141+00:00",
    }
    if any(c.get(k) != v or type(c.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("prospective value scope or authority changed")
    expected_inputs = {
        *(RAW_BASE + name for name in ("response.body", "result.json", "intent.json")),
        BASE + "context_evidence_intake/report.json",
        BASE + "context_evidence_intake/independent_proof.json",
        BASE + "selection.json",
        CALENDAR,
    }
    if set(c["inputs"]) != expected_inputs or c["resources"] != {
        "max_generated_bytes": 16777216,
        "reserve_host_D_bytes": 8589934592,
        "max_rss_bytes": 2147483648,
        "max_wakeup_seconds": 600,
        "next_partition_time_reserve_seconds": 30,
    }:
        raise DataValidationError("prospective input population or resource limits changed")


def validate_entry_calendar(cal):
    if not {"trade_date", "exchange", "is_open"} <= set(cal.columns):
        raise DataValidationError("entry calendar columns missing")
    entry = cal[pd.to_datetime(cal.trade_date).eq(pd.Timestamp("2026-09-14"))]
    if (
        len(entry) != 2
        or set(entry.exchange) != {"SSE", "SZSE"}
        or not entry.is_open.map(lambda value: type(value) is bool and value).all()
    ):
        raise DataValidationError("entry must be explicitly open in both saved calendars")


def parse_snapshot(raw):
    try:
        obj = strict_json(raw)
        names, items = obj["data"]["fields"], obj["data"]["items"]
        required = {"ts_code", "trade_date", *FIELDS}
        if (
            type(obj["code"]) is not int
            or obj["code"] != 0
            or not isinstance(names, list)
            or not all(type(n) is str for n in names)
            or len(names) != len(set(names))
            or not required <= set(names)
            or not isinstance(items, list)
            or not 0 < len(items) < 6000
        ):
            raise ValueError
        for part in (obj, obj["data"]):
            if (
                part.get("has_more")
                or part.get("truncated")
                or isinstance(part.get("total"), (int, float))
                and part["total"] > len(items)
            ):
                raise ValueError
        rows, seen = [], set()
        for item in items:
            if not isinstance(item, list) or len(item) != len(names):
                raise ValueError
            row = dict(zip(names, item, strict=True))
            code = row["ts_code"]
            if (
                type(code) is not str
                or re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) is None
                or code in seen
                or row["trade_date"] != "20260911"
            ):
                raise ValueError
            seen.add(code)
            for field in FIELDS:
                value = row[field]
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                ):
                    raise ValueError
            rows.append({"instrument_id": code, **{k: row[k] for k in FIELDS}})
        return pd.DataFrame(rows)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DataValidationError("invalid fixed-date raw valuation snapshot") from exc


def positive_finite(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def validate_registration(created_at, received_at):
    for value in (created_at, received_at):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise DataValidationError("prospective timing requires aware actual timestamps")
    local = created_at.astimezone(ZoneInfo("Asia/Shanghai"))
    if (
        local.date() != date(2026, 9, 13)
        or received_at > created_at
        or received_at.astimezone(ZoneInfo("Asia/Shanghai")).date() < ASOF
    ):
        raise DataValidationError("registration cannot precede receipt or be backdated")


def valuation_scores(frame, codes):
    if (
        type(codes) is not tuple
        or len(codes) != 256
        or len(set(codes)) != 256
        or any(type(c) is not str or not c for c in codes)
    ):
        raise DataValidationError("retain the ordered fixed256 cohort")
    required = ["instrument_id", *FIELDS]
    if not set(required) <= set(frame.columns):
        raise DataValidationError("missing valuation source columns")
    if frame.instrument_id.isna().any() or frame.instrument_id.duplicated().any():
        raise DataValidationError("unknown or duplicate source identity")
    f = frame.set_index("instrument_id").reindex(codes)[list(FIELDS)].copy()
    valid = {name: f[name].map(positive_finite) for name in FIELDS}
    f["size_group"] = pd.Series(pd.NA, index=f.index, dtype="Int64")
    n = int(valid["circ_mv"].sum())
    if n >= 20:
        size = f.loc[valid["circ_mv"], "circ_mv"].rank(method="average")
        f.loc[valid["circ_mv"], "size_group"] = np.floor((size - 1) * 5 / n).clip(0, 4).astype(int)
    f["score"] = np.nan
    f["reason"] = "valuation_unknown_or_nonpositive"
    f.loc[~valid["circ_mv"], "reason"] = "size_unknown_or_nonpositive"
    if n < 20:
        f.loc[valid["circ_mv"], "reason"] = "fewer_than20_size_observations"
    else:
        eligible = valid["pe_ttm"] & valid["pb"] & valid["circ_mv"]
        for group in range(5):
            mask = eligible & f.size_group.eq(group).fillna(False)
            count = int(mask.sum())
            if count < 20:
                f.loc[mask, "reason"] = "fewer_than20_valid_values_in_size_group"
                continue
            pe = (-f.loc[mask, "pe_ttm"]).rank(method="average")
            pb = (-f.loc[mask, "pb"]).rank(method="average")
            f.loc[mask, "score"] = (pe + pb) / (2 * count)
            f.loc[mask, "reason"] = "observed_value_score"
    f["score_known"] = f.score.notna()
    return f.rename_axis("instrument_id").reset_index()
