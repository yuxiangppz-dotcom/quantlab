"""Bounded read-only source adapters; failures are coverage, never invented evidence."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from quantlab.scout.models import SHANGHAI, Coverage, Evidence, timestamp, web_url

MAX_BYTES = 2_000_000
CNINFO_STOCKS = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirect refused; configure final public HTTPS feed URL")


def read_public_feed(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not web_url(url) or parsed.port not in {None, 443}:
        raise ValueError("Feed must use public HTTPS on port 443")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError("Private feed destinations are not supported")
    request = urllib.request.Request(url, headers={"User-Agent": "QuantLab-Scout/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("Feed exceeds size limit")
    return body


def parse_feed(raw: bytes, source: str, now: datetime) -> list[Evidence]:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("DTD/entity declarations are not accepted")
    root = ET.fromstring(raw)
    # Strip namespace prefix for RSS and Atom interoperability.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    items = root.findall(".//item") or root.findall(".//entry")
    result = []
    for item in items[:100]:
        title = item.findtext("title", "").strip()
        link = item.find("link")
        url = (link.get("href") or link.text or "").strip() if link is not None else ""
        raw_time = item.findtext("pubDate") or item.findtext("published")
        published = None
        if raw_time:
            try:
                try:
                    parsed = timestamp(raw_time)
                except ValueError:
                    parsed = parsedate_to_datetime(raw_time)
                if parsed.tzinfo:
                    published = parsed.isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
        if not title:
            continue
        result.append(
            Evidence(
                source=source,
                title=title[:500],
                body=(item.findtext("description") or item.findtext("summary") or "")[:6000],
                url=url if web_url(url) else None,
                published_at=published,
                retrieved_at=now.isoformat(),
                kind="feed",
            )
        )
    return result


def load_manual(path: Path, now: datetime) -> list[Evidence]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Clue file must be a JSON list of at most 100 records")
    return [
        Evidence(
            source=str(row.get("source", "user_clue")),
            title=row["title"][:500],
            body=row.get("body", "")[:6000],
            url=row.get("url"),
            published_at=row.get("published_at"),
            retrieved_at=now.isoformat(),
            kind="user_clue_unverified",
            instrument_ids=tuple(row.get("instrument_ids", [])),
        )
        for row in rows
    ]


def collect_announcements(
    config: dict, now: datetime, online: bool, codes: list[str]
) -> tuple[list[Evidence], Coverage]:
    """Optional targeted TuShare announcement index; never claim full coverage."""
    name = "official_announcement_targeted"
    if not config["tushare_announcements"]:
        return [], Coverage(name, "not_configured", detail="TuShare anns_d disabled")
    if not online or not os.environ.get("TUSHARE_TOKEN"):
        return [], Coverage(name, "disabled", detail="offline or missing token")
    targets = list(dict.fromkeys(codes))[:8]
    if not targets:
        return [], Coverage(name, "empty_unconfirmed", detail="no target stocks")
    import tushare as ts

    try:
        api = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20)
    except Exception as exc:
        return [], Coverage(name, "failed", detail=type(exc).__name__)
    lookback_days = min(8, (config["lookback_hours"] + 23) // 24 + 1)
    earliest = now.date() - timedelta(days=lookback_days)
    start = earliest.strftime("%Y%m%d")
    end = now.date().strftime("%Y%m%d")
    evidence: list[Evidence] = []
    failed = invalid = truncated = 0
    for code in targets:
        try:
            frame = api.anns_d(
                ts_code=code,
                start_date=start,
                end_date=end,
                fields="ann_date,ts_code,name,title,url,rec_time",
            )
            rows = frame.to_dict("records")
        except Exception:
            failed += 1
            continue
        truncated += int(len(rows) > 100)
        for row in rows[:100]:
            try:
                if row.get("ts_code") != code:
                    raise ValueError("Announcement stock mismatch")
                event = datetime.strptime(str(row["ann_date"]), "%Y%m%d").date()
                if not earliest <= event <= now.date():
                    raise ValueError("Announcement date outside request")
                url = str(row["url"])
                title = str(row["title"]).strip()
                if not web_url(url) or not title:
                    raise ValueError("Announcement needs title and public URL")
                published = None
                raw_time = row.get("rec_time")
                if raw_time is not None and str(raw_time).strip().lower() not in {"", "nan", "nat"}:
                    try:
                        parsed = datetime.fromisoformat(str(raw_time))
                    except (TypeError, ValueError):
                        parsed = None
                    if parsed is not None and parsed.tzinfo is not None:
                        if parsed > now:
                            raise ValueError("Future announcement publication")
                        published = parsed.isoformat()
                evidence.append(
                    Evidence(
                        source="tushare:anns_d",
                        title=title[:500],
                        body="公告索引仅提供标题与原文链接；PDF正文未读取或核实。",
                        url=url,
                        published_at=published,
                        retrieved_at=datetime.now(SHANGHAI).isoformat(),
                        kind="official_announcement_index_unverified",
                        instrument_ids=(code,),
                        event_dates=(event.isoformat(),),
                    )
                )
            except (KeyError, TypeError, ValueError):
                invalid += 1
    if truncated:
        status = "possibly_truncated"
    elif failed == len(targets):
        status = "failed"
    elif failed or invalid:
        status = "partial"
    elif evidence:
        status = "targeted_only"
    else:
        status = "empty_unconfirmed"
    detail = (
        f"TuShare anns_d index for {len(targets)} target stocks only; "
        f"{failed} failed queries, {invalid} rejected rows, {truncated} local/provider cap hits; "
        "PDF content not read; empty is not proof of absence"
    )
    return evidence, Coverage(name, status, len(evidence), detail)


def read_cninfo_json(url: str, form: dict | None = None) -> dict:
    """Read only the two fixed official endpoints with bounded responses."""
    if url not in {CNINFO_STOCKS, CNINFO_QUERY}:
        raise ValueError("Unexpected CNINFO endpoint")
    payload = urlencode(form).encode() if form is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": "Mozilla/5.0 QuantLab-Scout/1.0",
            "Referer": "https://www.cninfo.com.cn/",
            "Origin": "https://www.cninfo.com.cn",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("CNINFO response exceeds size limit")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("CNINFO response must be an object")
    return result


def collect_cninfo_announcements(
    config: dict,
    now: datetime,
    online: bool,
    codes: list[str],
    *,
    post_selection: bool = False,
) -> tuple[list[Evidence], Coverage]:
    """Targeted official index. Its date-only timestamp is never a publication time."""
    name = (
        "cninfo_official_index_post_selection"
        if post_selection
        else "cninfo_official_index_targeted"
    )
    if not config["cninfo_announcements"]:
        return [], Coverage(name, "not_configured")
    if not online:
        return [], Coverage(name, "disabled", detail="offline mode")
    targets = list(dict.fromkeys(codes))[:8]
    if not targets:
        return [], Coverage(name, "empty_unconfirmed", detail="no target stocks")
    try:
        stock_rows = read_cninfo_json(CNINFO_STOCKS)["stockList"]
        if not isinstance(stock_rows, list):
            raise ValueError("Invalid CNINFO stock list")
        org_by_code = {
            row["code"]: row["orgId"]
            for row in stock_rows
            if isinstance(row, dict)
            and isinstance(row.get("code"), str)
            and isinstance(row.get("orgId"), str)
        }
    except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
        return [], Coverage(name, "failed", detail="stock list unavailable")
    lookback_days = min(8, (config["lookback_hours"] + 23) // 24 + 1)
    earliest = now.date() - timedelta(days=lookback_days)
    date_range = f"{earliest.isoformat()}~{now.date().isoformat()}"
    evidence: list[Evidence] = []
    failed = invalid = truncated = 0
    for code in targets:
        short_code = code[:6]
        org_id = org_by_code.get(short_code)
        if not re.fullmatch(r"\d{6}\.(SZ|SH)", code) or not org_id:
            failed += 1
            continue
        sh = code.endswith(".SH")
        form = {
            "pageNum": 1,
            "pageSize": 30,
            "column": "sse" if sh else "szse",
            "tabName": "fulltext",
            "plate": "sh" if sh else "sz",
            "stock": f"{short_code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": date_range,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        try:
            result = read_cninfo_json(CNINFO_QUERY, form)
            rows = result["announcements"]
            if rows is None and result.get("totalAnnouncement") == 0:
                rows = []
            if not isinstance(rows, list):
                raise ValueError("Invalid announcement list")
            truncated += int(bool(result.get("hasMore")) or len(rows) > 30)
        except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
            failed += 1
            continue
        for row in rows[:30]:
            try:
                if row["secCode"] != short_code:
                    raise ValueError("Announcement stock mismatch")
                event = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, SHANGHAI).date()
                if not earliest <= event <= now.date():
                    raise ValueError("Announcement date outside request")
                title = str(row["announcementTitle"]).strip()
                path = str(row["adjunctUrl"])
                if not title or not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", path):
                    raise ValueError("Missing title or unexpected PDF path")
                evidence.append(
                    Evidence(
                        source=(
                            "cninfo:official_index_post_selection"
                            if post_selection
                            else "cninfo:official_index"
                        ),
                        title=title[:500],
                        body=(
                            "巨潮公告索引仅提供标题和PDF链接；正文未由Scout读取。"
                            "公告日期不等于精确发布时间。"
                            + ("此条在模型分级后补查。" if post_selection else "")
                        ),
                        url=f"https://static.cninfo.com.cn/{path}",
                        published_at=None,
                        retrieved_at=datetime.now(SHANGHAI).isoformat(),
                        kind="official_announcement_index_unverified",
                        instrument_ids=(code,),
                        event_dates=(event.isoformat(),),
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid += 1
    if truncated:
        status = "possibly_truncated"
    elif failed == len(targets):
        status = "failed"
    elif failed or invalid:
        status = "partial"
    elif evidence:
        status = "targeted_only"
    else:
        status = "empty_unconfirmed"
    detail = (
        f"CNINFO index for {len(targets)} target stocks only; "
        f"{failed} failed queries, {invalid} rejected rows, {truncated} page-cap hits; "
        "count is raw index rows before evidence deduplication; "
        "PDF content not read, publication time unknown; empty is not proof of absence"
        + ("; queried after model selection" if post_selection else "")
    )
    return evidence, Coverage(name, status, len(evidence), detail)


def collect_kpl_limit_reasons(
    config: dict, now: datetime, online: bool, session: date, codes: list[str]
) -> tuple[list[Evidence], Coverage]:
    """Read bounded third-party limit-up themes after the documented next-day update."""
    name = "tushare_kpl_limit_themes"
    if not config["tushare_kpl_limit"]:
        return [], Coverage(name, "not_configured")
    if not online or not os.environ.get("TUSHARE_TOKEN"):
        return [], Coverage(name, "disabled", detail="offline or missing token")
    available_after = datetime.combine(session + timedelta(days=1), time(6), SHANGHAI)
    if now.astimezone(SHANGHAI) < available_after:
        return [], Coverage(
            name, "not_yet_expected", detail="provider documents next-day 06:00 update"
        )
    session_close = datetime.combine(session, time(15), SHANGHAI)
    if (now.astimezone(SHANGHAI) - session_close).total_seconds() > config["lookback_hours"] * 3600:
        return [], Coverage(name, "stale", detail="market session outside configured lookback")
    targets = set(dict.fromkeys(codes[:8]))
    if not targets:
        return [], Coverage(name, "empty_unconfirmed", detail="no target stocks")
    import tushare as ts

    try:
        frame = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20).kpl_list(
            trade_date=session.strftime("%Y%m%d"),
            tag="涨停",
            fields="ts_code,name,trade_date,tag,theme,status,lu_desc",
        )
        rows = frame.to_dict("records")
    except Exception as exc:
        return [], Coverage(name, "failed", detail=type(exc).__name__)
    evidence = []
    rejected = 0
    for row in rows[:8000]:
        if row.get("ts_code") not in targets:
            continue
        try:
            if str(row["trade_date"]) != session.strftime("%Y%m%d") or row["tag"] != "涨停":
                raise ValueError("KPL session or tag mismatch")
            theme = str(row["theme"]).strip()
            reason = str(row.get("lu_desc") or "").strip()
            if not theme or theme.lower() in {"nan", "none"}:
                raise ValueError("KPL theme missing")
            if reason.lower() in {"", "nan", "none"}:
                reason = "未知"
            board_status = str(row.get("status") or "").strip()
            if board_status.lower() in {"", "nan", "none"}:
                board_status = "未知"
            code = row["ts_code"]
            evidence.append(
                Evidence(
                    source="tushare:kpl_list",
                    title=f"第三方涨停题材标签：{theme[:180]}",
                    body=(
                        f"开盘啦榜单标注涨停原因：{reason[:240]}；"
                        f"连板状态：{board_status[:80]}。"
                        "这是第三方题材归类，不是上市公司公告或已核实的上涨因果。"
                        "供应商未提供逐条精确发布时间或原文URL。"
                    ),
                    url=None,
                    published_at=None,
                    retrieved_at=datetime.now(SHANGHAI).isoformat(),
                    kind="theme_board_unverified",
                    instrument_ids=(code,),
                    event_dates=(session.isoformat(),),
                )
            )
        except (KeyError, TypeError, ValueError):
            rejected += 1
    status = (
        "possibly_truncated"
        if len(rows) > 8000
        else "partial"
        if rejected
        else "targeted_only"
        if evidence
        else "empty_unconfirmed"
    )
    detail = (
        f"KPL tag=涨停 for {len(targets)} target stocks; provider returned {len(rows)} rows, "
        f"{rejected} targeted rows rejected. Third-party theme attribution, no per-row "
        "publication time or URL; not an issuer fact or causal explanation"
    )
    return evidence, Coverage(name, status, len(evidence), detail)


def collect_sources(config: dict, now: datetime, online: bool) -> tuple[list, list]:
    evidence: list[Evidence] = []
    coverage: list[Coverage] = []
    for feed in config.get("rss", []):
        name = feed["name"]
        if not online:
            coverage.append(Coverage(name, "disabled", detail="offline mode"))
            continue
        try:
            items = parse_feed(read_public_feed(feed["url"]), name, now)
            evidence.extend(items)
            coverage.append(Coverage(name, "ok", len(items)))
        except Exception as exc:
            coverage.append(Coverage(name, "failed", detail=type(exc).__name__))
    if not config.get("rss"):
        coverage.append(Coverage("RSS/Atom", "not_configured"))
    news_sources = config.get("tushare_news_sources", [])
    if not news_sources:
        coverage.append(Coverage("TuShare news", "not_configured"))
    elif not online or not os.environ.get("TUSHARE_TOKEN"):
        coverage.append(Coverage("TuShare news", "disabled", detail="offline or missing token"))
    else:
        import tushare as ts

        api = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20)
        start = now - timedelta(hours=config["lookback_hours"])
        for source in news_sources:
            try:
                frame = api.news(
                    src=source,
                    start_date=start.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=now.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S"),
                )
                rows = frame.to_dict("records")
                for row in rows[:100]:
                    # Tushare news datetime is China local time; no supplied URL invented.
                    published = datetime.fromisoformat(str(row["datetime"]))
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=SHANGHAI)
                    evidence.append(
                        Evidence(
                            source=f"tushare:{source}",
                            title=str(row.get("title") or "快讯")[:500],
                            body=str(row.get("content") or "")[:6000],
                            url=None,
                            published_at=published.isoformat(),
                            retrieved_at=now.isoformat(),
                        )
                    )
                detail = "bounded to 100 rows; not exhaustive"
                coverage.append(Coverage(f"tushare:{source}", "ok", min(len(rows), 100), detail))
            except Exception as exc:
                # Provider errors can contain tokens; never persist exception messages.
                coverage.append(Coverage(f"tushare:{source}", "failed", detail=type(exc).__name__))
    return evidence, coverage
