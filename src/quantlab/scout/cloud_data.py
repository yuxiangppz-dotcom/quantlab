"""Bounded TuShare refresh of an explicitly marked isolated cloud volume only."""

import math
import os
from datetime import datetime, timedelta
from time import monotonic
from uuid import uuid4

import pandas as pd

from quantlab.data.models import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    DailyPriceLimit,
    Security,
    TradingCalendar,
)
from quantlab.data.storage import ParquetStorage
from quantlab.scout.cloud_artifacts import guarded
from quantlab.scout.daily_runtime import atomic, exclusive, read
from quantlab.scout.market import latest_completed_session
from quantlab.scout.models import fingerprint

FIELDS = {
    "trade_cal": "exchange,cal_date,is_open",
    "stock_basic": "ts_code,symbol,name,exchange,market,list_status,list_date,delist_date",
    "daily": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    "adj_factor": "ts_code,trade_date,adj_factor",
    "daily_basic": "ts_code,trade_date,turnover_rate,total_mv,circ_mv",
    "stk_limit": "ts_code,trade_date,pre_close,up_limit,down_limit,exchange",
}
CAPS = {"daily": 6000, "adj_factor": 6000, "daily_basic": 6000, "stk_limit": 5800}


def secure_tushare_factory(factory):
    """Only upgrade the pinned SDK's known official HTTP endpoint, no custom host."""

    def create(*args, **kwargs):
        client = factory(*args, **kwargs)
        url = getattr(client, "_DataApi__http_url", None)
        allowed = {"http://api.waditu.com/dataapi", "https://api.waditu.com/dataapi"}
        if url not in allowed:
            raise ValueError("Unexpected TuShare transport in pinned SDK")
        client._DataApi__http_url = "https://api.waditu.com/dataapi"
        return client

    return create


class Fetcher:
    def __init__(self, root, now, client=None):
        self.root = guarded(root)
        self.now = now
        self.calls = 0
        self.started = monotonic()
        if client is None:
            import tushare as ts

            client = secure_tushare_factory(ts.pro_api)(os.environ["TUSHARE_TOKEN"], timeout=20)
        self.client = client

    def fetch(self, api, **params):
        identity = fingerprint([api, params, FIELDS[api]])
        path = self.root / "source_updates" / self.now.date().isoformat() / (identity + ".json")
        if path.exists():
            saved = read(path)
            if saved["status"] != "ok":
                raise ValueError("Previous data request failed; no automatic same-day retry")
            return saved["rows"]
        if self.calls >= 100 or monotonic() - self.started >= 900:
            raise ValueError("Cloud data update budget reached")
        self.calls += 1
        saved = {"api": api, "params": params, "retrieved_at": self.now.isoformat()}
        try:
            frame = self.client.query(api, fields=FIELDS[api], **params)
            if len(frame) > params.get("limit", 10_000):
                raise ValueError("Provider ignored requested row bound")
            rows = frame.astype(object).where(pd.notna(frame), None).to_dict("records")
            saved.update(status="ok", rows=rows)
        except Exception as exc:
            saved.update(status="failed", error_type=type(exc).__name__)
            atomic(path, saved)
            raise ValueError("Cloud data provider request failed; see private receipt") from None
        atomic(path, saved)
        return rows

    def all_rows(self, api, **params):
        limit = CAPS[api]
        rows = []
        seen = set()
        for page in range(3):
            batch = self.fetch(api, **params, limit=limit, offset=page * limit)
            for row in batch:
                key = (row["ts_code"], row["trade_date"])
                if key in seen:
                    raise ValueError("Duplicate/pagination ignored; coverage unknown")
                seen.add(key)
                rows.append(row)
            if len(batch) < limit:
                return rows
        raise ValueError("Pagination budget reached; coverage unknown")


def ymd(value):
    return datetime.strptime(str(value), "%Y%m%d").date()


