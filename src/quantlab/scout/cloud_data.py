"""Bounded TuShare refresh of an explicitly marked isolated cloud volume only."""

import math
import os
from datetime import datetime, timedelta
from time import monotonic

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
from quantlab.scout.daily_runtime import atomic, read
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
        start_date=(now.date() - timedelta(days=90)).strftime("%Y%m%d"),
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


def refresh_market(root, fetcher, now):
    root = guarded(root)
    storage = ParquetStorage(root / "market")
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
    asof = latest_completed_session(storage, now)
    days = sorted(
        {
            r.trade_date
            for r in storage.load_trading_calendar()
            if r.exchange == "SSE" and r.is_open and r.trade_date <= asof
        }
    )[-21:]
    if len(days) != 21:
        raise ValueError("Calendar lacks a complete 21-session research window")
    methods = {
        "daily": (storage.daily_bars_path, storage.save_daily_bars_by_date),
        "adj_factor": (storage.adj_factor_path, storage.save_adj_factors_by_date),
        "daily_basic": (storage.daily_basic_path, storage.save_daily_basic_by_date),
        "stk_limit": (storage.daily_price_limit_path, storage.save_daily_price_limits_by_date),
    }
    for day in days:
        for api, (path, save) in methods.items():
            if api in {"daily_basic", "stk_limit"} and day != asof:
                continue
            if not path(day).resolve().is_relative_to(root.resolve()):
                raise ValueError("Partition points outside isolated cloud volume")
            if not path(day).exists():
                rows = fetcher.all_rows(api, trade_date=day.strftime("%Y%m%d"))
                save(convert(api, rows, day), day)
        bars = {r.instrument_id for r in storage.load_daily_bars_by_date(day)}
        factors = {r.instrument_id for r in storage.load_adj_factors_by_date(day)}
        if not bars or not bars.issubset(factors):
            raise ValueError("Daily bars/adjustment factors do not cover the same instruments")
    return {"asof_session": asof.isoformat(), "provider_calls": fetcher.calls}
