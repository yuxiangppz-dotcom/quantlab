"""Immutable multi-session price observations for frozen article-strategy lists.

The market contract contains actual raw daily OHLC plus provider adj_factor,
exchange sessions, date-qualified target statuses/limits, and an aware asof.
No provider calls, synthetic fills, T+0 exits, fees or net-profit inference.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from quantlab.scout.article_risks import day_key, finite, stable_id

VERSION = "article_frozen_price_observation_v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 2, 3, 5)


def _stamp(value: object) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(SHANGHAI) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _known(record: dict, asof: datetime) -> bool:
    if "source_status" in record and record["source_status"] not in {"available", "complete", "ok"}:
        return False
    if (
        record.get("complete") is False
        or record.get("verified") is False
        or record.get("partial") is True
        or record.get("truncated") is True
    ):
        return False
    for key in ("first_seen_at", "observed_at"):
        if key in record:
            stamp = _stamp(record[key])
            if stamp is None or stamp > asof:
                return False
    return True


def _indexed(rows: list[dict], asof: datetime) -> tuple[dict, set]:
    output, conflicts = {}, set()
    for row in rows:
        day = day_key(row.get("date", row.get("trade_date")))
        if not day or not _known(row, asof):
            continue
        canonical = {
            "date": day,
            **{key: finite(row.get(key)) for key in ("open", "high", "low", "close", "adj_factor")},
        }
        if day in output and output[day] != canonical:
            conflicts.add(day)
        output[day] = canonical
    return output, conflicts


def _valid(row: dict | None) -> bool:
    if row is None:
        return False
    fields = [row[key] for key in ("open", "high", "low", "close", "adj_factor")]
    if any(value is None or value <= 0 for value in fields):
        return False
    return (
        row["low"]
        <= min(row["open"], row["close"])
        <= max(row["open"], row["close"])
        <= row["high"]
    )


def _session_reached(asof: datetime, day: str, at: time) -> bool:
    return bool(day) and asof >= datetime.combine(datetime.fromisoformat(day).date(), at, SHANGHAI)


def _field_record(table: dict, code: str, target: str) -> dict | None:
    value = table.get(code)
    if isinstance(value, list):
        matches = [
            item for item in value if day_key(item.get("date", item.get("trade_date"))) == target
        ]
        if len(matches) != 1:
            return None
        return matches[0]
    if isinstance(value, dict) and day_key(value.get("date", value.get("trade_date"))) == target:
        return value
    return None


def _entry_observation(
    candidate: dict,
    bars: dict,
    conflicts: set,
    market: dict,
    target: str,
    signal: str,
    asof: datetime,
) -> dict:
    result = {
        "entry_check": "pending",
        "frozen": False,
        "observed_at": asof.isoformat(),
        "target_date": target,
        "reason_code": "target_open_not_observed",
        "entry_mode": "open_reference",
        "fill_status": "not_inferred",
        "evaluation_basis": "retrospective_open_from_daily_not_a_preopen_confirmation",
    }
    if not _session_reached(asof, target, time(9, 30)):
        return result
    code = candidate.get("ts_code", candidate.get("instrument_id"))
    price = candidate.get("price", candidate.get("prices", {}))
    d1, d0 = bars.get(target), bars.get(signal)
    status = _field_record(market.get("statuses", {}), code, target)
    limits = _field_record(market.get("limits", {}), code, target)
    result.update(
        {"entry_check": "unobservable", "reason_code": "target_open_status_or_limits_missing"}
    )
    if (
        status is not None
        and _known(status, asof)
        and status.get("status") in {"suspended", "st", "delisting", "not_tradable"}
    ):
        result.update(
            {
                "entry_check": "fail",
                "frozen": True,
                "reason_code": "target_not_tradable",
                "status_receipt": deepcopy(status),
                "evidence_sha256": stable_id({"target_status_receipt": status}),
            }
        )
        return result
    if (
        not _valid(d1)
        or not _valid(d0)
        or target in conflicts
        or signal in conflicts
        or status is None
        or not _known(status, asof)
        or status.get("status") != "tradable"
        or limits is None
        or not _known(limits, asof)
    ):
        return result
    upper, lower = finite(limits.get("up_limit")), finite(limits.get("down_limit"))
    tick = finite(limits.get("tick", 0.01))
    if upper is None or lower is None or tick is None or not 0 < lower < upper or tick <= 0:
        result["reason_code"] = "target_limits_invalid"
        return result
    actual_open = d1["open"]
    if actual_open < lower - tick / 2 or actual_open > upper + tick / 2:
        result["reason_code"] = "open_inconsistent_with_actual_limits"
        return result
    adjusted_open = finite(actual_open * d1["adj_factor"] / d0["adj_factor"])
    entry_low, entry_high, invalidation = [
        finite(price.get(key)) for key in ("entry_low", "entry_high", "invalidation")
    ]
    inputs = {
        "raw_open": actual_open,
        "adjusted_open": adjusted_open,
        "signal_adj_factor": d0["adj_factor"],
        "target_adj_factor": d1["adj_factor"],
        "up_limit": upper,
        "down_limit": lower,
        "tick": tick,
        "target_status": status["status"],
        "entry_low": entry_low,
        "entry_high": entry_high,
        "invalidation": invalidation,
        "price_status": price.get("status"),
    }
    result.update({"inputs": inputs, "evidence_sha256": stable_id(inputs), "frozen": True})
    if (
        adjusted_open is None
        or entry_low is None
        or entry_high is None
        or invalidation is None
        or not 0 < invalidation < entry_low <= entry_high
        or price.get("status") != "valid"
    ):
        result.update({"entry_check": "fail", "reason_code": "frozen_reference_interval_invalid"})
    elif adjusted_open <= invalidation:
        result.update({"entry_check": "fail", "reason_code": "open_not_above_invalidation"})
    elif adjusted_open < entry_low:
        result.update({"entry_check": "fail", "reason_code": "open_below_reference_low"})
    elif adjusted_open > entry_high:
        result.update({"entry_check": "fail", "reason_code": "open_above_reference_high"})
    elif abs(actual_open - upper) <= tick / 2:
        result.update(
            {"entry_check": "unobservable", "reason_code": "open_at_up_limit_execution_unconfirmed"}
        )
    elif abs(actual_open - lower) <= tick / 2:
        result.update(
            {
                "entry_check": "unobservable",
                "reason_code": "open_at_down_limit_execution_unconfirmed",
            }
        )
    else:
        result.update({"entry_check": "pass", "reason_code": "open_reference_condition_met"})
    return result


def _window_observation(
    bars: dict, conflicts: set, sessions: list[str], target: str, horizon: int, asof: datetime
) -> dict:
    result = {
        "status": "pending",
        "horizon": horizon,
        "dates": [],
        "open_to_close": None,
        "mfe": None,
        "mae": None,
        "price_basis": "raw_price_times_same_provider_adj_factor",
    }
    if target not in sessions:
        result["reason_code"] = "target_session_calendar_unknown"
        return result
    index = sessions.index(target)
    window = sessions[index : index + horizon]
    result["dates"] = window
    if len(window) != horizon or not _session_reached(asof, window[-1], time(15)):
        result["reason_code"] = "window_not_mature"
        return result
    missing = [day for day in window if day in conflicts or not _valid(bars.get(day))]
    if missing:
        result.update(
            {
                "status": "missing",
                "missing_dates": missing,
                "reason_code": "necessary_daily_price_missing_or_invalid",
            }
        )
        return result
    start = finite(bars[target]["open"] * bars[target]["adj_factor"])
    end = finite(bars[window[-1]]["close"] * bars[window[-1]]["adj_factor"])
    highs = [finite(bars[day]["high"] * bars[day]["adj_factor"]) for day in window]
    lows = [finite(bars[day]["low"] * bars[day]["adj_factor"]) for day in window]
    if start is None or start <= 0 or end is None or any(value is None for value in highs + lows):
        result.update({"status": "missing", "reason_code": "invalid_adjusted_price_scale"})
        return result
    metrics = {
        "open_to_close": finite(end / start - 1),
        "mfe": finite(max(highs) / start - 1),
        "mae": finite(min(lows) / start - 1),
    }
    if any(value is None for value in metrics.values()):
        result.update({"status": "missing", "reason_code": "nonfinite_price_ratio"})
        return result
    result.update({"status": "complete", "reason_code": "actual_price_window_complete", **metrics})
    return result


def _summary(rows: list[dict]) -> dict:
    result = {"original_count": len(rows), "windows": {}}
    for horizon in HORIZONS:
        observations = [row["windows"][str(horizon)] for row in rows]
        complete = [item for item in observations if item["status"] == "complete"]
        result["windows"][str(horizon)] = {
            "total_count": len(rows),
            "complete_count": len(complete),
            "pending_count": sum(item["status"] == "pending" for item in observations),
            "missing_count": sum(item["status"] == "missing" for item in observations),
            "coverage": len(complete) / len(rows) if rows else None,
            "mean_open_to_close_price_change": mean(item["open_to_close"] for item in complete)
            if complete
            else None,
            "mean_mfe": mean(item["mfe"] for item in complete) if complete else None,
            "mean_mae": mean(item["mae"] for item in complete) if complete else None,
            "not_net_returns": True,
        }
    return result


def observe(frozen_report: dict, market: dict, previous: dict | None = None) -> dict:
    """Retain every frozen candidate; first determined opening scenario never changes.

    Daily values are RAW. Their adj_factor must be from one consistent provider
    scale. An actual signal-day factor maps the target open onto frozen bounds.
    The horizon price ratios share D1's open denominator; missing days are never
    replaced by later records, and future/unfinished windows remain pending.
    """
    asof = _stamp(market.get("asof"))
    if asof is None:
        raise ValueError("Article observation requires an aware actual asof")
    report_hash = stable_id(frozen_report)
    signal, target = (
        day_key(frozen_report.get("signal_date")),
        day_key(frozen_report.get("target_date")),
    )
    if not signal or not target or target <= signal:
        raise ValueError("Invalid frozen article signal/target dates")
    sessions = sorted({day_key(value) for value in market.get("sessions", []) if day_key(value)})
    candidates = frozen_report.get("candidates", [])
    ids = [row.get("ts_code", row.get("instrument_id")) for row in candidates]
    if any(not code for code in ids) or len(ids) != len(set(ids)):
        raise ValueError("Frozen article candidates require unique security identities")
    prior_rows = {}
    if previous is not None:
        if previous.get("version") != VERSION or previous.get("report_sha256") != report_hash:
            raise ValueError("Previous observation does not belong to frozen report")
        if previous.get("sha256") != stable_id(
            {k: v for k, v in previous.items() if k != "sha256"}
        ):
            raise ValueError("Previous article observation integrity mismatch")
        prior_asof = _stamp(previous.get("observed_at"))
        if prior_asof is None or asof < prior_asof:
            raise ValueError("Observation clocks cannot run backward")
        prior_rows = {row["ts_code"]: row for row in previous["rows"]}
        if set(prior_rows) != set(ids):
            raise ValueError("Previous observation removed or added frozen candidates")
    result = {
        "version": VERSION,
        "run_id": frozen_report.get("run_id", report_hash[:20]),
        "report_sha256": report_hash,
        "market_sha256": stable_id(market),
        "strategy_version": frozen_report.get("strategy_version"),
        "config_hash": frozen_report.get("config_hash"),
        "signal_date": signal,
        "target_date": target,
        "observed_at": asof.isoformat(),
        "main_price_observation": "D1_open_to_D3_close",
        "rows": [],
        "not_fills_or_net_profit": True,
        "d1_open_to_close_is_diagnostic_not_t_plus_zero_exit": True,
        "missing_policy": "retain_all_frozen_candidates_no_zero_fill_no_replacement",
        "previous_sha256": previous.get("sha256") if previous else None,
    }
    for candidate, code in zip(candidates, ids, strict=True):
        candidate = deepcopy(candidate)
        bars, conflicts = _indexed(market.get("daily", {}).get(code, []), asof)
        current_entry = _entry_observation(candidate, bars, conflicts, market, target, signal, asof)
        prior_entry = prior_rows.get(code, {}).get("entry_observation")
        entry = (
            deepcopy(prior_entry) if prior_entry and prior_entry.get("frozen") else current_entry
        )
        gap = None
        gap_status = "pending" if not _session_reached(asof, target, time(9, 30)) else "missing"
        if (
            gap_status != "pending"
            and _valid(bars.get(target))
            and _valid(bars.get(signal))
            and not {target, signal} & conflicts
        ):
            start, last = bars[target], bars[signal]
            numerator = finite(start["open"] * start["adj_factor"])
            denominator = finite(last["close"] * last["adj_factor"])
            if numerator is not None and denominator is not None and denominator > 0:
                gap = finite(numerator / denominator - 1)
                gap_status = "complete" if gap is not None else "missing"
        windows = {
            str(n): _window_observation(bars, conflicts, sessions, target, n, asof)
            for n in HORIZONS
        }
        row = {
            "ts_code": code,
            "name": candidate.get("name"),
            "final_status": candidate.get("final_status"),
            "route": candidate.get("route", candidate.get("setup")),
            "routes": candidate.get("routes"),
            "strategy_version": candidate.get("strategy_version", result["strategy_version"]),
            "data_version": candidate.get("data_version", frozen_report.get("data_version")),
            "chart_version": candidate.get("chart_version", frozen_report.get("chart_version")),
            "chart_status": candidate.get("chart_status"),
            "chart_quality": candidate.get("intraday_quality"),
            "group_ids": candidate.get("group_ids", []),
            "entry_observation": entry,
            "entry_check": entry["entry_check"],
            "entry_evidence_changed_since_freeze": bool(
                prior_entry
                and prior_entry.get("frozen")
                and current_entry.get("evidence_sha256")
                and prior_entry.get("evidence_sha256") != current_entry["evidence_sha256"]
            ),
            "latest_entry_evidence_diagnostic": current_entry
            if prior_entry and prior_entry.get("frozen")
            else None,
            "d1_gap": gap,
            "d1_gap_status": gap_status,
            "windows": windows,
            "main_price_change": windows["3"]["open_to_close"],
            "observation_asof": asof.isoformat(),
        }
        result["rows"].append(row)
    result["all_frozen"] = _summary(result["rows"])
    passed = [row for row in result["rows"] if row["entry_check"] == "pass"]
    result["open_reference_pass_subset"] = {
        "ids": [row["ts_code"] for row in passed],
        **_summary(passed),
    }
    result["entry_counts"] = {
        state: sum(row["entry_check"] == state for row in result["rows"])
        for state in ("pending", "unobservable", "pass", "fail")
    }
    return {**result, "sha256": stable_id(result)}


def save_observation(output_root: str | Path, tracking: dict) -> Path:
    """Save immutable content-addressed evidence, atomically; never overwrite old runs.

    Caller chooses an isolated observation directory, never canonical data.
    Existing byte-identical snapshots are safe to reuse; collisions fail closed.
    """
    if tracking.get("sha256") != stable_id({k: v for k, v in tracking.items() if k != "sha256"}):
        raise ValueError("Article observation checksum invalid")
    run_id = str(tracking.get("run_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", run_id):
        raise ValueError("Unsafe article observation run identity")
    asof = _stamp(tracking.get("observed_at"))
    if asof is None:
        raise ValueError("Article observation timestamp invalid")
    root = Path(output_root).resolve()
    directory = root / run_id / "observations"
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Observation directory resolves outside selected root")
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Observation directory resolves outside selected root")
    path = directory / f"{asof.strftime('%Y%m%dT%H%M%S%f')}-{tracking['sha256'][:20]}.json"
    if not path.resolve().is_relative_to(root):
        raise ValueError("Observation file resolves outside selected root")
    payload = (
        json.dumps(tracking, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("Immutable observation collision or corruption")
        return path
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory, prefix=".article-observation-", delete=False
        ) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("Immutable observation collision or corruption") from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path