def numeric(value, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("Invalid provider number")
    return result


def convert(api, rows, day):
    result = []
    for row in rows:
        if ymd(row["trade_date"]) != day:
            raise ValueError("Provider partition date mismatch")
        code = row["ts_code"]
        if not code.endswith((".SH", ".SZ", ".BJ")):
            continue  # Price-limit endpoint also covers non-A-share instruments.
        if api == "daily":
            result.append(
                DailyBar(
                    code,
                    day,
                    *(
                        numeric(row[k], positive=True)
                        for k in ("open", "high", "low", "close", "pre_close")
                    ),
                    numeric(row["vol"]) * 100,
                    numeric(row["amount"]) * 1000,
                )
            )
        elif api == "adj_factor":
            result.append(AdjFactor(code, day, numeric(row["adj_factor"], positive=True)))
        elif api == "daily_basic":
            result.append(
                DailyBasic(
                    code,
                    day,
                    numeric(row["turnover_rate"]) / 100,
                    numeric(row["total_mv"], positive=True) * 10_000,
                    numeric(row["circ_mv"], positive=True) * 10_000,
                )
            )
        elif api == "stk_limit":
            result.append(
                DailyPriceLimit(
                    code,
                    day,
                    None if row.get("pre_close") is None else numeric(row["pre_close"]),
                    numeric(row["up_limit"], positive=True),
                    numeric(row["down_limit"], positive=True),
                    row.get("exchange"),
                    "tushare:stk_limit",
                    code + ":" + day.isoformat(),
                )
            )
    if not result or len({r.instrument_id for r in result}) != len(result):
        raise ValueError("Empty or duplicated A-share partition")
    return result


def refresh_calendar(root, fetcher, now):
    storage = ParquetStorage(guarded(root) / "market")
    calendar = storage.load_trading_calendar()
    # A fresh calendar is archived once per day; errors cannot change existing records.
    rows = fetcher.fetch(
        "trade_cal",
        exchange="SSE",
        start_date=(now.date() - timedelta(days=365)).strftime("%Y%m%d"),
        end_date=(now.date() + timedelta(days=45)).strftime("%Y%m%d"),
    )
    merged = {(r.exchange, r.trade_date): r for r in calendar}
    for row in rows:
        if row["exchange"] != "SSE" or row["is_open"] not in {0, 1}:
            raise ValueError("Invalid exchange calendar")
        record = TradingCalendar("SSE", ymd(row["cal_date"]), bool(row["is_open"]))
        merged[(record.exchange, record.trade_date)] = record
    if ("SSE", now.date()) not in merged:
        raise ValueError("Calendar does not cover today")
    storage.save_trading_calendar(list(merged.values()))
    return storage


def _refresh_master(storage, fetcher):
    securities = []
    for state in ("L", "D", "P"):
        for row in fetcher.fetch("stock_basic", exchange="", list_status=state):
            code = row["ts_code"]
            if not code.endswith((".SH", ".SZ", ".BJ")):
                continue
            securities.append(
                Security(
                    code,
                    row["symbol"],
                    row["name"],
                    row["exchange"],
                    code.rsplit(".", 1)[1],
                    row["market"],
                    row["list_status"],
                    ymd(row["list_date"]),
                    ymd(row["delist_date"]) if row.get("delist_date") else None,
                )
            )
    if not securities:
        raise ValueError("Securities master empty")
    storage.save_securities(securities)


def _methods(storage):
    return {
        "daily": (storage.daily_bars_path, storage.save_daily_bars_by_date),
        "adj_factor": (storage.adj_factor_path, storage.save_adj_factors_by_date),
        "daily_basic": (storage.daily_basic_path, storage.save_daily_basic_by_date),
        "stk_limit": (storage.daily_price_limit_path, storage.save_daily_price_limits_by_date),
    }


def _window(storage, now, sessions):
    asof = latest_completed_session(storage, now)
    days = sorted(
        {
            r.trade_date
            for r in storage.load_trading_calendar()
            if r.exchange == "SSE" and r.is_open and r.trade_date <= asof
        }
    )[-sessions:]
    if len(days) != sessions:
        raise ValueError(f"Calendar lacks a complete {sessions}-session research window")
    return asof, days


def _path(root, method, day):
    path = method(day)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Partition points outside isolated cloud volume")
    return path


def _coverage(storage, day):
    bars = storage.load_daily_bars_by_date(day)
    adjustments = storage.load_adj_factors_by_date(day)
    codes = {r.instrument_id for r in bars}
    factors = {r.instrument_id for r in adjustments if finite_positive(r.adj_factor)}
    if not codes or not codes.issubset(factors):
        return "daily_adjustment_instrument_coverage_unknown"
    if (
        len(codes) != len(bars)
        or len({r.instrument_id for r in adjustments}) != len(adjustments)
        or any(row.trade_date != day for row in bars + adjustments)
    ):
        return "partition_identity_or_duplicate_error"
    return None


def finite_positive(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _required(storage, root, days, asof):
    methods = _methods(storage)
    result = []
    for day in days:
        for api, (path, save) in methods.items():
            if api in {"daily_basic", "stk_limit"} and day != asof:
                continue
            result.append((day, api, _path(root, path, day), save))
    return result


def prepare_history(root, fetcher, now, *, partition_limit=40):
    """Prepare a resumable isolated 120-session cache in bounded maintenance batches.

    File existence is insufficient: every daily instrument must have a positive
    matching factor. Invalid existing partitions are reported, never overwritten.
    Fetcher still enforces the shared 100 calls/900 seconds and page bounds.
    No model, prediction claim, notification or canonical path is touched.
    """
    if not isinstance(partition_limit, int) or not 1 <= partition_limit <= 40:
        raise ValueError("History partition batch must be between one and forty")
    root = guarded(root)
    directory = root / "data_preparation"
    if not directory.resolve().is_relative_to(root.resolve()):
        raise ValueError("Data preparation path outside isolated volume")
    with exclusive(directory / "history.lock"):
        state_path = directory / "history120.json"
        previous = read(state_path) if state_path.exists() else {}
        sequence = previous.get("batch_sequence", 0) + 1
        receipt = {
            "version": "isolated_history120_v1",
            "status": "preparing",
            "history_sessions": 120,
            "partition_limit": partition_limit,
            "batch_sequence": sequence,
            "updated_at": now.isoformat(),
            "completed_this_batch": 0,
            "provider_calls": 0,
            "remaining_partitions": None,
            "coverage_issues": [],
        }
        calls_before = fetcher.calls

        def save_receipt():
            receipt["provider_calls"] = fetcher.calls - calls_before
            atomic(state_path, receipt)
            atomic(directory / "batches" / f"batch-{sequence:06d}.json", receipt)

        try:
            storage = refresh_calendar(root, fetcher, now)
            if not storage.load_securities():
                _refresh_master(storage, fetcher)
            asof, days = _window(storage, now, 120)
            receipt.update(asof_session=asof.isoformat(), history_start=days[0].isoformat())
            required = _required(storage, root, days, asof)
            pending = [row for row in required if not row[2].is_file()]
            receipt.update(
                required_partitions=len(required),
                remaining_partitions=len(pending),
                reused_partitions=len(required) - len(pending),
            )
            save_receipt()
            for day, api, _partition_path, save in pending[:partition_limit]:
                receipt["last_operation"] = {"api": api, "session": day.isoformat()}
                rows = fetcher.all_rows(api, trade_date=day.strftime("%Y%m%d"))
                save(convert(api, rows, day), day)
                receipt["completed_this_batch"] += 1
                receipt["remaining_partitions"] -= 1
                save_receipt()
            # Only check sessions whose pair now exists. Missing pairs stay pending.
            for day in days:
                if (
                    storage.daily_bars_path(day).is_file()
                    and storage.adj_factor_path(day).is_file()
                ):
                    issue = _coverage(storage, day)
                    if issue:
                        receipt["coverage_issues"].append(
                            {"session": day.isoformat(), "reason": issue}
                        )
            receipt["status"] = (
                "blocked_coverage"
                if receipt["coverage_issues"]
                else "pending"
                if receipt["remaining_partitions"]
                else "ready"
            )
            receipt["progress_fraction"] = (len(required) - receipt["remaining_partitions"]) / len(
                required
            )
        except Exception as exc:
            receipt.update(
                status="failed",
                error_type=type(exc).__name__,
                coverage_state="unknown_not_complete",
            )
        save_receipt()
        return receipt


def refresh_market(root, fetcher, now, *, history_sessions=21, initialize_history=False):
    """Refresh recent increments; longer missing history belongs to maintenance."""
    if history_sessions not in {21, 120}:
        raise ValueError("Supported fixed research history is 21 or 120 sessions")
    root = guarded(root)
    storage = ParquetStorage(root / "market")
    if initialize_history and history_sessions == 120:
        receipt = prepare_history(root, fetcher, now)
        if receipt["status"] != "ready":
            return receipt
    try:
        asof, days = _window(storage, now, history_sessions)
    except ValueError:
        if history_sessions == 120:
            raise ValueError("history_bootstrap_required: calendar lacks 120 sessions") from None
        raise
    if history_sessions == 120:
        for day in days[:-21]:
            if (
                not storage.daily_bars_path(day).is_file()
                or not storage.adj_factor_path(day).is_file()
                or _coverage(storage, day)
            ):
                raise ValueError("history_bootstrap_required: earlier cache missing/incomplete")
    _refresh_master(storage, fetcher)
    methods = _methods(storage)
    for day in days:
        for api, (path, save) in methods.items():
            if api in {"daily_basic", "stk_limit"} and day != asof:
                continue
            if not _path(root, path, day).exists():
                rows = fetcher.all_rows(api, trade_date=day.strftime("%Y%m%d"))
                save(convert(api, rows, day), day)
        if _coverage(storage, day):
            raise ValueError("Daily bars/adjustment factors do not cover the same instruments")
    return {
        "asof_session": asof.isoformat(),
        "provider_calls": fetcher.calls,
        "history_sessions": history_sessions,
        "status": "ready",
    }


def refresh_observation_limits(root, fetcher, now, *, max_reports=200, max_sessions=20):
    """Recover frozen D1 limits, including dates older than the refresh as-of day.

    Only missing partitions are fetched. Existing incomplete/invalid partitions
    remain untouched and retain unknown observations. The same Fetcher supplies
    the shared provider call, pagination and elapsed-time budgets.
    """
    from quantlab.scout.nextday_tracking import verify_freeze

    if not 1 <= max_reports <= 200 or not 1 <= max_sessions <= 20:
        raise ValueError("Observation supplement bounds exceeded")
    root = guarded(root)
    storage = ParquetStorage(root / "market")
    receipt = {
        "version": "frozen_d1_limit_supplement_v1",
        "status": "running",
        "started_at": now.isoformat(),
        "model_calls": 0,
        "provider_calls": 0,
        "rows": [],
        "report_issues": [],
    }
    calls_before = fetcher.calls
    with exclusive(root / "data_preparation" / "observation-limits.lock"):
        try:
            asof = latest_completed_session(storage, now)
            calendar = {
                row.trade_date
                for row in storage.load_trading_calendar()
                if row.exchange == "SSE" and row.is_open
            }
            paths = sorted((root / "runs").rglob("report.json"), reverse=True)
            receipt["omitted_reports"] = max(0, len(paths) - max_reports)
            targets = {}
            for path in paths[:max_reports]:
                try:
                    report = read(path)
                    if (
                        not report.get("nextday_freeze")
                        or report.get("synthetic")
                        or report.get("status") == "demo"
                    ):
                        continue
                    freeze = verify_freeze(report)
                    frozen_target = freeze["timing"]["target_session"]
                    if report["timing"]["target_session"] != frozen_target:
                        raise ValueError("Observation target does not match frozen timing")
                    day = datetime.fromisoformat(frozen_target).date()
                    if day not in calendar or day > asof:
                        continue
                    targets.setdefault(day, set()).update(
                        row["instrument_id"] for row in freeze["rows"]
                    )
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    receipt["report_issues"].append(
                        {"run_id": path.parent.name, "error_type": type(exc).__name__}
                    )
            # Existing partitions cost no requests and must not consume the batch
            # slots needed by missing target dates from interrupted observations.
            ordered = sorted(
                targets, key=lambda day: (storage.daily_price_limit_path(day).exists(), day)
            )
            receipt["omitted_target_sessions"] = max(0, len(ordered) - max_sessions)
            for day in ordered[:max_sessions]:
                row = {"target_session": day.isoformat(), "status": "unknown"}
                receipt["rows"].append(row)
                try:
                    partition = _path(root, storage.daily_price_limit_path, day)
                    if partition.exists():
                        row["partition_action"] = "reused_without_overwrite"
                    else:
                        values = fetcher.all_rows("stk_limit", trade_date=day.strftime("%Y%m%d"))
                        storage.save_daily_price_limits_by_date(
                            convert("stk_limit", values, day), day
                        )
                        row["partition_action"] = "created_missing_partition"
                    limits = storage.load_daily_price_limits_by_date(day)
                    codes = {item.instrument_id for item in limits}
                    if len(codes) != len(limits) or any(
                        item.trade_date != day
                        or not finite_positive(item.up_limit)
                        or not finite_positive(item.down_limit)
                        for item in limits
                    ):
                        row["reason"] = "existing_partition_invalid_preserved"
                    else:
                        missing = sorted(targets[day] - codes)
                        row.update(
                            status="partial_unknown" if missing else "ready",
                            missing_instrument_ids=missing,
                        )
                except Exception as exc:
                    row.update(status="failed_preserved", error_type=type(exc).__name__)
            receipt["status"] = (
                "unknown"
                if receipt["report_issues"]
                or receipt["omitted_reports"]
                or receipt["omitted_target_sessions"]
                or any(row["status"] != "ready" for row in receipt["rows"])
                else "ready"
            )
        except Exception as exc:
            receipt.update(status="failed_preserved", error_type=type(exc).__name__)
        receipt["provider_calls"] = fetcher.calls - calls_before
        atomic(root / "observations" / "data_supplements" / (uuid4().hex + ".json"), receipt)
    return receipt
