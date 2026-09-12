"""Lossless retrospective observations and candidate overlaps, never account postings."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime

from quantlab.data.dividend_raw import DATES, FIELDS, MEASURES, strict_json
from quantlab.data.models import DataValidationError
from quantlab.data.models import canonical_payload_fingerprint as fingerprint

EVENT_DATES = ("record_date", "ex_date", "pay_date", "div_listdate")
KNOWN_STATUSES = {
    "实施",
    "预案",
    "股东大会通过",
    "股东大会未通过",
    "未通过",
    "停止实施",
    "股东提议",
    "预披露",
    "其他",
}


def parse_date(value):
    if value is None or value == "":
        return None, False
    try:
        if (
            not isinstance(value, str)
            or len(value) != 8
            or not value.isascii()
            or not value.isdigit()
        ):
            raise ValueError
        return datetime.strptime(value, "%Y%m%d").date().isoformat(), False
    except ValueError:
        return None, True


def number(value):
    if value is None or value == "":
        return None, False
    if type(value) not in (float, int):
        return None, True
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    return (float(value), False) if valid else (None, True)


def observations(raw, receipt, code):
    """One identity per response-row occurrence; exact duplicate contents stay separate."""
    if hashlib.sha256(raw).hexdigest() != receipt["wire_sha256"]:
        raise DataValidationError("dividend response bytes changed")
    body = strict_json(raw)
    if (
        type(body.get("code")) is not int
        or body.get("code") != 0
        or receipt.get("status") != "nonempty"
    ):
        raise DataValidationError("event adapter requires a verified nonempty response")
    fields, values = body["data"]["fields"], body["data"]["items"]
    if len(set(fields)) != len(fields) or not set(FIELDS) <= set(fields):
        raise DataValidationError("dividend observation schema changed")
    stamp = datetime.fromisoformat(receipt["observed_at"])
    if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
        raise DataValidationError("unknown dividend observation timezone")
    result = []
    for index, values_row in enumerate(values):
        if len(values_row) != len(fields):
            raise DataValidationError("dividend row length changed")
        row = dict(zip(fields, values_row, strict=True))
        if row["ts_code"] != code:
            raise DataValidationError("dividend row belongs to another code")
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
        mapped = {
            "instrument_id": code,
            "row_index": index,
            "response_fingerprint": receipt["fingerprint"],
            "response_sha256": receipt["wire_sha256"],
            "request_fingerprint": receipt["intent_fingerprint"],
            "observation_id": fingerprint([receipt["fingerprint"], index]),
            "content_id": fingerprint(row),
            "observed_at": stamp.isoformat(),
            "raw_payload": payload,
            "raw_status": row["div_proc"] if isinstance(row["div_proc"], str) else None,
            "historical_pit_certified": False,
            "cashflow_eligible": False,
        }
        issues = []
        for key in DATES:
            mapped[key], bad = parse_date(row[key])
            if bad:
                issues.append("malformed_" + key)
        for key in MEASURES:
            mapped[key], bad = number(row[key])
            if bad:
                issues.append("malformed_" + key)
        status = row["div_proc"].strip() if isinstance(row["div_proc"], str) else None
        mapped["normalized_status"] = status
        mapped["status_normalized"] = status != row["div_proc"]
        mapped["implementation_candidate"] = status == "实施"
        if mapped["status_normalized"]:
            issues.append("status_normalized")
        if status not in KNOWN_STATUSES:
            issues.append("unknown_status")
        for left, right, label in (
            ("record_date", "ex_date", "record_after_ex"),
            ("record_date", "pay_date", "pay_before_record"),
            ("record_date", "div_listdate", "listing_before_record"),
        ):
            if mapped[left] and mapped[right] and mapped[left] > mapped[right]:
                issues.append(label)
        shares = [mapped[key] for key in ("stk_div", "stk_bo_rate", "stk_co_rate")]
        if any(value is None for value in shares):
            issues.append("share_components_unknown")
        elif not math.isclose(shares[0], shares[1] + shares[2], rel_tol=0, abs_tol=1e-8):
            issues.append("share_components_disagree")
        if mapped["cash_div_tax"] is None:
            issues.append("cash_before_tax_unknown")
        if mapped["base_date"] is None or mapped["base_share"] is None:
            issues.append("base_shares_unknown")
        if mapped["implementation_candidate"]:
            if not mapped["record_date"] or not mapped["ex_date"]:
                issues.append("implemented_record_or_ex_unknown")
            if (
                mapped["cash_div_tax"] is not None
                and mapped["cash_div_tax"] > 0
                and not mapped["pay_date"]
            ):
                issues.append("positive_cash_pay_date_unknown")
            if (
                mapped["stk_div"] is not None
                and mapped["stk_div"] > 0
                and not mapped["div_listdate"]
            ):
                issues.append("positive_shares_listing_date_unknown")
        mapped["has_event_date"] = any(mapped[key] for key in EVENT_DATES)
        if not mapped["has_event_date"]:
            issues.append("event_date_relevance_unknown")
        mapped["candidate_group_id"] = fingerprint(
            [
                code,
                *(row[key] for key in ("end_date", "ann_date", "div_proc", "imp_ann_date")),
            ]
        )
        mapped["quality_flags"] = issues
        result.append(mapped)
    content_counts = Counter(row["content_id"] for row in result)
    groups = defaultdict(set)
    for row in result:
        groups[row["candidate_group_id"]].add(row["content_id"])
    for row in result:
        row["duplicate_occurrences"] = content_counts[row["content_id"]]
        row["candidate_conflict"] = len(groups[row["candidate_group_id"]]) > 1
        if row["duplicate_occurrences"] > 1:
            row["quality_flags"].append("duplicate_content")
        if row["candidate_conflict"]:
            row["quality_flags"].append("candidate_version_conflict")
        row["quality_flags"] = json.dumps(row["quality_flags"], ensure_ascii=True)
    if len(result) != receipt["rows"]:
        raise DataValidationError("dividend observation population changed")
    return result


def link_window(window, rows, cutoff):
    """Candidate overlap is inclusive; observed history is distinct from signal knowledge."""
    entry, exit_day = window["entry_date"], window["exit_date"]
    if bool(window["entry_beyond_cutoff"]) != (entry is None):
        raise DataValidationError("entry cutoff evidence is inconsistent")
    if bool(window["exit_beyond_cutoff"]) != (exit_day is None):
        raise DataValidationError("exit cutoff evidence is inconsistent")
    if entry and exit_day and exit_day < entry:
        raise DataValidationError("window exit precedes entry")
    if entry is None and exit_day is not None:
        raise DataValidationError("known exit cannot precede an unavailable entry")
    identity = fingerprint(window)
    end = exit_day or cutoff
    signal_close = datetime.fromisoformat(window["trade_date"] + "T15:00:00+08:00")
    links = []
    for row in rows:
        if row["instrument_id"] != window["instrument_id"]:
            raise DataValidationError("cross-instrument window candidates")
        if not entry or not any(row[key] and entry <= row[key] <= end for key in EVENT_DATES):
            continue
        record_inside = bool(row["record_date"] and entry <= row["record_date"] <= end)
        notice_dates = [row[key] for key in ("ann_date", "imp_ann_date") if row[key]]
        known = datetime.fromisoformat(row["observed_at"]) <= signal_close
        known &= all(day <= window["trade_date"] for day in notice_dates)
        links.append(
            {
                "window_id": identity,
                "observation_id": row["observation_id"],
                "content_id": row["content_id"],
                "candidate_group_id": row["candidate_group_id"],
                "implementation_candidate": row["implementation_candidate"],
                "record_date_in_window": record_inside,
                "payment_after_intended_exit": None
                if exit_day is None
                else bool(
                    record_inside and exit_day and row["pay_date"] and row["pay_date"] > exit_day
                ),
                "listing_after_intended_exit": None
                if exit_day is None
                else bool(
                    record_inside
                    and exit_day
                    and row["div_listdate"]
                    and row["div_listdate"] > exit_day
                ),
                "locally_observed_by_signal_close": known,
                "candidate_conflict": row["candidate_conflict"],
                "quality_flags": row["quality_flags"],
            }
        )
    summary = {
        **window,
        "window_id": identity,
        "candidate_observations": len(links),
        "candidate_unique_contents": len({row["content_id"] for row in links}),
        "implementation_candidates": sum(row["implementation_candidate"] for row in links),
        "conflicting_candidates": sum(row["candidate_conflict"] for row in links),
        "observed_candidates_at_signal_close": sum(
            row["locally_observed_by_signal_close"] for row in links
        ),
        "payments_after_intended_exit": sum(
            row["payment_after_intended_exit"]
            for row in links
            if row["payment_after_intended_exit"] is not None
        ),
        "listings_after_intended_exit": sum(
            row["listing_after_intended_exit"]
            for row in links
            if row["listing_after_intended_exit"] is not None
        ),
        "undated_observations_for_code": sum(not row["has_event_date"] for row in rows),
        "unknown_event_history": True,
        "entitlement_amount": None,
        "dividend_tax_fen": None,
        "complete_cost_fen": None,
        "cashflow_eligible": False,
        "execution_authority": False,
    }
    summary["overlap_evaluated"] = entry is not None
    summary["overlap_through_intended_exit"] = entry is not None and exit_day is not None
    summary["post_exit_comparison_known"] = exit_day is not None
    if exit_day is None:
        summary["payments_after_intended_exit"] = None
        summary["listings_after_intended_exit"] = None
    if entry is None:
        for key in (
            "candidate_observations",
            "candidate_unique_contents",
            "implementation_candidates",
            "conflicting_candidates",
            "observed_candidates_at_signal_close",
            "payments_after_intended_exit",
            "listings_after_intended_exit",
        ):
            summary[key] = None
    return summary, links
