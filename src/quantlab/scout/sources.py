"""Bounded read-only source adapters; failures are coverage, never invented evidence."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from quantlab.scout.models import SHANGHAI, Coverage, Evidence, timestamp, web_url

MAX_BYTES = 2_000_000


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
