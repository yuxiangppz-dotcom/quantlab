"""Bounded TuShare research packs with immutable raw snapshots and explicit coverage."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic
from uuid import uuid4

from quantlab.scout.models import SHANGHAI, Coverage, Evidence, fingerprint, timestamp

DOC_IDS = {
    "limit_list_d": 298,
    "kpl_list": 347,
    "kpl_concept_cons": 351,
    "forecast_vip": 45,
    "express_vip": 46,
    "repurchase": 124,
    "stk_holdertrade": 175,
    "share_float": 160,
    "suspend_d": 214,
    "moneyflow": 170,
    "fina_indicator": 79,
    "fina_mainbz": 81,
    "disclosure_date": 162,
    "index_classify": 181,
    "index_member_all": 335,
}
ROW_CAPS = {
    "limit_list_d": 2500,
    "kpl_list": 8000,
    "kpl_concept_cons": 3000,
    "forecast_vip": 3500,
    "stk_holdertrade": 3000,
    "share_float": 6000,
    "index_member_all": 2000,
}
EVENT_APIS = {"forecast_vip", "express_vip", "repurchase", "stk_holdertrade"}


def report_periods(asof: date, count: int = 7, include_next: bool = False) -> list[str]:
    """Recent quarter ends; forecasts may be published before the period ends."""
    year = asof.year
    periods = [
        date(y, m, d)
        for y in range(year - 2, year + 2)
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
    ]
    past = sorted((day for day in periods if day <= asof), reverse=True)[:count]
    if include_next:
        upcoming = min(day for day in periods if day > asof)
        return [upcoming.strftime("%Y%m%d")] + [day.strftime("%Y%m%d") for day in past]
    return [day.strftime("%Y%m%d") for day in past]


def ymd(value: object) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return None


def public_time(row: dict, key: str, cutoff: datetime) -> bool:
    """Date-only publication can be used after retrieval, never backdated."""
    day = ymd(row.get(key))
    return day is not None and day <= cutoff.date()


def source_status(exc: Exception) -> str:
    message = str(exc).lower()
    if any(word in message for word in ("权限", "积分", "permission")):
        return "permission_denied"
    if any(word in message for word in ("频次", "限流", "rate limit")):
        return "rate_limited"
    if any(word in message for word in ("timeout", "connection", "network", "ssl")):
        return "network_error"
    return "provider_error"


class TusharePack:
    def __init__(self, root: Path, now: datetime, online: bool):
        self.root = root
        self.now = now
        self.online = online
        self.matrix: list[dict] = []
        self.row_snapshots: dict[str, str] = {}
        self.client = None
        if online and os.environ.get("TUSHARE_TOKEN"):
            import tushare as ts

            self.client = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20)

    def fetch(self, api: str, params: dict) -> list[dict]:
        """One request or a recent immutable cache. Never expose provider errors or token."""
        key = fingerprint([api, params])[:16]
        prefix = f"{api}_{key}_"
        fetched_at = datetime.now(SHANGHAI)
        if self.client is None:
            self.matrix.append({"api": api, "params": params, "status": "disabled_no_token"})
            return []
        for path in sorted(self.root.glob(prefix + "*.json"), reverse=True):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = (fetched_at - timestamp(saved["retrieved_at"])).total_seconds()
                if 0 <= age <= 6 * 3600 and saved["status"] in {
                    "data",
                    "empty_unconfirmed",
                    "possibly_truncated",
                }:
                    self.matrix.append(
                        {
                            "api": api,
                            "params": params,
                            "status": "cached_" + saved["status"],
                            "rows": len(saved["rows"]),
                            "fields": saved["fields"],
                            "snapshot": str(path),
                            "retrieved_at": saved["retrieved_at"],
                            "elapsed_ms": 0,
                        }
                    )
                    rows = saved["rows"]
                    self.row_snapshots.update((fingerprint([api, row]), str(path)) for row in rows)
                    return rows
            except (OSError, KeyError, ValueError, TypeError):
                continue
        started = monotonic()
        try:
            frame = getattr(self.client, api)(**params)
            rows = json.loads(frame.to_json(orient="records", force_ascii=False))
            cap = ROW_CAPS.get(api)
            status = (
                "possibly_truncated"
                if cap and len(rows) >= cap
                else ("data" if rows else "empty_unconfirmed")
            )
            path = self.root / f"{prefix}{fetched_at:%Y%m%dT%H%M%S%f}-{uuid4().hex[:6]}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "api": api,
                "params": params,
                "retrieved_at": fetched_at.isoformat(),
                "status": status,
                "fields": list(frame.columns),
                "rows": rows,
            }
            with path.open("x", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, allow_nan=False)
            self.row_snapshots.update((fingerprint([api, row]), str(path)) for row in rows)
            self.matrix.append(
                {
                    "api": api,
                    "params": params,
                    "status": status,
                    "rows": len(rows),
                    "fields": list(frame.columns),
                    "snapshot": str(path),
                    "retrieved_at": fetched_at.isoformat(),
                    "elapsed_ms": round((monotonic() - started) * 1000),
                }
            )
            return rows
        except Exception as exc:
            self.matrix.append(
                {
                    "api": api,
                    "params": params,
                    "status": source_status(exc),
                    "rows": None,
                    "fields": [],
                    "elapsed_ms": round((monotonic() - started) * 1000),
                }
            )
            return []

    def coverage(self) -> list[Coverage]:
        return [
            Coverage(
                "tushare:" + item["api"],
                item["status"],
                item.get("rows") or 0,
                f"params={item['params']}; snapshot={item.get('snapshot') or 'none'}; "
                f"elapsed_ms={item.get('elapsed_ms', 0)}",
            )
            for item in self.matrix
        ]

    def evidence(
        self,
        api: str,
        row: dict,
        code: str,
        title: str,
        kind: str,
        fields: tuple[str, ...],
        event_key: str | None = None,
        snapshot_refs: tuple[str, ...] = (),
    ) -> Evidence:
        body = {key: row.get(key) for key in fields if key in row}
        body["unit_note"] = {
            "forecast_vip": "net_profit_min/max in CNY 10,000; change percentages in percent",
            "express_vip": "revenue/profit/assets in CNY yuan",
            "moneyflow": "net_mf_amount in CNY 10,000, not institution account flow",
            "share_float": "float_share in shares; float_ratio percent of total share capital",
            "fina_mainbz": "bz_sales/profit/cost in stated curr_type; dimensions cannot be summed",
        }.get(api, "provider fields; unknown values remain null")
        event = ymd(row.get(event_key)) if event_key else None
        snapshot = self.row_snapshots.get(fingerprint([api, row]))
        return Evidence(
            source="tushare:" + api,
            title=title,
            body=json.dumps(body, ensure_ascii=False, allow_nan=False),
            url="https://tushare.pro/document/2?doc_id=" + str(DOC_IDS[api]),
            published_at=None,  # Announcement day is not an exact timestamp.
            retrieved_at=datetime.now(SHANGHAI).isoformat(),
            kind=kind,
            instrument_ids=(code,),
            event_dates=(event.isoformat(),) if event else (),
            snapshot_refs=snapshot_refs or ((snapshot,) if snapshot else ()),
        )


def fetch_unlock_window(
    pack: TusharePack, start: date, end: date, code: str | None = None
) -> list[dict]:
    """Split capped date ranges; a capped single day remains explicitly incomplete."""
    params = {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    if code:
        params["ts_code"] = code
    rows = pack.fetch("share_float", params)
    if len(rows) < ROW_CAPS["share_float"] or start >= end:
        return rows
    middle = start + timedelta(days=(end - start).days // 2)
    return fetch_unlock_window(pack, start, middle, code) + fetch_unlock_window(
        pack, middle + timedelta(days=1), end, code
    )


def collect_market_pack(
    pack: TusharePack,
    session: date,
    recent_sessions: list[date],
    valid_codes: set[str],
    names: dict[str, str],
) -> tuple[list[Evidence], list[dict], dict[str, dict]]:
    """Collect new events, theme expansion, historical limit shape, risk and flow."""
    evidence: list[Evidence] = []
    leads: list[dict] = []
    context: dict[str, dict] = {}
    session_text = session.strftime("%Y%m%d")
    cutoff = pack.now
    newest = cutoff.date() - timedelta(days=4)

    def add_event(api: str, row: dict, title: str, fields: tuple[str, ...]) -> None:
        code = str(row.get("ts_code") or "")
        if code not in valid_codes or not public_time(row, "ann_date", cutoff):
            return
        item = pack.evidence(api, row, code, title, "company_event_date_only", fields, "ann_date")
        evidence.append(item)
        event_day = ymd(row.get("ann_date"))
        if event_day and event_day >= newest:
            leads.append(
                {
                    "instrument_ids": [code],
                    "relation": "new_company_event_date_only",
                    "route_type": "event",
                    "event_date": event_day.isoformat(),
                    "event_source": api,
                    "summary": title,
                    "evidence_ids": [item.evidence_id],
                }
            )

    for day in recent_sessions[-5:]:
        for row in pack.fetch("limit_list_d", {"trade_date": day.strftime("%Y%m%d")}):
            code = str(row.get("ts_code") or "")
            if code in valid_codes and ymd(row.get("trade_date")) == day:
                evidence.append(
                    pack.evidence(
                        "limit_list_d",
                        row,
                        code,
                        f"历史涨跌停/炸板结构 {day.isoformat()}",
                        "historical_limit_structure",
                        (
                            "trade_date",
                            "limit",
                            "limit_times",
                            "open_times",
                            "first_time",
                            "last_time",
                            "up_stat",
                            "close",
                            "fd_amount",
                        ),
                        "trade_date",
                    )
                )
                context.setdefault(code, {}).setdefault("limit_history", []).append(
                    {
                        key: row.get(key)
                        for key in (
                            "trade_date",
                            "limit",
                            "limit_times",
                            "open_times",
                            "first_time",
                            "last_time",
                            "up_stat",
                        )
                    }
                )
    kpl = pack.fetch("kpl_list", {"trade_date": session_text, "tag": "涨停"})
    for row in kpl:
        code = str(row.get("ts_code") or "")
        if code in valid_codes and ymd(row.get("trade_date")) == session:
            evidence.append(
                pack.evidence(
                    "kpl_list",
                    row,
                    code,
                    f"第三方开盘啦题材线索 {session.isoformat()}",
                    "third_party_theme_unverified",
                    ("trade_date", "theme", "lu_desc", "tag", "status"),
                    "trade_date",
                )
            )
    # A single full-market KPL response hits its 3000-row cap. Query a bounded
    # set of hot stock identities, then the discovered themes by documented ID.
    concept_ids: list[str] = []
    for row in kpl[:8]:
        code = str(row.get("ts_code") or "")
        if code not in valid_codes:
            continue
        for item in pack.fetch("kpl_concept_cons", {"trade_date": session_text, "con_code": code}):
            concept = str(item.get("ts_code") or "")
            if concept.endswith(".KP") and concept not in concept_ids:
                concept_ids.append(concept)
    for concept in concept_ids[:8]:
        for row in pack.fetch("kpl_concept_cons", {"trade_date": session_text, "ts_code": concept}):
            code = str(row.get("con_code") or "")
            if code not in valid_codes or ymd(row.get("trade_date")) != session:
                continue
            if row.get("con_name") and str(row["con_name"]).replace(" ", "") != names[code].replace(
                " ", ""
            ):
                continue
            item = pack.evidence(
                "kpl_concept_cons",
                row,
                code,
                f"第三方题材成分：{row.get('name') or concept}",
                "third_party_theme_membership_unverified",
                ("trade_date", "ts_code", "name", "con_code", "con_name", "desc"),
                "trade_date",
            )
            evidence.append(item)
            leads.append(
                {
                    "instrument_ids": [code],
                    "relation": "third_party_theme_membership_unverified",
                    "route_type": "sector",
                    "theme_id": concept,
                    "summary": item.title,
                    "evidence_ids": [item.evidence_id],
                }
            )
            context.setdefault(code, {}).setdefault("themes", []).append(
                str(row.get("name") or concept)
            )

    periods = report_periods(cutoff.date(), count=5, include_next=True)
    for api, fields in (
        (
            "forecast_vip",
            (
                "ann_date",
                "end_date",
                "type",
                "p_change_min",
                "p_change_max",
                "net_profit_min",
                "net_profit_max",
                "summary",
                "change_reason",
                "update_flag",
            ),
        ),
        (
            "express_vip",
            (
                "ann_date",
                "end_date",
                "revenue",
                "n_income",
                "yoy_net_profit",
                "perf_summary",
                "update_flag",
            ),
        ),
    ):
        seen = set()
        for period in periods:
            for row in pack.fetch(api, {"period": period}):
                key = (
                    row.get("ts_code"),
                    row.get("ann_date"),
                    row.get("end_date"),
                    row.get("update_flag"),
                    api,
                )
                if key in seen:
                    continue
                seen.add(key)
                add_event(api, row, f"{api} 公司披露 {row.get('ann_date') or '日期未知'}", fields)
    beginning = cutoff.date() - timedelta(days=30)
    for api, fields in (
        ("repurchase", ("ann_date", "end_date", "proc", "exp_date", "vol", "amount")),
        (
            "stk_holdertrade",
            (
                "ann_date",
                "holder_type",
                "in_de",
                "change_vol",
                "change_ratio",
                "begin_date",
                "close_date",
            ),
        ),
    ):
        middle = beginning + timedelta(days=15)
        for start, end in ((beginning, middle), (middle + timedelta(days=1), cutoff.date())):
            for row in pack.fetch(
                api, {"start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
            ):
                add_event(api, row, f"{api} 公司披露 {row.get('ann_date') or '日期未知'}", fields)
    future = cutoff.date() + timedelta(days=30)
    for week in range(5):
        start = cutoff.date() + timedelta(days=1 + week * 7)
        end = min(start + timedelta(days=6), future)
        if start > end:
            break
        for row in fetch_unlock_window(pack, start, end):
            code = str(row.get("ts_code") or "")
            float_day = ymd(row.get("float_date"))
            if (
                code in valid_codes
                and public_time(row, "ann_date", cutoff)
                and float_day
                and start <= float_day <= end
            ):
                evidence.append(
                    pack.evidence(
                        "share_float",
                        row,
                        code,
                        "已披露的未来解禁日历",
                        "known_future_unlock",
                        ("ann_date", "float_date", "float_share", "float_ratio", "share_type"),
                        "float_date",
                    )
                )
                context.setdefault(code, {}).setdefault("future_unlocks", []).append(
                    {
                        key: row.get(key)
                        for key in ("ann_date", "float_date", "float_share", "float_ratio")
                    }
                )
    for row in pack.fetch("suspend_d", {"trade_date": session_text}):
        code = str(row.get("ts_code") or "")
        if code in valid_codes and ymd(row.get("trade_date")) == session:
            evidence.append(
                pack.evidence(
                    "suspend_d",
                    row,
                    code,
                    "已知停复牌状态",
                    "trading_status",
                    ("trade_date", "suspend_timing", "suspend_type"),
                    "trade_date",
                )
            )
            context.setdefault(code, {})["trading_status"] = {
                key: row.get(key) for key in ("trade_date", "suspend_timing", "suspend_type")
            }
    flows: dict[str, dict[str, float | None]] = {}
    flow_days = {day.strftime("%Y%m%d") for day in recent_sessions[-5:]}
    for day in recent_sessions[-5:]:
        for row in pack.fetch("moneyflow", {"trade_date": day.strftime("%Y%m%d")}):
            code = str(row.get("ts_code") or "")
            if code in valid_codes and ymd(row.get("trade_date")) == day:
                flows.setdefault(code, {})[day.isoformat()] = row.get("net_mf_amount")
    for code, values in flows.items():
        summary = {}
        for horizon in (1, 3, 5):
            days = [day.isoformat() for day in recent_sessions[-horizon:]]
            present = [values.get(day) for day in days]
            summary[f"net_{horizon}d_wan_cny"] = (
                sum(present)
                if len(days) == horizon and all(isinstance(x, (int, float)) for x in present)
                else None
            )
            summary[f"coverage_{horizon}d"] = sum(isinstance(x, (int, float)) for x in present)
        context.setdefault(code, {})["moneyflow"] = summary
        evidence.append(
            pack.evidence(
                "moneyflow",
                {"ts_code": code, "trade_date": session_text, **summary},
                code,
                "个股资金流窗口统计（供应商口径）",
                "moneyflow_context",
                tuple(summary),
                "trade_date",
                tuple(
                    item["snapshot"]
                    for item in pack.matrix
                    if item["api"] == "moneyflow"
                    and item.get("snapshot")
                    and item["params"].get("trade_date") in flow_days
                ),
            )
        )
    return evidence, leads, context


def collect_sw_memberships(pack: TusharePack, valid_codes: set[str]) -> dict[str, str]:
    """Current SW2021 L1 identities, with per-class limits exposed in the matrix."""
    groups = pack.fetch("index_classify", {"level": "L1", "src": "SW2021"})
    memberships: dict[str, str] = {}
    for group in groups[:40]:
        l1_code = str(group.get("index_code") or "")
        if not l1_code.endswith(".SI"):
            continue
        for row in pack.fetch("index_member_all", {"l1_code": l1_code, "is_new": "Y"}):
            code = str(row.get("ts_code") or "")
            if code in valid_codes and row.get("is_new") == "Y":
                name = str(row.get("l1_name") or group.get("industry_name") or "")
                if name:
                    memberships[code] = name
    return memberships


def collect_deep_pack(
    pack: TusharePack, candidates: list[dict], target_sessions: list[date]
) -> tuple[list[Evidence], dict[str, dict]]:
    """Get business and scheduled-report background for the bounded deep pool."""
    evidence: list[Evidence] = []
    context: dict[str, dict] = {}
    unlock_start = pack.now.date() + timedelta(days=1)
    unlock_end = pack.now.date() + timedelta(days=30)
    for candidate in candidates:
        code = candidate["instrument_id"]
        for row in fetch_unlock_window(pack, unlock_start, unlock_end, code):
            if (
                row.get("ts_code") != code
                or not public_time(row, "ann_date", pack.now)
                or not (unlock_start <= (ymd(row.get("float_date")) or date.min) <= unlock_end)
            ):
                continue
            evidence.append(
                pack.evidence(
                    "share_float",
                    row,
                    code,
                    "已披露的未来解禁日历",
                    "known_future_unlock",
                    ("ann_date", "float_date", "float_share", "float_ratio", "share_type"),
                    "float_date",
                )
            )
            context.setdefault(code, {}).setdefault("future_unlocks", []).append(
                {
                    key: row.get(key)
                    for key in ("ann_date", "float_date", "float_share", "float_ratio")
                }
            )
        for api, fields in (
            (
                "fina_indicator",
                (
                    "ann_date",
                    "end_date",
                    "profit_dedt",
                    "gross_margin",
                    "debt_to_assets",
                    "ocf_to_or",
                    "or_yoy",
                    "netprofit_yoy",
                ),
            ),
            (
                "fina_mainbz",
                ("end_date", "bz_item", "bz_code", "bz_sales", "bz_profit", "bz_cost", "curr_type"),
            ),
        ):
            rows = []
            for period in report_periods(pack.now.date()):
                rows = [
                    row
                    for row in pack.fetch(api, {"ts_code": code, "period": period})
                    if row.get("ts_code", code) == code and str(row.get("end_date")) == period
                ]
                if api == "fina_indicator":
                    rows = [row for row in rows if public_time(row, "ann_date", pack.now)]
                if rows:
                    break
            if api == "fina_indicator":
                rows.sort(key=lambda x: str(x.get("ann_date") or ""), reverse=True)
                rows = rows[:1]
            else:
                rows = rows[:8]  # Report period is not a publication date.
            if api == "fina_mainbz" and rows:
                segments = [{key: row.get(key) for key in fields if key in row} for row in rows]
                snapshot = pack.row_snapshots.get(fingerprint([api, rows[0]]))
                context.setdefault(code, {})[api] = segments
                evidence.append(
                    pack.evidence(
                        api,
                        {"end_date": rows[0].get("end_date"), "segments": segments},
                        code,
                        "已取得的主营构成节选，公开时间未知",
                        "financial_background",
                        ("end_date", "segments"),
                        "end_date",
                        (snapshot,) if snapshot else (),
                    )
                )
                continue
            for row in rows:
                item = pack.evidence(
                    api,
                    row,
                    code,
                    "已取得的财务指标背景"
                    if api == "fina_indicator"
                    else "已取得的主营构成背景，公开时间未知",
                    "financial_background",
                    fields,
                    "ann_date" if api == "fina_indicator" else "end_date",
                )
                evidence.append(item)
                context.setdefault(code, {}).setdefault(api, []).append(
                    {key: row.get(key) for key in fields if key in row}
                )
        rows = pack.fetch("disclosure_date", {"ts_code": code})
        target_end = target_sessions[9] if len(target_sessions) >= 10 else None
        context.setdefault(code, {})["disclosure_window"] = {
            "start": pack.now.date().isoformat(),
            "end": target_end.isoformat() if target_end else None,
            "status": "covered" if target_end else "calendar_insufficient",
        }
        latest_by_period = {}
        for row in sorted(rows, key=lambda item: str(item.get("ann_date") or "")):
            if row.get("ts_code") == code and public_time(row, "ann_date", pack.now):
                latest_by_period[str(row.get("end_date") or row.get("pre_date"))] = row
        context[code]["disclosure_schedule"] = [
            {key: row.get(key) for key in ("ann_date", "end_date", "pre_date", "actual_date")}
            for row in latest_by_period.values()
        ]
        for row in latest_by_period.values():
            planned = ymd(row.get("pre_date"))
            actual = ymd(row.get("actual_date"))
            if (
                target_end is not None
                and planned is not None
                and pack.now.date() <= planned <= target_end
            ):
                evidence.append(
                    pack.evidence(
                        "disclosure_date",
                        row,
                        code,
                        "财报披露已完成"
                        if actual and actual <= pack.now.date()
                        else "已知财报披露计划",
                        "completed_disclosure"
                        if actual and actual <= pack.now.date()
                        else "scheduled_disclosure",
                        ("ann_date", "end_date", "pre_date", "actual_date"),
                        "pre_date",
                    )
                )
    return evidence, context
