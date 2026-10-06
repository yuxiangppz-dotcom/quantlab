"""Bounded read-only source adapters; failures are coverage, never invented evidence."""

from __future__ import annotations

import hashlib
import io
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
CNINFO_PDF = re.compile(r"https://static\.cninfo\.com\.cn/finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF")


def source_window(config: dict, now: datetime) -> tuple[datetime, datetime]:
    """D0 close to cutoff, including long holidays; fallback is explicitly legacy."""
    start = (
        timestamp(config["_source_window_start"])
        if config.get("_source_window_start")
        else (now - timedelta(hours=config["lookback_hours"]))
    )
    if start > now:
        raise ValueError("Source coverage start is after information cutoff")
    return start.astimezone(SHANGHAI), now.astimezone(SHANGHAI)


def window_detail(config: dict, now: datetime) -> str:
    start, end = source_window(config, now)
    return (
        f"requested_window={start.isoformat()}~{end.isoformat()}; "
        "bounded source acquisition, full coverage not established"
    )


def source_cache(
    config: dict, kind: str, identity: str, now: datetime, fetch, *, historical: bool
) -> tuple[object, str]:
    """Reuse only completed historical query slices; current-day feeds stay fresh.

    Watermarks are collection times, never publication or market novelty times.
    Cache writes are used only when the runtime supplies its isolated Scout path.
    """
    root = config.get("_source_cache_root")
    key = hashlib.sha256(identity.encode()).hexdigest()
    path = Path(root) / kind / f"{key}.json" if root else None
    if historical and path and path.is_file():
        try:
            if path.stat().st_size <= MAX_BYTES:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (
                    saved.get("version") == "source_slice_cache_v1"
                    and saved["identity"] == identity
                    and timestamp(saved["collected_at"]) <= now
                    and isinstance(saved["payload"], (dict, list))
                ):
                    return saved["payload"], saved["collected_at"]
        except (ValueError, KeyError, OSError):
            pass
    payload = fetch()
    collected = datetime.now(SHANGHAI).isoformat()
    if historical and path:
        serialized = json.dumps(
            {
                "version": "source_slice_cache_v1",
                "identity": identity,
                "collected_at": collected,
                "payload": payload,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
        if len(serialized.encode()) <= MAX_BYTES:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Cache immutable successful source slices. No failed response caching.
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(serialized)
            except FileExistsError:
                pass
    return payload, collected


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
    earliest = source_window(config, now)[0].date()
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
        "PDF content not read; empty is not proof of absence" + "; " + window_detail(config, now)
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


def bounded_cninfo_pages(form: dict, max_pages: int = 2) -> tuple[list[dict], bool]:
    """Bounded pagination; a remaining-page flag is never a negative search."""
    rows, more = [], False
    for page in range(1, max_pages + 1):
        result = read_cninfo_json(CNINFO_QUERY, {**form, "pageNum": page})
        batch = result.get("announcements")
        if batch is None and result.get("totalAnnouncement", 0) == 0:
            batch = []
        if not isinstance(batch, list):
            raise ValueError("Invalid announcement list")
        rows.extend(batch[: form["pageSize"]])
        more = bool(result.get("hasMore")) or len(batch) > form["pageSize"]
        if not more:
            break
    return rows, more


def collect_cninfo_announcements(
    config: dict,
    now: datetime,
    online: bool,
    codes: list[str],
    *,
    post_selection: bool = False,
    max_targets: int = 8,
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
    targets = list(dict.fromkeys(codes))[:max_targets]
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
    earliest = source_window(config, now)[0].date()
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
            rows, more = bounded_cninfo_pages(form)
            truncated += int(more)
        except (KeyError, TypeError, ValueError, OSError, urllib.error.URLError):
            failed += 1
            continue
        for row in rows:
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
        + "; "
        + window_detail(config, now)
        + ("; queried after model selection" if post_selection else "")
    )
    return evidence, Coverage(name, status, len(evidence), detail)


def collect_cninfo_market_index(
    config: dict, now: datetime, online: bool, valid_codes: set[str]
) -> tuple[list[Evidence], Coverage]:
    """Bounded market-wide announcement lead scan, independent of price ranking.

    CNINFO only supplies a date in this index.  This is a discovery sample, not a
    complete announcement subscription or a timestamped publication feed.
    """
    name = "cninfo_market_index"
    if not config.get("cninfo_market_index", False):
        return [], Coverage(name, "not_configured")
    if not online:
        return [], Coverage(name, "disabled", detail="offline mode")
    earliest = source_window(config, now)[0].date()
    rows_seen, failures, capped = 0, 0, 0
    evidence: list[Evidence] = []
    queries = [
        (column, plate, suffix, earliest, now.date())
        for column, plate, suffix in (("sse", "sh", "SH"), ("szse", "sz", "SZ"))
    ]
    request_count, request_cap, windows, gaps = 0, 12, [], []
    seen_documents = set()
    while queries and request_count < request_cap:
        column, plate, suffix, start_day, end_day = queries.pop(0)
        window = {
            "exchange": suffix,
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "status": "sampled",
        }
        for page in range(1, 3):
            if request_count >= request_cap:
                window["status"] = "truncated"
                break
            form = {
                "pageNum": page,
                "pageSize": 50,
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start_day.isoformat()}~{end_day.isoformat()}",
                "sortName": "time",
                "sortType": "desc",
                "isHLtitle": "true",
            }
            try:
                request_count += 1
                result, collected = source_cache(
                    config,
                    "cninfo_index",
                    json.dumps(form, sort_keys=True),
                    now,
                    lambda form=form: read_cninfo_json(CNINFO_QUERY, form),
                    historical=end_day < now.date(),
                )
                if not isinstance(result, dict):
                    raise ValueError("Invalid cached announcement response")
                rows = result.get("announcements") or []
                if not isinstance(rows, list):
                    raise ValueError("Invalid announcement list")
                more = bool(result.get("hasMore")) or len(rows) > 50
                if page == 2 and more:
                    capped += 1
                    window["status"] = "truncated"
                    if start_day < end_day:
                        # Date slices supplement a capped wide holiday window.
                        # An explicit global cap bounds latency and provider cost.
                        day = start_day
                        while day <= end_day:
                            queries.append((column, plate, suffix, day, day))
                            day += timedelta(days=1)
            except (ValueError, OSError, urllib.error.URLError):
                failures += 1
                window["status"] = "failed"
                break
            rows_seen += len(rows[:50])
            for row in rows[:50]:
                try:
                    code = f"{row['secCode']}.{suffix}"
                    if code not in valid_codes:
                        continue
                    event = datetime.fromtimestamp(
                        int(row["announcementTime"]) / 1000, SHANGHAI
                    ).date()
                    path = str(row["adjunctUrl"])
                    title = str(row["announcementTitle"]).strip()
                    if (
                        not earliest <= event <= now.date()
                        or not title
                        or not re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", path)
                    ):
                        continue
                    document_key = (code, path)
                    if document_key in seen_documents:
                        continue
                    seen_documents.add(document_key)
                    evidence.append(
                        Evidence(
                            source="cninfo:market_index",
                            title=title[:500],
                            body="巨潮市场公告索引；仅标题和PDF链接，精确发布时间未知。",
                            url=f"https://static.cninfo.com.cn/{path}",
                            published_at=None,
                            retrieved_at=collected,
                            kind="official_announcement_index_unverified",
                            instrument_ids=(code,),
                            event_dates=(event.isoformat(),),
                        )
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not more:
                break
        windows.append(window)
        if window["status"] != "sampled":
            gaps.append(window)
    gaps.extend(
        {
            "exchange": suffix,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": "not_queried_budget",
        }
        for _, _, suffix, start, end in queries
    )
    status = (
        "failed"
        if failures == 2 and not rows_seen
        else "possibly_truncated"
        if gaps
        else "partial"
        if failures
        else "sampled"
    )
    return evidence, Coverage(
        name,
        status,
        len(evidence),
        f"{rows_seen} raw rows, {request_count}/{request_cap} requests, 50 rows/page; "
        f"{failures} failed requests, {capped} page caps; market index is not exhaustive; "
        "publication time unknown; "
        + window_detail(config, now)
        + "; "
        + json.dumps({"queried_windows": windows, "coverage_gaps": gaps}, ensure_ascii=False),
    )


def read_cninfo_pdf(url: str) -> bytes:
    """Fetch only a bounded official archive PDF; never follow a redirect."""
    if not CNINFO_PDF.fullmatch(url):
        raise ValueError("Unexpected CNINFO PDF URL")
    request = urllib.request.Request(url, headers={"User-Agent": "QuantLab-Scout/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not raw.startswith(b"%PDF-"):
        raise ValueError("Invalid or oversized CNINFO PDF")
    return raw


def collect_cninfo_pdf_bodies(
    config: dict, now: datetime, online: bool, notices: list[Evidence], max_stocks: int = 8
) -> tuple[list[Evidence], Coverage]:
    """Extract one relevant initial official PDF per stock before AI ranking.

    Extraction is machine text, not an independently verified interpretation.
    The archive date is not a point-in-time publication timestamp.
    """
    name = "cninfo_official_pdf_text_targeted"
    if not config["cninfo_pdf_bodies"]:
        return [], Coverage(name, "not_configured")
    if not online or not config["cninfo_announcements"]:
        return [], Coverage(name, "disabled", detail="requires live official index")
    priority = re.compile(
        r"停牌|业绩|财务|年度报告|季度报告|异常波动|股票交易|"
        r"重大合同|中标|订单|重组|收购|合作|框架协议|政策|补贴|终止|诉讼"
    )
    chosen: dict[str, Evidence] = {}
    for item in notices:
        if (
            item.source not in {"cninfo:official_index", "cninfo:market_index"}
            or not item.url
            or not priority.search(item.title)
            or not item.instrument_ids
        ):
            continue
        code = item.instrument_ids[0]
        current = chosen.get(code)
        rank = (
            bool(re.search(r"停牌|重大合同|中标|重组|终止|诉讼", item.title)),
            (item.event_dates or ("",))[0],
            item.url,
        )
        if current is None or rank > (
            bool(re.search(r"停牌|重大合同|中标|重组|终止|诉讼", current.title)),
            (current.event_dates or ("",))[0],
            current.url,
        ):
            chosen[code] = item
    evidence: list[Evidence] = []
    failed = empty = oversized = 0
    from pypdf import PdfReader

    for item in list(chosen.values())[:max_stocks]:
        try:

            def extract_pdf(item=item):
                raw = read_cninfo_pdf(item.url)
                reader = PdfReader(io.BytesIO(raw), strict=True)
                if reader.is_encrypted or not 1 <= len(reader.pages) <= 12:
                    return {"status": "encrypted_or_over_page_budget"}
                parts = [page.extract_text() or "" for page in reader.pages]
                body, extraction = pdf_relevant_text(parts)
                return {
                    "status": "extracted",
                    "body": body,
                    "extraction": extraction,
                    "page_count": len(reader.pages),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }

            extracted, collected = source_cache(
                config,
                "pdf_text",
                item.url,
                now,
                extract_pdf,
                historical=True,  # A fixed official archive URL identifies one document.
            )
            if extracted["status"] != "extracted":
                oversized += 1
                continue
            body, extraction = extracted["body"], extracted["extraction"]
            if len(body) < 80:
                empty += 1
                continue
            sha = extracted["sha256"]
            evidence.append(
                Evidence(
                    source="cninfo:official_pdf_text",
                    title=item.title + "（机器提取正文）",
                    body=(
                        f"官方PDF SHA-256: {sha}; 页数: {extracted['page_count']}; "
                        "正文由机器提取，未人工核实；不能据公告日期推断精确披露时间。\n"
                        + body
                        + "\n抽取覆盖: "
                        + json.dumps(extraction, ensure_ascii=False)
                    ),
                    url=item.url,
                    published_at=None,
                    retrieved_at=collected,
                    kind="official_pdf_text_unverified",
                    instrument_ids=item.instrument_ids,
                    event_dates=item.event_dates,
                )
            )
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
            failed += 1
        except Exception:
            # A malformed PDF must lower coverage, not abort the entire research run.
            failed += 1
    if failed or empty or oversized:
        status = "partial"
    elif evidence:
        status = "targeted_only"
    else:
        status = "empty_unconfirmed"
    detail = (
        f"up to one priority PDF per stock, {len(chosen)} selected, {failed} failed, "
        f"{empty} without extractable text, {oversized} encrypted/over 12 pages; "
        "machine text not manually verified, exact publication time unknown; "
        "no OCR, empty is not proof of absence"
        + "; omitted_priority_evidence_ids="
        + json.dumps(
            [
                item.evidence_id
                for item in notices
                if priority.search(item.title)
                and item.evidence_id
                not in {e.evidence_id for e in list(chosen.values())[:max_stocks]}
            ]
        )
    )
    return evidence, Coverage(name, status, len(evidence), detail)


def pdf_relevant_text(parts: list[str], max_chars: int = 11000) -> tuple[str, dict]:
    """Preserve page-located limits before benefits under a bounded text budget."""
    adverse = re.compile(
        r"风险|不确定|终止|解除|违约|尚需|尚未|不能|不构成|"
        r"不保证|框架|意向|多年|分期|审批|条件|限制|亏损|减持|诉讼"
    )
    relevant = re.compile(
        r"合同|中标|订单|重组|合作|补贴|金额|利润|收入|履行|"
        r"实施|期限|交付|支付|业绩|财务|停牌"
    )
    entries = []
    for page, text in enumerate(parts, 1):
        paragraphs = re.split(r"\n\s*\n|(?<=[。；])", text)
        for index, paragraph in enumerate(paragraphs):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            family = (
                "counter_or_condition"
                if adverse.search(paragraph)
                else ("event_detail" if relevant.search(paragraph) else "background")
            )
            entries.append((family, page, index, paragraph))
    # The first small packet is the one most likely to survive stage packing:
    # one strongest restriction and one event detail, then all remaining clauses.
    ordered = []
    for family in ("counter_or_condition", "event_detail"):
        first = next((entry for entry in entries if entry[0] == family), None)
        if first:
            ordered.append(first)
    ordered.extend(
        sorted(
            (entry for entry in entries if entry not in ordered),
            key=lambda entry: (entry[0] == "background", entry[1], entry[2]),
        )
    )
    shown, used, omissions = [], 0, []
    for family, page, index, text in ordered:
        # Keep each selected fragment small enough for both evidence directions
        # to fit in the actual final request's per-document excerpt.
        excerpt = text[:450]
        fragment = f"[第{page}页 段{index + 1} {family}] {excerpt}\n"
        if used + len(fragment) > max_chars:
            omissions.append({"page": page, "paragraph": index + 1, "reason": "text_budget"})
            continue
        shown.append(fragment)
        used += len(fragment)
        if len(text) > len(excerpt):
            omissions.append(
                {"page": page, "paragraph": index + 1, "reason": "paragraph_tail_truncated"}
            )
    return "".join(shown).strip(), {
        "pages_scanned": len(parts),
        "selected_fragments": len(shown),
        "omitted_fragments": omissions,
        "truncated": bool(omissions),
        "priority": "restrictions and event clauses before background; no OCR",
    }


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
            coverage.append(
                Coverage(
                    name,
                    "possibly_truncated" if len(items) >= 100 else "sampled",
                    len(items),
                    window_detail(config, now)
                    + "; feed snapshot only; historical archive not queried",
                )
            )
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

        try:
            api = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20)
        except Exception as exc:
            coverage.extend(
                Coverage(f"tushare:{source}", "failed", detail=type(exc).__name__)
                for source in news_sources
            )
            return evidence, coverage
        start, end = source_window(config, now)
        for source in news_sources:
            source_count, failures, invalid, gaps = 0, 0, 0, []
            seen_news = set()
            intervals, cursor = [], start
            while cursor < end and len(intervals) < 8:
                next_midnight = datetime.combine(
                    cursor.date() + timedelta(days=1), time(), SHANGHAI
                )
                slice_end = end if len(intervals) == 7 else min(next_midnight, end)
                intervals.append((cursor, slice_end))
                cursor = slice_end
            slices = len(intervals)
            for slice_start, slice_end in intervals:
                try:
                    query = {
                        "src": source,
                        "start_date": slice_start.strftime("%Y-%m-%d %H:%M:%S"),
                        "end_date": slice_end.strftime("%Y-%m-%d %H:%M:%S"),
                    }

                    def fetch_news(query=query):
                        return [
                            {key: row.get(key) for key in ("datetime", "title", "content")}
                            for row in api.news(**query).to_dict("records")
                        ]

                    rows, collected = source_cache(
                        config,
                        "news",
                        json.dumps(query, sort_keys=True),
                        now,
                        fetch_news,
                        historical=slice_end <= datetime.combine(now.date(), time(), SHANGHAI),
                    )
                except Exception as exc:
                    failures += 1
                    gaps.append(
                        {
                            "start": slice_start.isoformat(),
                            "end": slice_end.isoformat(),
                            "status": "failed",
                            "failure_type": type(exc).__name__,
                        }
                    )
                    continue
                if len(rows) >= 100:
                    gaps.append(
                        {
                            "start": slice_start.isoformat(),
                            "end": slice_end.isoformat(),
                            "status": "possibly_truncated",
                        }
                    )
                for row in rows[:100]:
                    # Tushare news datetime is China local time; no supplied URL invented.
                    try:
                        published = datetime.fromisoformat(str(row["datetime"]))
                    except (KeyError, ValueError, TypeError):
                        invalid += 1
                        continue
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=SHANGHAI)
                    if not slice_start <= published <= slice_end:
                        continue
                    identity = (
                        published.isoformat(),
                        str(row.get("title")),
                        str(row.get("content")),
                    )
                    if identity in seen_news:
                        continue
                    seen_news.add(identity)
                    evidence.append(
                        Evidence(
                            source=f"tushare:{source}",
                            title=str(row.get("title") or "快讯")[:500],
                            body=str(row.get("content") or "")[:6000],
                            url=None,
                            published_at=published.isoformat(),
                            retrieved_at=collected,
                        )
                    )
                    source_count += 1
            status = (
                "failed"
                if failures == slices
                else "possibly_truncated"
                if gaps
                else "partial"
                if invalid
                else "sampled"
                if source_count
                else "empty_unconfirmed"
            )
            coverage.append(
                Coverage(
                    f"tushare:{source}",
                    status,
                    source_count,
                    window_detail(config, now)
                    + f"; {int(slices)}/8 bounded requests, 100 rows/request; "
                    + json.dumps(
                        {"coverage_gaps": gaps, "invalid_rows": invalid}, ensure_ascii=False
                    ),
                )
            )
    return evidence, coverage
