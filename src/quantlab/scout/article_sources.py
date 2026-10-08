"""Point-in-time, bounded TuShare sources for the article shortline research path.

This adapter stores immutable raw responses. It does not compute trading decisions,
equate money flow with investor identity, or reconstruct availability from data dates.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from quantlab.scout.cloud_artifacts import guarded
from quantlab.scout.cloud_data import secure_tushare_factory
from quantlab.scout.models import SHANGHAI, fingerprint, timestamp

SOURCE_VERSION = "article_tushare_6000_v1"
MANDATORY_6000 = ("dc_index", "dc_member", "dc_daily", "moneyflow_ind_dc", "ths_hot")
DEFAULT_SOURCE_CONFIG = {
    "mode": "tushare_6000",
    "enabled": {name: True for name in MANDATORY_6000},
    "max_requests": 160,
    "max_seconds": 900,
    "max_rows": 120_000,
    "cache_seconds": 6 * 3600,
    "dc_types": ["行业板块", "概念板块"],
    "ths_markets": ["热股", "行业板块", "概念板块"],
    "independent_minutes_required": False,
    "independent_announcements_required": False,
}


@dataclass(frozen=True)
class ApiSpec:
    doc_id: int
    cap: int | None
    fields: str
    params: frozenset[str]
    required: tuple[str, ...]
    empty_valid: bool = False
    units: dict[str, str] = field(default_factory=dict)
    update_note: str = "No exact update time established; actual retrieval is authoritative"

    @property
    def url(self):
        return f"https://tushare.pro/document/2?doc_id={self.doc_id}"


def spec(doc, cap, fields, params, required, **kwargs):
    return ApiSpec(doc, cap, fields, frozenset(params.split(",")), tuple(required), **kwargs)


SPECS = {
    "stock_basic": spec(
        25,
        6000,
        "ts_code,symbol,name,exchange,market,list_status,list_date,delist_date",
        "ts_code,name,exchange,market,list_status,is_hs",
        ("ts_code", "list_date", "list_status"),
    ),
    "daily_basic": spec(
        32,
        6000,
        "ts_code,trade_date,turnover_rate,total_share,float_share,free_share,total_mv,circ_mv",
        "ts_code,trade_date,start_date,end_date",
        ("ts_code", "trade_date", "total_share"),
        units={
            "total_share": "shares_10000",
            "float_share": "shares_10000",
            "free_share": "shares_10000",
            "total_mv": "CNY_10000",
            "circ_mv": "CNY_10000",
        },
    ),
    "dc_index": spec(
        362,
        5000,
        "ts_code,trade_date,name,leading,leading_code,pct_change,leading_pct,total_mv,"
        "turnover_rate,up_num,down_num,idx_type,level",
        "ts_code,name,trade_date,start_date,end_date,idx_type",
        ("ts_code", "trade_date", "idx_type"),
        units={"total_mv": "CNY_10000", "pct_change": "percent", "turnover_rate": "percent"},
    ),
    "dc_member": spec(
        363,
        5000,
        "trade_date,ts_code,con_code,name",
        "ts_code,con_code,trade_date,start_date,end_date",
        ("ts_code", "con_code", "trade_date"),
        update_note="Daily membership snapshots start 2024-12-20; never backfill current members",
    ),
    "dc_daily": spec(
        382,
        2000,
        "ts_code,trade_date,close,open,high,low,change,pct_change,vol,amount,"
        "swing,turnover_rate,category",
        "ts_code,trade_date,start_date,end_date,idx_type",
        ("ts_code", "trade_date", "close", "amount"),
        units={
            "amount": "CNY",
            "vol": "shares",
            "pct_change": "percent",
            "swing": "percent",
            "turnover_rate": "percent",
        },
    ),
    "moneyflow_ind_dc": spec(
        344,
        5000,
        "trade_date,content_type,ts_code,name,pct_change,close,net_amount,"
        "net_amount_rate,buy_elg_amount,buy_elg_amount_rate,buy_lg_amount,buy_lg_amount_rate,"
        "buy_md_amount,buy_md_amount_rate,buy_sm_amount,buy_sm_amount_rate,buy_sm_amount_stock,rank",
        "ts_code,trade_date,start_date,end_date,content_type",
        ("ts_code", "trade_date", "content_type"),
        units={
            name: "CNY"
            for name in (
                "net_amount",
                "buy_elg_amount",
                "buy_lg_amount",
                "buy_md_amount",
                "buy_sm_amount",
            )
        }
        | {
            name: "percent"
            for name in (
                "pct_change",
                "net_amount_rate",
                "buy_elg_amount_rate",
                "buy_lg_amount_rate",
                "buy_md_amount_rate",
                "buy_sm_amount_rate",
            )
        },
        update_note="After market close; provider-specific flow, not additive across providers",
    ),
    "ths_hot": spec(
        320,
        2000,
        "trade_date,data_type,ts_code,ts_name,rank,pct_change,current_price,"
        "concept,rank_reason,hot,rank_time",
        "trade_date,ts_code,market,is_new",
        ("ts_code", "trade_date", "rank_time"),
        units={"pct_change": "percent"},
        update_note="is_new=Y updates 22:30; N stages use each record's rank_time",
    ),
    "stock_st": spec(
        397,
        1000,
        "ts_code,name,trade_date,type,type_name",
        "ts_code,trade_date,start_date,end_date",
        ("ts_code", "trade_date"),
        empty_valid=True,
        update_note="Trading day 09:20; target-day status before this remains pending",
    ),
    "suspend_d": spec(
        214,
        5000,
        "ts_code,trade_date,suspend_timing,suspend_type",
        "ts_code,trade_date,start_date,end_date,suspend_type",
        ("ts_code", "trade_date"),
        empty_valid=True,
        update_note="Irregular updates; an empty query is not a forward trading guarantee",
    ),
    "limit_list_d": spec(
        298,
        2500,
        "trade_date,ts_code,industry,name,close,pct_chg,amount,limit_amount,"
        "float_mv,total_mv,turnover_ratio,fd_amount,first_time,last_time,open_times,up_stat,limit_times,limit",
        "trade_date,ts_code,limit_type,exchange,start_date,end_date",
        ("ts_code", "trade_date", "limit"),
        empty_valid=True,
        units={"pct_chg": "percent"},
        update_note="Excludes ST; U/D/Z are separate sets; amount unit is not assumed",
    ),
    "index_classify": spec(
        181,
        None,
        "index_code,industry_name,parent_code,level,industry_code,is_pub,src",
        "index_code,level,parent_code,src",
        ("index_code", "level"),
    ),
    "index_member_all": spec(
        335,
        2000,
        "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new",
        "l1_code,l2_code,l3_code,ts_code,is_new",
        ("ts_code", "l2_code", "in_date"),
        empty_valid=True,
        update_note=(
            "Latest membership is not historical completeness; save in/out dates and first seen"
        ),
    ),
    "sw_daily": spec(
        327,
        4000,
        "ts_code,trade_date,name,open,low,high,close,change,pct_change,vol,amount,pe,pb,float_mv,total_mv",
        "ts_code,trade_date,start_date,end_date",
        ("ts_code", "trade_date", "amount"),
        units={
            "vol": "shares_10000",
            "amount": "CNY_10000",
            "float_mv": "CNY_10000",
            "total_mv": "CNY_10000",
            "pct_change": "percent",
        },
        update_note="SW2021 trading day 18:30 update",
    ),
    "kpl_list": spec(
        347,
        8000,
        "ts_code,name,trade_date,lu_time,ld_time,open_time,last_time,lu_desc,tag,theme,net_change,"
        "bid_amount,status,bid_change,bid_turnover,pct_chg,bid_pct_chg,rt_pct_chg,amount,turnover_rate",
        "ts_code,trade_date,tag,start_date,end_date",
        ("ts_code", "trade_date"),
        empty_valid=True,
        units={"net_change": "CNY", "bid_amount": "CNY", "pct_chg": "percent"},
        update_note=(
            "Next trading day 06:00; Saturday updates Friday; no exact publication timestamp"
        ),
    ),
    "kpl_concept_cons": spec(
        351,
        3000,
        "ts_code,name,con_name,con_code,trade_date,desc,hot_num",
        "trade_date,ts_code,con_code",
        ("ts_code", "con_code", "trade_date"),
        empty_valid=True,
    ),
    "moneyflow": spec(
        170,
        6000,
        "ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,"
        "buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,buy_lg_vol,buy_lg_amount,sell_lg_vol,"
        "sell_lg_amount,buy_elg_vol,buy_elg_amount,sell_elg_vol,sell_elg_amount,net_mf_vol,net_mf_amount",
        "ts_code,trade_date,start_date,end_date",
        ("ts_code", "trade_date", "net_mf_amount"),
        units={
            name: "CNY_10000"
            for name in (
                "buy_sm_amount",
                "sell_sm_amount",
                "buy_md_amount",
                "sell_md_amount",
                "buy_lg_amount",
                "sell_lg_amount",
                "buy_elg_amount",
                "sell_elg_amount",
                "net_mf_amount",
            )
        }
        | {
            name: "hands_100"
            for name in (
                "buy_sm_vol",
                "sell_sm_vol",
                "buy_md_vol",
                "sell_md_vol",
                "buy_lg_vol",
                "sell_lg_vol",
                "buy_elg_vol",
                "sell_elg_vol",
                "net_mf_vol",
            )
        },
    ),
    "top_list": spec(
        106,
        10000,
        "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_sell,l_buy,"
        "l_amount,net_amount,net_rate,amount_rate,float_values,reason",
        "trade_date,ts_code",
        ("ts_code", "trade_date", "reason"),
        empty_valid=True,
        units={
            name: "unverified_amount"
            for name in ("amount", "l_sell", "l_buy", "l_amount", "net_amount", "float_values")
        }
        | {"net_rate": "percent", "amount_rate": "percent"},
        update_note="Amount units require same-scope daily/top_inst/source-announcement crosscheck",
    ),
    "top_inst": spec(
        107,
        10000,
        "trade_date,ts_code,exalter,side,buy,buy_rate,sell,sell_rate,net_buy,reason",
        "trade_date,ts_code",
        ("ts_code", "trade_date", "side", "reason"),
        empty_valid=True,
        units={
            "buy": "CNY",
            "sell": "CNY",
            "net_buy": "CNY",
            "buy_rate": "percent",
            "sell_rate": "percent",
        },
    ),
    "share_float": spec(
        160,
        6000,
        "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type",
        "ts_code,ann_date,float_date,start_date,end_date",
        ("ts_code", "ann_date", "float_date"),
        empty_valid=True,
        units={"float_share": "shares", "float_ratio": "percent"},
    ),
    "stk_holdertrade": spec(
        175,
        3000,
        "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,"
        "after_share,after_ratio,avg_price,total_share,begin_date,close_date",
        "ts_code,ann_date,start_date,end_date,trade_type,holder_type",
        ("ts_code", "ann_date"),
        empty_valid=True,
        units={"change_ratio": "percent", "after_ratio": "percent"},
        update_note="Actual holding changes, not a complete future reduction-plan source",
    ),
    "forecast": spec(
        45,
        3500,
        "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,"
        "net_profit_max,last_parent_net,first_ann_date,summary,change_reason",
        "ts_code,ann_date,start_date,end_date,period,type",
        ("ts_code", "ann_date"),
        empty_valid=True,
        units={
            "net_profit_min": "CNY_10000",
            "net_profit_max": "CNY_10000",
            "p_change_min": "percent",
            "p_change_max": "percent",
        },
        update_note="Daily 20:00-21:00; forecasts can concern future reporting periods",
    ),
    "block_trade": spec(
        161,
        1000,
        "ts_code,trade_date,price,vol,amount,buyer,seller",
        "ts_code,trade_date,start_date,end_date",
        ("ts_code", "trade_date"),
        empty_valid=True,
        units={"vol": "shares_10000", "amount": "unverified_amount"},
        update_note="Auxiliary evidence; amount unit unverified until actual crosscheck",
    ),
}
# The documented VIP endpoint shares forecast fields/filters, with 5000-point
# cross-sectional access. It is used only for bounded recent-period batch checks.
SPECS["forecast_vip"] = SPECS["forecast"]
API_SPECS = SPECS


@dataclass
class SourceBatch:
    rows: list[dict]
    receipt: dict

    def asdict(self):
        return asdict(self)


def day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), pattern).date()
        except ValueError:
            pass
    return None


def rank_timestamp(value, trade_date):
    """Provider rank time is not interchangeable with our retrieval timestamp."""
    if value is None:
        return None
    text = str(value).strip()
    for pattern in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI)
        except ValueError:
            pass
    if trade_date:
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                clock = datetime.strptime(text, pattern).time()
                return datetime.combine(trade_date, clock, SHANGHAI)
            except ValueError:
                pass
    try:
        result = datetime.fromisoformat(text)
        return result.astimezone(SHANGHAI) if result.tzinfo else result.replace(tzinfo=SHANGHAI)
    except ValueError:
        return None


def failure_status(exc):
    """Only inspect text for classification; never save it or credentials."""
    text = str(exc).lower()
    if any(term in text for term in ("权限", "积分", "permission", "无权", "unauthorized")):
        return "permission_denied"
    return "failed"


def normalize(api, row):
    """Retain provider fields; canonical values are separate and explicitly unit tagged."""
    result = {}
    for key, unit in SPECS[api].units.items():
        value = row.get(key)
        if value is None or unit == "unverified_amount":
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        if unit in {"CNY", "CNY_10000"}:
            result[key + "_yuan"] = value * (10_000 if unit == "CNY_10000" else 1)
        elif unit in {"shares", "shares_10000", "hands_100"}:
            result[key + "_shares"] = (
                value * {"shares": 1, "shares_10000": 10_000, "hands_100": 100}[unit]
            )
        elif unit == "percent":
            result[key + "_fraction"] = value / 100
    return result


def membership_at(row, asof):
    """Membership interval only; this does not establish historical availability."""
    start, end = day(row.get("in_date")), day(row.get("out_date"))
    return start is not None and start <= asof and (end is None or asof < end)


class ArticleSources:
    def __init__(
        self,
        root: Path,
        cutoff: datetime,
        online=False,
        client=None,
        clock=None,
        *,
        max_requests=160,
        max_seconds=900,
        max_rows=120_000,
        cache_seconds=6 * 3600,
        elapsed_clock=monotonic,
    ):
        if cutoff.tzinfo is None:
            raise ValueError("Source cutoff must have a timezone")
        root = Path(root)
        if (root / ".scout-cloud").exists():
            root = guarded(root)
        elif (
            root.is_symlink()
            or not (root / ".scout-article").is_file()
            or (root / ".scout-article").read_text(encoding="utf-8") != "quantlab-scout-article-v1"
        ):
            raise ValueError("Article source writes need an explicitly marked isolated root")
        self.root = root.resolve()
        self.raw_root = self.root / "article_sources" / "raw"
        if not self.raw_root.resolve().is_relative_to(self.root):
            raise ValueError("Article source subtree points outside isolated root")
        self.cutoff = cutoff.astimezone(SHANGHAI)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.elapsed_clock = elapsed_clock
        self.started = elapsed_clock()
        self.max_requests, self.max_seconds, self.max_rows = max_requests, max_seconds, max_rows
        self.cache_seconds = cache_seconds
        if min(max_requests, max_seconds, max_rows) <= 0:
            raise ValueError("Source budgets must be positive")
        self.calls = self.row_count = 0
        self.matrix: list[dict] = []
        self.client = client
        if client is None and online and os.environ.get("TUSHARE_TOKEN"):
            import tushare as ts

            self.client = secure_tushare_factory(ts.pro_api)(
                os.environ["TUSHARE_TOKEN"], timeout=20
            )
        self._seen: dict[str, dict] = {}
        self._first_seen: dict[str, str] = {}
        for path in sorted(self.raw_root.glob("*/*.result.json")):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if fingerprint(saved.get("rows", [])) != saved.get("raw_sha256"):
                    continue
                for row, first in zip(
                    saved.get("rows", []), saved.get("row_first_seen", []), strict=True
                ):
                    key = fingerprint([saved["api"], row])
                    self._first_seen[key] = min(first, self._first_seen.get(key, first))
            except (OSError, KeyError, ValueError, TypeError):
                continue

    def _write(self, path, value):
        if not path.parent.resolve().is_relative_to(self.root):
            raise ValueError("Raw source path escapes isolated root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False)

    def _enrich(self, api, saved, expected_dates, freshness_required):
        spec = SPECS[api]
        retrieved = timestamp(saved["fetched_at"])
        problems, rows, data_dates = [], [], set()
        for ordinal, (row, first) in enumerate(
            zip(saved["rows"], saved["row_first_seen"], strict=True)
        ):
            if any(key not in row or row[key] is None for key in spec.required):
                problems.append("missing_required_fields")
                continue
            if any(
                key in saved["params"]
                and str(row.get(key)) not in str(saved["params"][key]).split(",")
                for key in ("ts_code", "con_code")
            ):
                problems.append("requested_subject_mismatch")
                continue
            data_date = day(row.get("trade_date"))
            if data_date:
                data_dates.add(data_date)
            if freshness_required and expected_dates and data_date not in expected_dates:
                problems.append("unexpected_data_date")
                continue
            if api == "share_float":
                float_day = day(row.get("float_date"))
                start = day(saved["params"].get("start_date"))
                end = day(saved["params"].get("end_date"))
                exact = day(saved["params"].get("float_date"))
                if (
                    float_day is None
                    or (start is not None and float_day < start)
                    or (end is not None and float_day > end)
                    or (exact is not None and float_day != exact)
                ):
                    problems.append("requested_unlock_date_mismatch")
                    continue
            public = rank_timestamp(row.get("rank_time"), data_date) if api == "ths_hot" else None
            ann_day = day(row.get("ann_date"))
            if api == "ths_hot" and (
                public is None
                or public > self.cutoff
                or public > retrieved
                or public.date() != data_date
            ):
                problems.append("rank_time_unavailable_or_future")
                continue
            if ann_day and ann_day > self.cutoff.date():
                problems.append("announcement_after_cutoff")
                continue
            if timestamp(first) > self.cutoff or retrieved > self.cutoff:
                problems.append("retrieved_after_cutoff")
                continue
            normalized = normalize(api, row)
            rows.append(
                row
                | {
                    "normalized": normalized,
                    "_source_id": fingerprint([api, row, first, ordinal]),
                    "_row_ordinal": ordinal,
                    "_first_seen_at": first,
                    "_fetched_at": saved["fetched_at"],
                    "_data_date": data_date.isoformat() if data_date else None,
                    "_published_at": public.isoformat() if public else None,
                    "_published_date": ann_day.isoformat() if ann_day else None,
                    "_publication_time_status": "exact_rank_time"
                    if public
                    else "date_only_or_unknown",
                    "_units_verified": not any(
                        unit == "unverified_amount" for unit in spec.units.values()
                    ),
                    "_historical_availability": "not_reconstructed_from_data_date",
                }
            )
        if freshness_required and expected_dates and not expected_dates.issubset(data_dates):
            # Empty event lists describe a completed query, not missing daily observations.
            if not (spec.empty_valid and not saved["rows"]):
                problems.append("missing_expected_dates")
        status = saved["status"]
        if status == "available" and problems:
            status = "partial" if rows else "delayed"
        receipt = {
            key: value for key, value in saved.items() if key not in {"rows", "row_first_seen"}
        }
        receipt.update(
            status=status,
            valid_count=len(rows),
            expected_dates=[d.isoformat() for d in sorted(expected_dates)],
            data_dates=[d.isoformat() for d in sorted(data_dates)],
            reasons=sorted(set(problems)),
            known_at_cutoff=retrieved <= self.cutoff,
            empty_response=not saved["rows"],
            empty_semantics="queried_no_records" if spec.empty_valid else "dense_source_missing",
        )
        return SourceBatch(rows, receipt)

    def fetch(
        self,
        api,
        params=None,
        *,
        expected_date=None,
        expected_dates=(),
        scope="request",
        freshness_required=True,
    ):
        if api not in SPECS:
            raise ValueError("Unsupported article source API")
        params = dict(params or {})
        spec = SPECS[api]
        if set(params) - spec.params:
            raise ValueError("Undocumented API parameters, including pagination, are prohibited")
        if api == "dc_index" and "idx_type" not in params:
            raise ValueError("DC directory requires explicit classification type")
        expected = {day(item) for item in expected_dates}
        if expected_date:
            expected.add(day(expected_date))
        if None in expected:
            raise ValueError("Invalid expected data date")
        identity = fingerprint([SOURCE_VERSION, api, params, spec.fields])
        now = self.clock().astimezone(SHANGHAI)
        if identity in self._seen:
            batch = self._enrich(api, self._seen[identity], expected, freshness_required)
            batch.receipt["cache_reused"] = True
            if batch.receipt["status"] in {"failed", "permission_denied"}:
                batch.rows = []
                batch.receipt["valid_count"] = 0
            self.matrix.append(batch.receipt)
            return batch
        folder = self.raw_root / identity
        for path in sorted(folder.glob("*.result.json"), reverse=True):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = (now - timestamp(saved["fetched_at"])).total_seconds()
                if fingerprint(saved.get("rows", [])) != saved.get("raw_sha256"):
                    continue
                if (
                    saved["status"] in {"available", "partial"}
                    and 0 <= age <= self.cache_seconds
                    and timestamp(saved["fetched_at"]) <= self.cutoff
                ):
                    batch = self._enrich(api, saved, expected, freshness_required)
                    batch.receipt["cache_reused"] = True
                    self._seen[identity] = saved
                    self.matrix.append(batch.receipt)
                    return batch
            except (OSError, KeyError, ValueError, TypeError):
                continue
        record = {
            "version": SOURCE_VERSION,
            "request_id": uuid4().hex,
            "api": api,
            "params": params,
            "fields": spec.fields.split(","),
            "scope": scope,
            "source_url": spec.url,
            "units": spec.units,
            "update_note": spec.update_note,
            "row_cap": spec.cap,
            "cutoff_at": self.cutoff.isoformat(),
            "requested_at": now.isoformat(),
            "first_seen_at": None,
            "fetched_at": now.isoformat(),
            "row_count": 0,
            "truncated": False,
            "coverage_mode": scope,
            "cache_reused": False,
            "status": "failed",
            "rows": [],
            "row_first_seen": [],
        }
        path = folder / f"{now:%Y%m%dT%H%M%S%f}-{record['request_id']}.result.json"
        record["artifact_ref"] = path.relative_to(self.root).as_posix()
        if self.client is None:
            record["reason"] = "offline_or_no_credentials"
        elif now > self.cutoff:
            record["reason"] = "source_cutoff_passed"
        elif (
            self.calls >= self.max_requests
            or self.row_count >= self.max_rows
            or self.elapsed_clock() - self.started >= self.max_seconds
        ):
            record["reason"] = "source_budget_exhausted"
        else:
            self._write(
                path.with_name(path.name.removesuffix(".result.json") + ".intent.json"),
                {key: val for key, val in record.items() if key not in {"rows", "row_first_seen"}}
                | {"status": "delivery_unknown"},
            )
            self.calls += 1
            try:
                frame = self.client.query(api, fields=spec.fields, **params)
                raw = json.loads(frame.to_json(orient="records", force_ascii=False))
                fetched = self.clock().astimezone(SHANGHAI)
                record["fetched_at"] = fetched.isoformat()
                record["first_seen_at"] = fetched.isoformat()
                record["fields"] = list(frame.columns)
                record["row_count"] = len(raw)
                self.row_count += len(raw)
                record["rows"] = raw
                record["row_first_seen"] = [
                    self._first_seen.setdefault(fingerprint([api, row]), fetched.isoformat())
                    for row in raw
                ]
                record["truncated"] = bool(spec.cap and len(raw) >= spec.cap)
                record["status"] = "partial" if record["truncated"] else "available"
                if len(raw) > self.max_rows or self.row_count > self.max_rows:
                    record.update(status="partial", reason="source_row_budget_exceeded")
                if self.elapsed_clock() - self.started >= self.max_seconds:
                    record.update(status="failed", reason="source_time_budget_exceeded")
                if not raw and not spec.empty_valid:
                    record.update(status="delayed", reason="dense_source_returned_empty")
                if (
                    api == "stock_st"
                    and expected
                    and max(expected) >= fetched.date()
                    and fetched.time() < time(9, 20)
                ):
                    record.update(status="delayed", reason="target_status_before_documented_update")
                if api in {"stock_st", "suspend_d"} and expected and max(expected) > fetched.date():
                    record.update(status="delayed", reason="future_target_state_not_available")
                if (
                    api == "sw_daily"
                    and expected
                    and max(expected) >= fetched.date()
                    and fetched.time() < time(18, 30)
                ):
                    record.update(status="delayed", reason="industry_before_documented_update")
                if (
                    api in {"top_list", "top_inst", "limit_list_d", "moneyflow_ind_dc"}
                    and expected
                    and max(expected) >= fetched.date()
                    and fetched.time() < time(15)
                ):
                    record.update(status="delayed", reason="completed_session_not_yet_available")
                if (
                    api == "kpl_list"
                    and expected
                    and max(expected) + timedelta(days=1) >= fetched.date()
                    and fetched.time() < time(6)
                ):
                    record.update(status="delayed", reason="KPL_before_documented_next_day_update")
            except Exception as exc:
                record.update(status=failure_status(exc), error_type=type(exc).__name__)
        record["raw_sha256"] = fingerprint(record["rows"])
        self._write(path, record)
        batch = self._enrich(api, record, expected, freshness_required)
        if batch.receipt["status"] in {"failed", "permission_denied"}:
            batch.rows = []
            batch.receipt["valid_count"] = 0
        self._seen[identity] = record
        self.matrix.append(batch.receipt)
        return batch

    def report(self, records, *, coverage_mode, extra=None):
        mandatory = {
            api: [item["status"] for item in self.matrix if item["api"] == api]
            for api in MANDATORY_6000
        }
        degraded = any(
            not statuses or any(status != "available" for status in statuses)
            for statuses in mandatory.values()
        )
        return {
            "source_version": SOURCE_VERSION,
            "config": DEFAULT_SOURCE_CONFIG
            | {
                "max_requests": self.max_requests,
                "max_seconds": self.max_seconds,
                "max_rows": self.max_rows,
                "cache_seconds": self.cache_seconds,
            },
            "mode": "degraded" if degraded else "tushare_6000",
            "coverage_mode": coverage_mode,
            "records": records,
            "coverage": list(self.matrix),
            "mandatory_6000": mandatory,
            "costs": {
                "requests": self.calls,
                "rows": self.row_count,
                "elapsed_seconds": round(self.elapsed_clock() - self.started, 3),
                "max_requests": self.max_requests,
                "max_seconds": self.max_seconds,
                "max_rows": self.max_rows,
            },
            "source_ids": sorted({row["_source_id"] for rows in records.values() for row in rows}),
            **(extra or {}),
        }

    def collect_basic_context(self, signal_date, target_date=None):
        records = {}
        for api in ("stock_st", "suspend_d"):
            params = {"trade_date": signal_date.strftime("%Y%m%d")}
            if api == "suspend_d":
                params["suspend_type"] = "S"
            records[api] = self.fetch(
                api, params, expected_date=signal_date, scope="whole_signal_day"
            ).rows
            if target_date:
                target = self.fetch(
                    api,
                    params | {"trade_date": target_date.strftime("%Y%m%d")},
                    expected_date=target_date,
                    scope="target_state_only",
                )
                records[api + "_target"] = target.rows
        return self.report(records, coverage_mode="basic_signal_state")

    def probe_6000(self, signal_date):
        """Five real adapter calls, but not an assertion of full theme coverage."""
        records = {}
        directory = self.fetch(
            "dc_index",
            {"trade_date": signal_date.strftime("%Y%m%d"), "idx_type": "概念板块"},
            expected_date=signal_date,
            scope="permission_probe_directory",
        )
        records["dc_index"] = directory.rows
        member_params = {"trade_date": signal_date.strftime("%Y%m%d")}
        if directory.rows:
            member_params["ts_code"] = sorted(row["ts_code"] for row in directory.rows)[0]
        records["dc_member"] = self.fetch(
            "dc_member",
            member_params,
            expected_date=signal_date,
            scope="permission_probe_one_group",
        ).rows
        records["dc_daily"] = self.fetch(
            "dc_daily",
            {"trade_date": signal_date.strftime("%Y%m%d"), "idx_type": "概念板块"},
            expected_date=signal_date,
            scope="permission_probe_day",
        ).rows
        records["moneyflow_ind_dc"] = self.fetch(
            "moneyflow_ind_dc",
            {"trade_date": signal_date.strftime("%Y%m%d"), "content_type": "概念"},
            expected_date=signal_date,
            scope="permission_probe_day",
        ).rows
        records["ths_hot"] = self.fetch(
            "ths_hot",
            {"trade_date": signal_date.strftime("%Y%m%d"), "market": "热股", "is_new": "N"},
            expected_date=signal_date,
            scope="permission_probe_staged_heat",
        ).rows
        return self.report(records, coverage_mode="permission_probe_only")

    def collect_direction_sources(
        self,
        signal_date,
        sessions,
        candidate_codes=None,
        *,
        group_ids=None,
        member_request_budget=80,
        reserve_requests=0,
    ):
        """Fetch shared context, then budget members with downstream calls reserved.

        The caller's member budget is only an upper bound: shared SW/DC calls
        must finish before computing the actual member-stage allowance.
        """
        for name, value in (
            ("member_request_budget", member_request_budget),
            ("reserve_requests", reserve_requests),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(name + " must be a nonnegative integer")
        if reserve_requests > self.max_requests:
            raise ValueError("Reserved requests exceed the fixed total request budget")
        dates = sorted({day(item) for item in sessions if day(item) and day(item) <= signal_date})[
            -6:
        ]
        records = {
            api: []
            for api in (
                "dc_index",
                "dc_member",
                "dc_daily",
                "moneyflow_ind_dc",
                "ths_hot",
                "sw_daily",
                "index_classify",
                "index_member_all",
                "kpl_list",
                "kpl_concept_cons",
                "limit_list_d",
            )
        }

        def add(api, params, data_day=None, scope="whole_day", fresh=True):
            batch = self.fetch(
                api, params, expected_date=data_day, scope=scope, freshness_required=fresh
            )
            records[api].extend(batch.rows)
            return batch

        for kind in DEFAULT_SOURCE_CONFIG["dc_types"]:
            add(
                "dc_index",
                {"trade_date": signal_date.strftime("%Y%m%d"), "idx_type": kind},
                signal_date,
                "full_directory:" + kind,
            )
        for current in dates:
            for kind in DEFAULT_SOURCE_CONFIG["dc_types"]:
                add(
                    "dc_daily",
                    {"trade_date": current.strftime("%Y%m%d"), "idx_type": kind},
                    current,
                )
            for kind in ("行业", "概念"):
                add(
                    "moneyflow_ind_dc",
                    {"trade_date": current.strftime("%Y%m%d"), "content_type": kind},
                    current,
                )
            add("sw_daily", {"trade_date": current.strftime("%Y%m%d")}, current)
        for market in DEFAULT_SOURCE_CONFIG["ths_markets"]:
            add(
                "ths_hot",
                {"trade_date": signal_date.strftime("%Y%m%d"), "market": market, "is_new": "N"},
                signal_date,
                "heat:" + market,
            )
        # Shared daily KPL/limit response gets retained independently from member completeness.
        for tag in ("涨停", "跌停", "炸板"):
            add("kpl_list", {"trade_date": signal_date.strftime("%Y%m%d"), "tag": tag}, signal_date)
        for limit in ("U", "D", "Z"):
            add(
                "limit_list_d",
                {"trade_date": signal_date.strftime("%Y%m%d"), "limit_type": limit},
                signal_date,
            )
        for level in ("L1", "L2"):
            add(
                "index_classify",
                {"src": "SW2021", "level": level},
                fresh=False,
                scope="SW2021_directory",
            )
        directory_ids = sorted({row["ts_code"] for row in records["dc_index"]})
        requested_ids = sorted(set(group_ids or directory_ids))
        if set(requested_ids) - set(directory_ids):
            raise ValueError("Requested DC group is not in the fetched directory")
        # Complete the documented industry fallback before spending the residual
        # budget on hundreds of concept member partitions.
        for row in sorted(records["index_classify"], key=lambda item: item["index_code"]):
            if row.get("level") != "L1":
                continue
            for latest in ("Y", "N"):
                add(
                    "index_member_all",
                    {"l1_code": row["index_code"], "is_new": latest},
                    fresh=False,
                    scope="SW2021_members:" + latest,
                )
        member_batches = []
        # A global response may be complete. If capped, discard it as a group denominator.
        global_members = add(
            "dc_member",
            {"trade_date": signal_date.strftime("%Y%m%d")},
            signal_date,
            "all_directory_members_attempt",
        )
        complete_global = global_members.receipt["status"] == "available" and bool(
            global_members.rows
        )
        completed_ids = (
            {row["ts_code"] for row in global_members.rows} if complete_global else set()
        )
        member_stage_start = self.calls
        tail_requests = 1  # KPL shared membership attempt follows DC membership.
        allowance = min(
            member_request_budget,
            max(0, self.max_requests - self.calls - reserve_requests - tail_requests),
        )
        if not complete_global:
            records["dc_member"] = []
            for code in requested_ids[:allowance]:
                if self.max_requests - self.calls <= reserve_requests + tail_requests:
                    break
                batch = add(
                    "dc_member",
                    {"trade_date": signal_date.strftime("%Y%m%d"), "ts_code": code},
                    signal_date,
                    "full_group_members:" + code,
                )
                member_batches.append(batch.receipt)
                if batch.receipt["status"] == "available" and batch.rows:
                    completed_ids.add(code)
            if candidate_codes:
                # Supplement discovery only; candidate-targeted links cannot prove full groups.
                remaining = min(
                    max(0, allowance - len(member_batches)),
                    max(0, self.max_requests - self.calls - reserve_requests - tail_requests),
                )
                for code in sorted(set(candidate_codes))[:remaining]:
                    if self.max_requests - self.calls <= reserve_requests + tail_requests:
                        break
                    add(
                        "dc_member",
                        {"trade_date": signal_date.strftime("%Y%m%d"), "con_code": code},
                        signal_date,
                        "candidate_targeted_members:" + code,
                    )
        kpl = None
        if self.max_requests - self.calls > reserve_requests:
            kpl = add(
                "kpl_concept_cons",
                {"trade_date": signal_date.strftime("%Y%m%d")},
                signal_date,
                "KPL_members_attempt",
            )
        if kpl is not None and kpl.receipt["truncated"] and candidate_codes:
            remaining = min(20, max(0, self.max_requests - self.calls - reserve_requests))
            for code in sorted(set(candidate_codes))[:remaining]:
                if self.max_requests - self.calls <= reserve_requests:
                    break
                add(
                    "kpl_concept_cons",
                    {"trade_date": signal_date.strftime("%Y%m%d"), "con_code": code},
                    signal_date,
                    "KPL_candidate_targeted:" + code,
                )
        missing = sorted(set(requested_ids) - completed_ids)
        result = self.report(
            records,
            coverage_mode="full_directory_bounded_members",
            extra={
                "membership": {
                    "directory_count": len(directory_ids),
                    "requested_count": len(requested_ids),
                    "completed_group_ids": sorted(completed_ids),
                    "unverified_group_ids": missing,
                    "full_directory_membership": not missing and bool(directory_ids),
                    "scope": "all_directory" if group_ids is None else "explicit_research_groups",
                    "history_note": (
                        "Membership is a signal-day snapshot; persistence requires genuine "
                        "earlier available snapshots"
                    ),
                    "budget": {
                        "requested_member_budget": member_request_budget,
                        "actual_member_allowance": allowance,
                        "calls_before_member_stage": member_stage_start,
                        "reserved_downstream_requests": reserve_requests,
                        "remaining_total_requests": max(0, self.max_requests - self.calls),
                        "skipped_group_count": max(0, len(requested_ids) - len(member_batches))
                        if not complete_global
                        else 0,
                    },
                },
                "fallback": {
                    "industry": "SW2021",
                    "kpl": "actual_snapshot_only",
                    "kpl_membership_status": kpl.receipt["status"]
                    if kpl is not None
                    else "not_queried_reserved_budget",
                    "degraded": bool(missing),
                },
            },
        )
        if missing:
            result["mode"] = "degraded"
        return result

    def collect_candidate_checks(
        self, signal_date, sessions, codes, *, target_sessions=(), include_block_trade=False
    ):
        """Check all routed shape stocks before AI budget allocation, not only the final 24."""
        dates = sorted({day(item) for item in sessions if day(item) and day(item) <= signal_date})[
            -5:
        ]
        codes = sorted(set(codes))
        records = {
            api: []
            for api in (
                "moneyflow",
                "top_list",
                "top_inst",
                "share_float",
                "stk_holdertrade",
                "forecast",
                "forecast_vip",
                "block_trade",
            )
        }

        def add(api, params, expected=(), scope="candidate_checks", fresh=True):
            batch = self.fetch(
                api, params, expected_dates=expected, scope=scope, freshness_required=fresh
            )
            records[api].extend(batch.rows)
            return batch

        risk_leaves = defaultdict(list)

        def risk_range(api, range_start, range_end):
            """Only documented date partitions; a capped single day is still partial."""
            batch = self.fetch(
                api,
                {
                    "start_date": range_start.strftime("%Y%m%d"),
                    "end_date": range_end.strftime("%Y%m%d"),
                },
                scope="shared_risk_date_window",
                freshness_required=False,
            )
            if batch.receipt["truncated"] and range_start < range_end:
                middle = range_start + timedelta(days=(range_end - range_start).days // 2)
                return risk_range(api, range_start, middle) + risk_range(
                    api, middle + timedelta(days=1), range_end
                )
            risk_leaves[api].append(batch.receipt)
            return batch.rows

        for current in dates:
            for api in ("top_list", "top_inst"):
                add(
                    api,
                    {"trade_date": current.strftime("%Y%m%d")},
                    (current,),
                    "shared_whole_day_events",
                )
        if not dates:
            return self.report(records, coverage_mode="no_required_trading_window")
        # Five shared daily requests cover all routed shape stocks, before the
        # deep-study quota. Targeted splits are needed only on proven truncation.
        for current in dates:
            batch = self.fetch(
                "moneyflow",
                {"trade_date": current.strftime("%Y%m%d")},
                expected_date=current,
                scope="shared_whole_day_moneyflow",
            )
            if batch.receipt["truncated"]:
                for code in codes:
                    add(
                        "moneyflow",
                        {"trade_date": current.strftime("%Y%m%d"), "ts_code": code},
                        (current,),
                        "capped_day_stock_split:" + code,
                    )
            else:
                records["moneyflow"].extend(batch.rows)
        future = sorted(
            {day(item) for item in target_sessions if day(item) and day(item) > signal_date}
        )[:5]
        risk_end = future[-1] if future else signal_date + timedelta(days=10)
        unlock_start = future[0] if future else signal_date
        ann_start = self.cutoff.date() - timedelta(days=30)
        records["share_float"].extend(risk_range("share_float", unlock_start, risk_end))
        unlock_global_coverage = {
            current
            for receipt in risk_leaves["share_float"]
            if receipt["status"] == "available"
            and not receipt["truncated"]
            and receipt.get("valid_count") == receipt.get("row_count")
            for current in future
            if day(receipt["params"]["start_date"]) <= current <= day(receipt["params"]["end_date"])
        }
        global_unlock_complete = bool(future) and set(future) <= unlock_global_coverage
        targeted_unlocks = {}
        if future and not global_unlock_complete:
            for code in codes:
                if (
                    self.calls >= self.max_requests
                    or self.row_count >= self.max_rows
                    or self.elapsed_clock() - self.started >= self.max_seconds
                ):
                    targeted_unlocks[code] = {
                        "status": "failed",
                        "reason": "targeted_unlock_not_queried_fixed_budget_exhausted",
                    }
                    continue
                batch = self.fetch(
                    "share_float",
                    {
                        "ts_code": code,
                        "start_date": unlock_start.strftime("%Y%m%d"),
                        "end_date": risk_end.strftime("%Y%m%d"),
                    },
                    scope="capped_or_unknown_unlock_stock_split:" + code,
                    freshness_required=False,
                )
                targeted_unlocks[code] = batch.receipt
                if (
                    batch.receipt["status"] == "available"
                    and not batch.receipt["truncated"]
                    and batch.receipt.get("valid_count") == batch.receipt.get("row_count")
                ):
                    # The complete stock-window response supersedes its incomplete
                    # global subset, avoiding duplicated holder/event amounts.
                    records["share_float"] = [
                        row for row in records["share_float"] if row.get("ts_code") != code
                    ] + batch.rows
                else:
                    existing = {
                        fingerprint(
                            {key: value for key, value in row.items() if not key.startswith("_")}
                        )
                        for row in records["share_float"]
                    }
                    records["share_float"].extend(
                        row
                        for row in batch.rows
                        if fingerprint(
                            {key: value for key, value in row.items() if not key.startswith("_")}
                        )
                        not in existing
                    )
        records["stk_holdertrade"].extend(
            risk_range("stk_holdertrade", ann_start, self.cutoff.date())
        )
        quarters = sorted(
            date(year, month, last)
            for year in range(self.cutoff.year - 2, self.cutoff.year + 2)
            for month, last in ((3, 31), (6, 30), (9, 30), (12, 31))
        )
        past = [current for current in quarters if current <= self.cutoff.date()][-3:]
        upcoming = [current for current in quarters if current > self.cutoff.date()][:2]
        forecast_periods = past + upcoming
        for period in forecast_periods:
            batch = self.fetch(
                "forecast_vip",
                {
                    "period": period.strftime("%Y%m%d"),
                    "start_date": ann_start.strftime("%Y%m%d"),
                    "end_date": self.cutoff.strftime("%Y%m%d"),
                },
                scope="shared_recent_forecast_period:" + period.isoformat(),
                freshness_required=False,
            )
            if batch.receipt["truncated"]:
                for code in codes:
                    add(
                        "forecast",
                        {
                            "ts_code": code,
                            "period": period.strftime("%Y%m%d"),
                            "start_date": ann_start.strftime("%Y%m%d"),
                            "end_date": self.cutoff.strftime("%Y%m%d"),
                        },
                        scope="capped_forecast_stock_split:" + code,
                        fresh=False,
                    )
            else:
                records["forecast_vip"].extend(batch.rows)
        if include_block_trade:
            records["block_trade"].extend(risk_range("block_trade", dates[0], signal_date))
        wanted = set(codes)
        records = {
            api: [row for row in rows if row.get("ts_code") in wanted]
            for api, rows in records.items()
        }
        # Keep convenient forecast compatibility, with original API source IDs.
        records["forecast"].extend(records["forecast_vip"])
        coverage_by_code = {}
        for code in codes:
            observed = [
                day(row["trade_date"]) for row in records["moneyflow"] if row["ts_code"] == code
            ]
            coverage_by_code[code] = {
                "moneyflow": {
                    "status": "available"
                    if len(dates) == 5 and sorted(observed) == dates
                    else "partial",
                    "expected_dates": [current.isoformat() for current in dates],
                    "valid_dates": sorted({current.isoformat() for current in observed}),
                },
                "share_float": {
                    "status": (
                        "partial"
                        if len(future) != 5
                        else "available"
                        if global_unlock_complete
                        or targeted_unlocks.get(code, {}).get("status") == "available"
                        else targeted_unlocks.get(code, {}).get("status", "partial")
                    ),
                    "coverage_mode": "complete_global_unlock_window"
                    if global_unlock_complete
                    else "targeted_stock_unlock_window"
                    if code in targeted_unlocks
                    else "required_calendar_incomplete",
                    "expected_dates": [current.isoformat() for current in future],
                    "start_date": unlock_start.isoformat(),
                    "end_date": risk_end.isoformat(),
                    "global_complete_dates": [
                        current.isoformat() for current in sorted(unlock_global_coverage)
                    ],
                    "request_id": targeted_unlocks.get(code, {}).get("request_id"),
                    "reason": targeted_unlocks.get(code, {}).get("reason")
                    or ("required_calendar_incomplete" if len(future) != 5 else None),
                    "row_count": sum(row["ts_code"] == code for row in records["share_float"]),
                },
                "risk_note": (
                    "Source-specific checks; does not replace full announcement risk review"
                ),
            }
        return self.report(
            records,
            coverage_mode="all_supplied_shape_candidates",
            extra={
                "requested_candidate_codes": codes,
                "required_moneyflow_sessions": [current.isoformat() for current in dates],
                "target_unlock_sessions": [current.isoformat() for current in future],
                "target_unlock_window_status": (
                    "calendar_complete" if len(future) == 5 else "required_calendar_incomplete"
                ),
                "candidate_coverage": coverage_by_code,
                "forecast_periods": [current.isoformat() for current in forecast_periods],
                "forecast_coverage_mode": "five_explicit_reporting_periods_not_all_future_plans",
            },
        )
