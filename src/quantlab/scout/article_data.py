"""Read isolated market Parquet and bind dated source evidence to article inputs.

Canonical QuantLab daily amounts are already CNY and volumes shares. Only an
explicit raw TuShare schema (ts_code/vol) is converted from thousand CNY/hands.
No provider calls, writes, forward filling, or synthetic historical membership.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from quantlab.scout.article_sources import day, membership_at
from quantlab.scout.models import SHANGHAI, fingerprint, timestamp


def _scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return (
            value.date().isoformat()
            if isinstance(value, (datetime, pd.Timestamp))
            else value.isoformat()
        )
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _read(path):
    if not path.is_file():
        return []
    return [
        {key: _scalar(value) for key, value in row.items()}
        for row in pd.read_parquet(path).to_dict("records")
    ]


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _partition(root, table, current):
    return (
        root
        / table
        / f"year={current.year}"
        / f"month={current.month:02}"
        / f"{current.isoformat()}.parquet"
    )


def _index(rows, table, partition_date=None):
    result, conflicts = {}, set()
    for row in rows:
        code = row.get("instrument_id", row.get("ts_code"))
        current = day(row.get("trade_date"))
        if not code or current is None:
            continue
        key = (code, current.isoformat())
        if partition_date is not None and current != partition_date:
            conflicts.add(key)
        if key in result and result[key] != row:
            conflicts.add(key)
        result.setdefault(key, row)
    return result, conflicts


def load_market(root, *, signal_date=None, history_sessions=160, now=None):
    """Read at least 160 exact exchange sessions from root/market, without mutation."""
    if history_sessions < 160:
        raise ValueError("Article binding loads at least 160 warmup sessions")
    root = Path(root).resolve() / "market"
    if not root.is_dir():
        raise ValueError("Isolated article market directory missing")
    securities = _read(root / "securities" / "securities.parquet")
    calendar = _read(root / "calendar" / "calendar.parquet")
    by_exchange_day, calendar_conflicts = {}, set()
    for row in calendar:
        if row.get("exchange") != "SSE":
            continue
        current = day(row.get("trade_date"))
        if current is None or row.get("is_open") not in (True, False, 0, 1):
            continue
        value = bool(row["is_open"])
        if current in by_exchange_day and by_exchange_day[current] != value:
            calendar_conflicts.add(current)
        by_exchange_day.setdefault(current, value)
    sessions = sorted(
        current
        for current, opened in by_exchange_day.items()
        if opened or current in calendar_conflicts
    )
    if not sessions:
        raise ValueError("No SSE exchange trading calendar")
    observed = sorted(
        current
        for path in (root / "daily").rglob("*.parquet")
        if (current := day(path.stem)) is not None
    )
    if not observed:
        raise ValueError("No daily market partitions")
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        raise ValueError("Market read clock must have a timezone")
    now = now.astimezone(SHANGHAI)
    completed = [
        current
        for current in sessions
        if current < now.date() or (current == now.date() and now.time() >= time(18))
    ]
    expected = max(completed) if completed else None
    eligible = [current for current in observed if expected and current <= expected]
    if signal_date is None and not eligible:
        raise ValueError("No completed market daily partition at the read clock")
    signal = day(signal_date) if signal_date is not None else max(eligible)
    if signal not in sessions:
        raise ValueError("Signal partition is outside the exchange calendar")
    if expected is None or signal > expected:
        raise ValueError("Signal session has not passed the daily availability gate")
    index = sessions.index(signal)
    warmup = sessions[max(0, index - history_sessions + 1) : index + 1]
    bars_by_code = defaultdict(list)
    issues, partition_coverage = [], []
    for current in warmup:
        daily, daily_conflicts = _index(_read(_partition(root, "daily", current)), "daily", current)
        adjustments, adj_conflicts = _index(
            _read(_partition(root, "adj_factor", current)), "adj_factor", current
        )
        limits, limit_conflicts = _index(
            _read(_partition(root, "daily_price_limit", current)), "daily_price_limit", current
        )
        basics, basic_conflicts = _index(
            _read(_partition(root, "daily_basic", current)), "daily_basic", current
        )
        partition_coverage.append(
            {
                "date": current.isoformat(),
                "daily_count": len(daily),
                "adj_count": len(adjustments),
                "limit_count": len(limits),
                "daily_basic_count": len(basics),
                "missing_adjustment_count": len(daily.keys() - adjustments.keys()),
                "missing_price_limit_count": len(daily.keys() - limits.keys()),
                "status": "complete"
                if (
                    daily
                    and not (
                        daily_conflicts
                        or adj_conflicts
                        or limit_conflicts
                        or current in calendar_conflicts
                    )
                    and daily.keys() <= adjustments.keys()
                    and daily.keys() <= limits.keys()
                )
                else "unknown",
            }
        )
        for key, raw in sorted(daily.items()):
            code, actual_date = key
            canonical = "instrument_id" in raw and "volume" in raw
            raw_tushare = "ts_code" in raw and "vol" in raw and "instrument_id" not in raw
            row = {"date": actual_date, "trade_date": actual_date, "ts_code": code}
            conflict = key in daily_conflicts or current in calendar_conflicts
            for name in ("open", "high", "low", "close", "pre_close"):
                row[name] = None if conflict else _number(raw.get(name))
            amount, volume = (
                _number(raw.get("amount")),
                _number(raw.get("volume" if canonical else "vol")),
            )
            row["amount_cny"] = (
                amount
                if canonical
                else amount * 1000
                if raw_tushare and amount is not None
                else None
            )
            row["volume_shares"] = (
                volume
                if canonical
                else volume * 100
                if raw_tushare and volume is not None
                else None
            )
            if conflict:
                row["amount_cny"] = row["volume_shares"] = None
            factor = adjustments.get(key, {})
            limit = limits.get(key, {})
            basic = basics.get(key, {})
            raw_basic = "ts_code" in basic and "instrument_id" not in basic
            basic_scale = 10000 if raw_basic else 1
            turnover = _number(basic.get("turnover_rate"))
            total_mv, circ_mv = _number(basic.get("total_mv")), _number(basic.get("circ_mv"))
            row["adj_factor"] = None if key in adj_conflicts else _number(factor.get("adj_factor"))
            row["up_limit"] = None if key in limit_conflicts else _number(limit.get("up_limit"))
            row["down_limit"] = None if key in limit_conflicts else _number(limit.get("down_limit"))
            row["turnover_rate"] = (
                None
                if key in basic_conflicts or turnover is None
                else turnover / 100
                if raw_basic
                else turnover
            )
            row["daily_basic"] = {} if key in basic_conflicts else basic
            row["total_mv_cny"] = (
                None if key in basic_conflicts or total_mv is None else total_mv * basic_scale
            )
            row["circ_mv_cny"] = (
                None if key in basic_conflicts or circ_mv is None else circ_mv * basic_scale
            )
            row["source_ids"] = [
                fingerprint(["local_parquet", table, record])
                for table, record in (
                    ("daily", raw),
                    ("adj_factor", factor),
                    ("daily_price_limit", limit),
                )
                if record
            ]
            row["unit_basis"] = (
                "canonical_CNY_shares"
                if canonical
                else "raw_TuShare_thousand_CNY_hands"
                if raw_tushare
                else "unknown_schema"
            )
            row["availability"] = "existing_local_data; original_retrieved_at_unknown"
            if conflict or key in adj_conflicts or key in limit_conflicts or key in basic_conflicts:
                row["join_status"] = "conflict_unknown"
                issues.append(
                    {
                        "ts_code": code,
                        "date": actual_date,
                        "reason": "conflicting_partition_records",
                    }
                )
            else:
                row["join_status"] = "complete" if factor and limit else "partial"
            bars_by_code[code].append(row)
    return {
        "sessions": sessions,
        "signal_date": signal,
        "latest_date": max(observed),
        "expected_completed_session": expected,
        "securities": securities,
        "bars_by_code": dict(bars_by_code),
        "warmup_dates": warmup,
        "history_sessions_loaded": len(warmup),
        "read_at": now.isoformat(),
        "freshness": "current" if signal == expected else "stale",
        "partition_coverage": partition_coverage,
        "conflicts": issues,
        "calendar_status": "unknown" if calendar_conflicts else "available",
        "calendar_conflict_dates": sorted(current.isoformat() for current in calendar_conflicts),
        "market_source": "existing_isolated_Parquet; original_availability_not_reconstructed",
    }


def _stamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        return timestamp(value)
    except (ValueError, TypeError):
        return None


def _known(row, cutoff):
    seen = _stamp(row.get("_first_seen_at", row.get("first_seen_at")))
    fetched = _stamp(row.get("_fetched_at", row.get("fetched_at")))
    if seen is None or fetched is None:
        return None
    return max(seen, fetched) if max(seen, fetched) <= cutoff else None


def _completed_requests(coverage, api, cutoff):
    result = []
    for item in coverage:
        if item.get("api") != api or item.get("status") != "available" or item.get("truncated"):
            continue
        known = _stamp(item.get("fetched_at"))
        count = item.get("row_count")
        if known is None or not isinstance(count, int) or isinstance(count, bool) or count < 0:
            continue
        if known > cutoff or item.get("valid_count", item.get("row_count")) != item.get(
            "row_count"
        ):
            continue
        result.append(item)
    return result


def _rows_from_request(rows, request, *, current=None, group=None):
    """Match a receipt to all of its retained rows, before using absence as evidence."""
    params = request.get("params", {})
    matched = {}
    for row in rows:
        if row.get("_fetched_at") != request.get("fetched_at"):
            continue
        if current is not None and day(row.get("trade_date")) != current:
            continue
        if params.get("ts_code") and row.get("ts_code") != params["ts_code"]:
            continue
        if params.get("l1_code") and row.get("l1_code") != params["l1_code"]:
            continue
        if params.get("is_new") and row.get("is_new") != params["is_new"]:
            continue
        if group is not None and row.get("ts_code") != group:
            continue
        matched[row.get("_source_id", fingerprint(row))] = row
    return list(matched.values())


def _master_rows(records, coverage, cutoff):
    known = [row for row in records if _known(row, cutoff) is not None]
    candidates, conflicts = {}, set()
    for request in _completed_requests(coverage, "stock_basic", cutoff):
        rows = _rows_from_request(known, request)
        if len(rows) != request["row_count"]:
            continue
        for row in rows:
            code = row.get("ts_code")
            if not code:
                continue
            semantic = {key: value for key, value in row.items() if not key.startswith("_")}
            previous = candidates.get(code)
            if previous is not None and semantic != {
                key: value for key, value in previous.items() if not key.startswith("_")
            }:
                conflicts.add(code)
            candidates.setdefault(code, row)
    return {code: row for code, row in candidates.items() if code not in conflicts}, conflicts


def bind_context(market, source_pack, signal_date, cutoff_at):
    """Bind exact dated states and membership, never forward-fill missing snapshots."""
    signal = day(signal_date)
    cutoff = _stamp(cutoff_at)
    if signal is None or cutoff is None:
        raise ValueError("Binding needs a valid signal day and timezone-aware cutoff")
    cutoff = cutoff.astimezone(SHANGHAI)
    records, coverage = source_pack.get("records", {}), source_pack.get("coverage", [])
    security_by_code = {}
    fresh_masters, security_conflicts = _master_rows(
        records.get("stock_basic", []), coverage, cutoff
    )
    master_rows = list(market["securities"])
    existing_codes = {row.get("instrument_id", row.get("ts_code")) for row in master_rows}
    master_rows.extend(row for code, row in fresh_masters.items() if code not in existing_codes)
    for row in master_rows:
        code = row.get("instrument_id", row.get("ts_code"))
        if not code:
            continue
        if code in security_by_code and security_by_code[code].get("master_record") != row:
            security_conflicts.add(code)
        security_by_code.setdefault(
            code,
            {
                "ts_code": code,
                "name": row.get("name"),
                "list_date": row.get("list_date"),
                "delist_date": row.get("delist_date"),
                "master_record": row,
                "is_st": None,
                "is_suspended": None,
                "is_delisting": None,
                "status_by_date": {},
                "suspended_by_date": {},
                "status_sources": {},
            },
        )
    for api in ("stock_st", "suspend_d"):
        usable = [row for row in records.get(api, []) if _known(row, cutoff) is not None]
        for request in _completed_requests(coverage, api, cutoff):
            current = day(request.get("params", {}).get("trade_date"))
            if current is None or current > cutoff.date():
                continue
            specific = request.get("params", {}).get("ts_code")
            codes = specific.split(",") if specific else list(security_by_code)
            matched = {
                row.get("_source_id", fingerprint(row)): row
                for row in usable
                if day(row.get("trade_date")) == current
                and row.get("_fetched_at") == request["fetched_at"]
                and (not specific or row.get("ts_code") in codes)
            }
            if len(matched) != request["row_count"]:
                continue
            positives = {
                row["ts_code"]
                for row in matched.values()
                if api == "stock_st" or row.get("suspend_type", "S") == "S"
            }
            for code in codes:
                if code not in security_by_code:
                    continue
                security = security_by_code[code]
                current_iso = current.isoformat()
                if api == "stock_st":
                    security["status_by_date"].setdefault(current_iso, {"is_delisting": None})[
                        "is_st"
                    ] = code in positives
                    if current == signal:
                        security["is_st"] = code in positives
                else:
                    security["suspended_by_date"][current_iso] = code in positives
                    if current == signal:
                        security["is_suspended"] = code in positives
                security["status_sources"][api + ":" + current_iso] = {
                    "source": "tushare:" + api,
                    "known_at": request["fetched_at"],
                    "request_id": request.get("request_id"),
                    "scope": request.get("scope"),
                }
    master_known = _stamp(market.get("current_master_known_at"))
    master_current = (
        day(market.get("current_master_date")) == signal
        and master_known is not None
        and master_known <= cutoff
        and market.get("current_master_status") == "available"
    )
    for code, security in security_by_code.items():
        delisted = day(security["delist_date"])
        if delisted and delisted <= signal:
            security["is_delisting"] = True
        # Current master supports current research only. It is not copied into
        # historical status_by_date: historical process status may be unknown.
        elif fresh_masters.get(code, {}).get("list_status") == "L":
            security["is_delisting"] = False
            fresh = fresh_masters[code]
            security["current_master"] = fresh
            security["master_snapshot_date"] = timestamp(fresh["_fetched_at"]).date().isoformat()
            security["master_known_at"] = _known(fresh, cutoff).isoformat()
            security["list_date"] = fresh.get("list_date", security["list_date"])
            security["name"] = fresh.get("name", security["name"])
        elif master_current and security["master_record"].get("list_status") == "L":
            security["is_delisting"] = False
        if code in security_conflicts:
            security.update(list_date=None, is_delisting=None, master_conflict=True)
        if signal.isoformat() in security["status_by_date"]:
            security["status_by_date"][signal.isoformat()]["is_delisting"] = security[
                "is_delisting"
            ]
    snapshots, partial_groups = [], []
    dc_members = [row for row in records.get("dc_member", []) if _known(row, cutoff) is not None]
    dc_by_date_group = defaultdict(list)
    for row in dc_members:
        current = day(row.get("trade_date"))
        if current and current <= signal:
            dc_by_date_group[(current, row["ts_code"])].append(row)
    dc_complete = _completed_requests(coverage, "dc_member", cutoff)
    dc_directory = {
        (day(row.get("trade_date")), row["ts_code"]): row
        for row in records.get("dc_index", [])
        if _known(row, cutoff) is not None and row.get("ts_code")
    }
    for (current, group), rows in sorted(dc_by_date_group.items()):
        requests = [
            item
            for item in dc_complete
            if day(item.get("params", {}).get("trade_date")) == current
            and item.get("params", {}).get("ts_code", group) == group
            and "con_code" not in item.get("params", {})
        ]
        directory = dc_directory.get((current, group))
        kind = directory.get("idx_type") if directory else None
        # DC industry is auxiliary to the strategy's default SW L2 industries.
        if kind == "行业板块":
            continue
        complete = kind == "概念板块" and any(
            len(_rows_from_request(dc_members, request, current=current)) == request["row_count"]
            for request in requests
        )
        known = max(_known(row, cutoff) for row in rows + ([directory] if directory else []))
        snapshot = {
            "group_id": group,
            "group_type": "theme" if kind == "概念板块" else "unclassified_dc",
            "snapshot_date": current.isoformat(),
            "members": sorted({row["con_code"] for row in rows}),
            "known_at": known.isoformat(),
            "complete": complete,
            "source": "tushare:dc_member",
            "source_ids": sorted({row["_source_id"] for row in rows}),
            "coverage_mode": "complete_daily_group" if complete else "membership_incomplete",
        }
        snapshots.append(snapshot)
        if not complete:
            partial_groups.append(group)
    # Current SW membership can form a current snapshot, never six historical ones.
    sw_requests = _completed_requests(coverage, "index_member_all", cutoff)
    sw_known = [
        row for row in records.get("index_member_all", []) if _known(row, cutoff) is not None
    ]
    sw_rows = [row for row in sw_known if membership_at(row, signal)]
    sw_by_group = defaultdict(list)
    for row in sw_rows:
        if row.get("l2_code"):
            sw_by_group[row["l2_code"]].append(row)
    for group, rows in sorted(sw_by_group.items()):
        parent = {row.get("l1_code") for row in rows}
        complete = all(
            any(
                item.get("params", {}).get("l1_code") == l1
                and item.get("params", {}).get("is_new", "Y") == latest
                and len(_rows_from_request(sw_known, item)) == item["row_count"]
                for item in sw_requests
            )
            for l1 in parent
            for latest in ("Y", "N")
        )
        snapshots.append(
            {
                "group_id": group,
                "group_type": "industry",
                "snapshot_date": signal.isoformat(),
                "members": sorted({row["ts_code"] for row in rows}),
                "known_at": max(
                    [_known(row, cutoff) for row in rows]
                    + [
                        _stamp(item["fetched_at"])
                        for item in sw_requests
                        if item.get("params", {}).get("l1_code") in parent
                    ]
                ).isoformat(),
                "complete": complete,
                "source": "tushare:index_member_all:SW2021",
                "source_ids": sorted({row["_source_id"] for row in rows}),
                "coverage_mode": (
                    "current_interval_filtered_snapshot; historical_availability_uncertain"
                ),
            }
        )
    limits = {key: [] for key in ("U", "D", "Z")}
    for row in records.get("limit_list_d", []):
        if (
            day(row.get("trade_date")) == signal
            and _known(row, cutoff) is not None
            and row.get("limit") in limits
        ):
            limits[row["limit"]].append(row["ts_code"])
    return {
        "security_by_code": security_by_code,
        "group_snapshots": snapshots,
        "limit_structure": {key: sorted(set(value)) for key, value in limits.items()},
        "signal_date": signal.isoformat(),
        "cutoff_at": cutoff.isoformat(),
        "partial_group_ids": sorted(set(partial_groups)),
        "historical_state_note": (
            "Missing dated ST/delisting/suspension stays unknown; current master not backfilled"
        ),
        "source_mode": source_pack.get("mode", "unknown"),
        "current_master_status": "available" if master_current or fresh_masters else "unknown",
        "coverage": coverage,
    }


CONTEXT_VERSION = "scout-article-context-v1"


def _link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _context_root(root):
    original = Path(root).absolute()
    if any(_link(part) for part in (original, *original.parents)):
        raise ValueError("Context root must not traverse a symbolic link")
    resolved = original.resolve(strict=True)
    marker = resolved / ".scout-article"
    if (
        not resolved.name.startswith("scout_article_")
        or _link(marker)
        or not marker.is_file()
        or marker.read_text(encoding="utf-8") != "quantlab-scout-article-v1"
    ):
        raise ValueError("Context persistence requires a newly marked scout_article_ root")
    for relative in ("article_context", "article_context/snapshots"):
        target = resolved / relative
        if _link(target) or not target.resolve().is_relative_to(resolved):
            raise ValueError("Context snapshot subtree must stay inside the isolated root")
        if target.exists() and not target.is_dir():
            raise ValueError("Context snapshot subtree is not a directory")
    return resolved


def _flag(value):
    return value if isinstance(value, bool) else None


def _day_end(current):
    return datetime.combine(current, time(23, 59, 59), tzinfo=SHANGHAI)


def _current_context(context, signal, cutoff):
    current = signal.isoformat()
    states = {}
    for code, security in context.get("security_by_code", {}).items():
        status = security.get("status_by_date", {}).get(current, {})
        states[code] = {
            "status": {
                "is_st": _flag(status.get("is_st", security.get("is_st"))),
                "is_delisting": _flag(status.get("is_delisting", security.get("is_delisting"))),
            },
            "suspended": _flag(
                security.get("suspended_by_date", {}).get(current, security.get("is_suspended"))
            ),
        }
    groups = [
        deepcopy(row)
        for row in context.get("group_snapshots", [])
        if day(row.get("snapshot_date")) == signal
        and _stamp(row.get("known_at")) is not None
        and _stamp(row["known_at"]) <= cutoff
    ]
    # Incomplete groups are preserved. Their completeness is never upgraded.
    return {
        "version": CONTEXT_VERSION,
        "signal_date": current,
        "known_at": cutoff.isoformat(),
        "security_states": states,
        "group_snapshots": groups,
    }


def _read_context(path):
    if _link(path) or not path.is_file():
        raise ValueError("Context snapshots must be regular create-only files")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        digest = saved.pop("sha")
        valid = (
            saved.get("version") == CONTEXT_VERSION
            and digest == fingerprint(saved)
            and day(saved.get("signal_date")) is not None
            and _stamp(saved.get("known_at")) is not None
            and isinstance(saved.get("security_states"), dict)
            and isinstance(saved.get("group_snapshots"), list)
        )
    except (OSError, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Context snapshot integrity or schema check failed")
    return saved, digest


def _unknown_context(payloads, current, known):
    """Equal-time disagreement invalidates that day's state, without tie breaking."""
    codes = {code for payload in payloads for code in payload["security_states"]}
    groups = {}
    for payload in payloads:
        for row in payload["group_snapshots"]:
            if day(row.get("snapshot_date")) != current:
                continue
            key = (row.get("group_type"), row.get("group_id"))
            groups.setdefault(key, deepcopy(row))
    for row in groups.values():
        row.update(
            members=[],
            complete=False,
            known_at=known.isoformat(),
            coverage_mode="equal_time_context_conflict_unknown",
            source_ids=[],
        )
    return {
        "version": CONTEXT_VERSION,
        "signal_date": current.isoformat(),
        "known_at": known.isoformat(),
        "security_states": {
            code: {"status": {"is_st": None, "is_delisting": None}, "suspended": None}
            for code in codes
        },
        "group_snapshots": list(groups.values()),
    }


def extend_context(root, context, signal_date, cutoff_at, *, save=True):
    """Persist today's actual context and recover only genuinely available past days.

    Snapshots are create-only and hash checked. Previously downloaded historical
    dates cannot be made available before their actual freeze/retrieval time.
    """
    isolated = _context_root(root)
    signal, cutoff = day(signal_date), _stamp(cutoff_at)
    if signal is None or cutoff is None:
        raise ValueError("Context extension needs a signal day and aware cutoff")
    cutoff = cutoff.astimezone(SHANGHAI)
    if signal > cutoff.date():
        raise ValueError("A context freeze cannot precede its signal day")
    payload = _current_context(context, signal, cutoff)
    digest = fingerprint(payload)
    folder = isolated / "article_context" / "snapshots"
    saved_ref = None
    if save:
        folder.mkdir(parents=True, exist_ok=True)
        _context_root(isolated)  # Check the existing subtree after creation too.
        name = f"{signal}-{cutoff:%Y%m%dT%H%M%S%f}-{digest}.json"
        target = folder / name
        try:
            with target.open("x", encoding="utf-8") as stream:
                json.dump(payload | {"sha": digest}, stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
        except FileExistsError:
            existing, existing_sha = _read_context(target)
            if existing_sha != digest or existing != payload:
                raise ValueError("Existing context snapshot cannot be replaced") from None
        saved_ref = target.relative_to(isolated).as_posix()
    by_date = defaultdict(list)
    rejected = []
    # Include the current context even in a read-only replay. Existing snapshots
    # at the exact same timestamp can expose disagreement with this input.
    by_date[signal].append((cutoff, digest, payload))
    for path in sorted(folder.glob("*.json")):
        stored, sha = _read_context(path)
        current, known = day(stored["signal_date"]), _stamp(stored["known_at"])
        if current > signal or known > cutoff:
            rejected.append({"signal_date": current.isoformat(), "reason": "after_cutoff"})
            continue
        if current < signal and known > _day_end(current):
            rejected.append(
                {"signal_date": current.isoformat(), "reason": "first_available_after_signal_day"}
            )
            continue
        by_date[current].append((known, sha, stored))
    selected, conflicts = {}, []
    for current, candidates in sorted(by_date.items()):
        latest = max(item[0] for item in candidates)
        latest_payloads = {sha: stored for known, sha, stored in candidates if known == latest}
        if len(latest_payloads) > 1:
            conflicts.append(current.isoformat())
            selected[current] = _unknown_context(list(latest_payloads.values()), current, latest)
        else:
            selected[current] = next(iter(latest_payloads.values()))
    result = deepcopy(context)
    # Rebuild history from saved daily contexts only, not from today's fresh
    # historical-status requests, accumulated metadata, or today's master.
    for security in result.get("security_by_code", {}).values():
        security["status_by_date"] = {}
        security["suspended_by_date"] = {}
    groups = []
    for current, stored in selected.items():
        current_iso = current.isoformat()
        for code, state in stored["security_states"].items():
            if code not in result.get("security_by_code", {}):
                continue
            security = result["security_by_code"][code]
            status = state.get("status", {})
            security["status_by_date"][current_iso] = {
                "is_st": _flag(status.get("is_st")),
                "is_delisting": _flag(status.get("is_delisting")),
            }
            security["suspended_by_date"][current_iso] = _flag(state.get("suspended"))
            if current == signal:
                security.update(
                    is_st=_flag(status.get("is_st")),
                    is_delisting=_flag(status.get("is_delisting")),
                    is_suspended=_flag(state.get("suspended")),
                )
        for row in stored["group_snapshots"]:
            known = _stamp(row.get("known_at"))
            if (
                day(row.get("snapshot_date")) == current
                and known is not None
                and known <= _stamp(stored["known_at"])
            ):
                groups.append(deepcopy(row))
    result["group_snapshots"] = groups
    result["partial_group_ids"] = sorted(
        {
            row["group_id"]
            for row in groups
            if day(row.get("snapshot_date")) == signal and not row.get("complete")
        }
    )
    result["context_history"] = {
        "version": CONTEXT_VERSION,
        "saved_snapshot": saved_ref,
        "selected_dates": [current.isoformat() for current in selected],
        "historical_dates": [current.isoformat() for current in selected if current < signal],
        "equal_time_conflict_dates": conflicts,
        "rejected_snapshots": rejected,
        "policy": "actual_daily_freeze_by_historical_day_end; no backfill from current metadata",
    }
    return result
