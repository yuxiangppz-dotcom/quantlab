"""Bounded official notice indexes and machine PDF evidence, in an isolated cache.

Only evidence actually available by the caller's cutoff enters model records.
Date-only archive labels never become precise publication timestamps or novelty.
Machine extraction is not an independently verified legal interpretation.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from quantlab.scout.article_risks import day_key, stable_id
from quantlab.scout.models import SHANGHAI, Evidence
from quantlab.scout.pipeline import read_config
from quantlab.scout.sources import (
    CNINFO_PDF,
    collect_cninfo_announcements,
    collect_cninfo_pdf_bodies,
)

NOTICE_CONFIG = {
    "version": "article_official_notices_v1",
    "lookback_hours": 168,
    "max_stocks": 24,
    "max_index_pages_per_stock": 2,
    "max_pdf_per_stock": 1,
    "max_pdf_pages": 12,
    "max_pdf_bytes": 2_000_000,
    "max_body_chars": 11_000,
    "machine_extraction_not_independent_verification": True,
}
RISK_TERMS = {
    "loss_forecast": r"预亏|亏损|业绩预告",
    "regulatory_inquiry": r"问询|监管函|关注函",
    "investigation": r"立案|调查通知|行政处罚|重大违法",
    "holder_sell": r"减持",
    "unlock": r"解禁|限售.{0,12}上市|限售.{0,12}流通",
    "trading_qualification": r"退市|暂停上市|停牌|交易限制|风险警示",
}


def _stamp(value) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(SHANGHAI) if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    raise ValueError("Unsupported notice evidence record")


def _write_once(path: Path, value: dict, *, root: Path) -> dict:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Notice receipt resolves outside isolated cache")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return value
    except FileExistsError:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("identity") != value.get("identity"):
            raise ValueError("Notice cache receipt identity conflict") from None
        return saved


def classify_notice_risk(
    title: str,
    body: str,
    *,
    index_only: bool,
    confirmation: dict | None = None,
    expected_code: str | None = None,
    effective_date: str | None = None,
) -> list[dict]:
    """Potential risks require context; title keywords never become hard exclusions.

    An optional structured qualification receipt is a separately verified source
    assertion, not a regex or the machine extraction's self-assessment.
    """
    result = []
    for kind, pattern in RISK_TERMS.items():
        in_title = re.search(pattern, title)
        in_body = re.search(pattern, body) if not index_only else None
        if in_title is None and in_body is None:
            continue
        action = "review_required" if index_only else "watch_only"
        if kind == "loss_forecast" and in_body is None and not re.search(r"预亏|亏损", title):
            action = "pass"
        excerpt_source = title if index_only or in_body is None else body
        match = in_title if index_only or in_body is None else in_body
        start = max(0, match.start() - 50) if match is not None else 0
        result.append(
            {
                "type": kind,
                "action": action,
                "basis": "index_title_requires_body"
                if index_only
                else "machine_body_requires_context",
                "excerpt": excerpt_source[start : start + 220],
                "confirmed_severity": False,
                "does_not_prove_all_future_plans_covered": kind in {"holder_sell", "unlock"},
            }
        )
    if confirmation is not None and not isinstance(confirmation, dict):
        result.append(
            {
                "type": "structured_qualification",
                "action": "review_required",
                "basis": "invalid_separate_qualification_receipt",
                "confirmed_severity": False,
                "reason_code": "qualification_receipt_malformed",
            }
        )
        return result
    if confirmation and confirmation.get("confirmed_risk") is True:
        scope = confirmation.get("scope")
        period = confirmation.get("effective_period")
        subject = confirmation.get("ts_code")
        start = day_key(period.get("start")) if isinstance(period, dict) else None
        end = day_key(period.get("end")) if isinstance(period, dict) else None
        effective = day_key(effective_date)
        trusted = (
            all(
                confirmation.get(key) is True
                for key in (
                    "subject_verified",
                    "scope_verified",
                    "effective_period_verified",
                    "source_verified",
                )
            )
            and bool(confirmation.get("source_id"))
            and (
                isinstance(scope, dict)
                and bool(scope)
                and scope.get("ts_code") == subject
                and scope.get("qualification") == confirmation.get("qualification_constraint")
                and isinstance(subject, str)
                and subject == expected_code
                and start is not None
                and effective is not None
                and start <= effective
                and (
                    end is None
                    and isinstance(period, dict)
                    and period.get("open_ended") is True
                    or end is not None
                    and effective <= end
                )
            )
        )
        constraint = confirmation.get("qualification_constraint")
        action = (
            "exclude"
            if trusted
            and isinstance(constraint, str)
            and constraint in {"delisting", "suspended", "trade_restricted", "st"}
            else "review_required"
        )
        result.append(
            {
                "type": "structured_qualification",
                "action": action,
                "basis": "separate_verified_qualification_receipt",
                "confirmed_severity": trusted,
                "source_id": confirmation.get("source_id"),
                "scope": confirmation.get("scope"),
                "effective_period": confirmation.get("effective_period"),
                "reason_code": "confirmed_qualification_restriction"
                if action == "exclude"
                else "confirmed_risk_scope_unresolved",
            }
        )
    return result


def collect_article_notices(
    root,
    codes,
    cutoff_at,
    *,
    online=False,
    max_stocks=24,
    collector=None,
    reader=None,
    config=None,
) -> dict:
    """Use old bounded adapters with new cache receipts; never alter their modules.

    Injectable collector/reader have the existing source function signatures and
    return (Evidence list, Coverage). Offline fixtures can exercise the whole
    contract; absent injections, offline mode never reaches a provider.
    """
    cutoff = _stamp(cutoff_at)
    if cutoff is None:
        raise ValueError("Article notice cutoff must have timezone")
    if type(max_stocks) is not int or not 0 <= max_stocks <= NOTICE_CONFIG["max_stocks"]:
        raise ValueError("Article notice stock budget must be within frozen 24-stock cap")
    supplied = dict(config or {})
    allowed = {
        "lookback_hours",
        "cninfo_announcements",
        "cninfo_pdf_bodies",
        "_source_window_start",
    }
    if set(supplied) - allowed:
        raise ValueError("Unknown or unsafe article notice configuration")
    root = Path(root)
    if online and (
        not root.resolve().name.startswith("scout_article_")
        or not (root / ".scout-article").is_file()
        or (root / ".scout-article").read_text().strip() != "quantlab-scout-article-v1"
    ):
        raise ValueError("Live notice collection requires explicit isolated article volume")
    fixed = read_config(None)
    fixed.update(
        {
            "lookback_hours": NOTICE_CONFIG["lookback_hours"],
            "cninfo_announcements": True,
            "cninfo_pdf_bodies": True,
            **supplied,
        }
    )
    if type(fixed["lookback_hours"]) is not int or not 1 <= fixed["lookback_hours"] <= 168:
        raise ValueError("Invalid fixed notice lookback")
    if not all(
        isinstance(fixed[key], bool) for key in ("cninfo_announcements", "cninfo_pdf_bodies")
    ):
        raise ValueError("Notice source flags must be boolean")
    if fixed.get("_source_window_start") is not None:
        start = _stamp(fixed["_source_window_start"])
        if start is None or start > cutoff:
            raise ValueError("Invalid fixed notice window start")
    cache = root / "article_notice_cache"
    if not cache.resolve().is_relative_to(root.resolve()):
        raise ValueError("Article notice cache resolves outside isolated root")
    fixed["_source_cache_root"] = str(cache)
    frozen = {**NOTICE_CONFIG, **supplied, "selected_stock_limit": max_stocks}
    config_hash = stable_id(frozen)
    unique = list(dict.fromkeys(codes))
    selected, omitted = unique[:max_stocks], unique[max_stocks:]
    result = {
        "version": NOTICE_CONFIG["version"],
        "cutoff_at": cutoff.isoformat(),
        "config_hash": config_hash,
        "records": {"announcements": []},
        "coverage": [],
        "costs": {
            "index_collector_calls": 0,
            "pdf_reader_calls": 0,
            "index_network_requests_upper_bound": 1 + 2 * len(selected) if online else 0,
            "pdf_network_requests_upper_bound": len(selected) if online else 0,
            "actual_network_request_count": None,
            "model_calls": 0,
        },
        "rejected_after_cutoff": [],
        "invalid_record_count": 0,
        "no_complete_future_risk_clearance_claim": True,
    }
    for code in omitted:
        result["coverage"].append(
            {
                "api": "announcements",
                "ts_code": code,
                "status": "unknown",
                "reason_code": "frozen_notice_stock_budget_exhausted",
            }
        )
    if not selected:
        return result
    index_collector = collector or collect_cninfo_announcements
    pdf_reader = reader or collect_cninfo_pdf_bodies
    try:
        result["costs"]["index_collector_calls"] = 1
        index_items, index_coverage = index_collector(
            fixed, cutoff, online, selected, max_targets=max_stocks
        )
        index_coverage = _dict(index_coverage)
    except Exception as exc:
        result["coverage"].extend(
            {
                "api": "announcements",
                "ts_code": code,
                "status": "unknown",
                "reason_code": "official_index_failed",
                "error_type": type(exc).__name__,
            }
            for code in selected
        )
        return result
    admitted, original_by_url = [], {}
    for item in index_items:
        try:
            value = _dict(item)
            ids = value.get("instrument_ids", ())
            seen = _stamp(value.get("retrieved_at"))
            published = _stamp(value["published_at"]) if value.get("published_at") else None
            if seen is not None and (
                seen > cutoff or (published is not None and published > cutoff)
            ):
                result["rejected_after_cutoff"].append(
                    {
                        "source_id": value.get("evidence_id"),
                        "url": value.get("url"),
                        "retrieved_at": value.get("retrieved_at"),
                        "published_at": value.get("published_at"),
                    }
                )
                continue
            if (
                len(ids) != 1
                or ids[0] not in selected
                or not isinstance(ids[0], str)
                or not re.fullmatch(r"\d{6}\.(SH|SZ)", ids[0])
                or seen is None
                or not isinstance(value.get("title"), str)
                or not value["title"].strip()
                or value.get("source")
                not in {"cninfo:official_index", "cninfo:official_index_post_selection"}
                or not CNINFO_PDF.fullmatch(str(value.get("url", "")))
                or (value.get("published_at") and published is None)
                or (published is not None and published > seen)
                or any(
                    not day_key(day) or day_key(day) > cutoff.date().isoformat()
                    for day in value.get("event_dates", ())
                )
            ):
                raise ValueError("Invalid official index identity, title or timing")
            evidence = (
                item
                if isinstance(item, Evidence)
                else Evidence(
                    **{
                        key: value[key]
                        for key in (
                            "source",
                            "title",
                            "body",
                            "url",
                            "published_at",
                            "retrieved_at",
                            "kind",
                            "instrument_ids",
                            "event_dates",
                        )
                        if key in value
                    }
                )
            )
            existing = original_by_url.get(value["url"])
            if existing is not None:
                if existing[0].evidence_id != evidence.evidence_id:
                    raise ValueError("Official document has contradictory subject or content")
                continue
            identity = stable_id([ids[0], value["url"]])
            receipt = _write_once(
                cache / "first_seen" / f"{identity}.json",
                {
                    "identity": identity,
                    "ts_code": ids[0],
                    "url": value["url"],
                    "first_seen_at": seen.isoformat(),
                    "archive_date_not_publication": published is None,
                },
                root=cache,
            )
            if receipt.get("ts_code") != ids[0] or receipt.get("url") != value["url"]:
                raise ValueError("Invalid retained subject receipt")
            if (
                _stamp(receipt.get("first_seen_at")) is None
                or _stamp(receipt["first_seen_at"]) > cutoff
            ):
                raise ValueError("Invalid retained first-seen receipt")
            admitted.append(evidence)
            original_by_url[value["url"]] = (evidence, receipt)
        except (ValueError, TypeError, KeyError, OSError):
            result["invalid_record_count"] += 1
    chosen = {}
    for item in admitted:
        code = item.instrument_ids[0]
        rank = (
            bool(classify_notice_risk(item.title, "", index_only=True)),
            (item.event_dates or ("",))[0],
            item.url,
        )
        prior = chosen.get(code)
        if prior is None or rank > (
            bool(classify_notice_risk(prior.title, "", index_only=True)),
            (prior.event_dates or ("",))[0],
            prior.url,
        ):
            chosen[code] = item
    proxies = [
        Evidence(
            source="cninfo:official_index",
            title=item.title + "【股票交易风险正文补查】",
            body=item.body,
            url=item.url,
            published_at=item.published_at,
            retrieved_at=item.retrieved_at,
            kind=item.kind,
            instrument_ids=item.instrument_ids,
            event_dates=item.event_dates,
        )
        for item in chosen.values()
    ]
    pdf_by_url, pdf_coverage = {}, {"status": "disabled"}
    if proxies:
        try:
            result["costs"]["pdf_reader_calls"] = 1
            bodies, coverage = pdf_reader(fixed, cutoff, online, proxies, max_stocks=max_stocks)
            pdf_coverage = _dict(coverage)
            for item in bodies:
                value = _dict(item)
                original = original_by_url.get(value.get("url"))
                seen = _stamp(value.get("retrieved_at"))
                published = _stamp(value["published_at"]) if value.get("published_at") else None
                if seen is not None and (
                    seen > cutoff or published is not None and published > cutoff
                ):
                    result["rejected_after_cutoff"].append(
                        {
                            "source_id": value.get("evidence_id"),
                            "url": value.get("url"),
                            "retrieved_at": value.get("retrieved_at"),
                        }
                    )
                    continue
                if (
                    original is None
                    or seen is None
                    or value.get("url") not in {item.url for item in proxies}
                    or tuple(value.get("instrument_ids", ())) != original[0].instrument_ids
                    or value.get("source") != "cninfo:official_pdf_text"
                    or value.get("published_at")
                    and (published is None or published > seen)
                    or tuple(value.get("event_dates", ())) != original[0].event_dates
                    or not isinstance(value.get("body"), str)
                    or len(value["body"].strip()) < 80
                ):
                    result["invalid_record_count"] += 1
                    continue
                if value["url"] in pdf_by_url:
                    result["invalid_record_count"] += 1
                    continue
                value["evidence_id"] = (
                    value.get("evidence_id")
                    or "ev-"
                    + stable_id(
                        [value["source"], value["url"], value["body"], value["instrument_ids"]]
                    )[:16]
                )
                identity = stable_id([value["url"], value["evidence_id"]])
                body_receipt = _write_once(
                    cache / "body_first_seen" / f"{identity}.json",
                    {
                        "identity": identity,
                        "source_id": value["evidence_id"],
                        "url": value["url"],
                        "first_seen_at": seen.isoformat(),
                    },
                    root=cache,
                )
                body_seen = _stamp(body_receipt.get("first_seen_at"))
                if body_seen is None or body_seen > cutoff:
                    result["invalid_record_count"] += 1
                    continue
                value["body_first_seen_at"] = body_seen.isoformat()
                pdf_by_url[value["url"]] = value
        except Exception as exc:
            pdf_coverage = {"status": "failed", "error_type": type(exc).__name__}
    for item in admitted:
        code = item.instrument_ids[0]
        receipt = original_by_url[item.url][1]
        extracted = pdf_by_url.get(item.url)
        body = extracted["body"] if extracted else ""
        source_id = extracted.get("evidence_id") if extracted else item.evidence_id
        result["records"]["announcements"].append(
            {
                "ts_code": code,
                "title": item.title,
                "body": body,
                "url": item.url,
                "_source_id": source_id,
                "source_ids": [item.evidence_id] + ([source_id] if extracted else []),
                "_first_seen_at": receipt["first_seen_at"],
                "body_first_seen_at": extracted.get("body_first_seen_at") if extracted else None,
                "_published_at": item.published_at,
                "announcement_dates": list(item.event_dates),
                "date_uncertainty": item.published_at is None,
                "novelty_status": "not_inferred_from_first_download",
                "index_only": extracted is None,
                "body_extracted": extracted is not None,
                "machine_text_not_independently_verified": True,
                "risk_flags": classify_notice_risk(
                    item.title,
                    body,
                    index_only=extracted is None,
                    confirmation=extracted.get("risk_confirmation") if extracted else None,
                    expected_code=code,
                    effective_date=cutoff.date().isoformat(),
                ),
                "reader_selection_marker_only": "股票交易风险正文补查" if extracted else None,
            }
        )
    for code in selected:
        records = [row for row in result["records"]["announcements"] if row["ts_code"] == code]
        missing_risk_bodies = [
            row["_source_id"] for row in records if row["index_only"] and row["risk_flags"]
        ]
        index_status = index_coverage.get("status")
        errors = result["invalid_record_count"] or result["rejected_after_cutoff"]
        status = (
            "available"
            if records
            and index_status in {"targeted_only", "ok", "complete"}
            and not missing_risk_bodies
            and not errors
            else ("partial" if records else "unknown")
        )
        result["coverage"].append(
            {
                "api": "announcements",
                "ts_code": code,
                "status": status,
                "rows": len(records),
                "body_count": sum(row["body_extracted"] for row in records),
                "targeted_only": True,
                "complete_risk_universe": False,
                "index_status": index_status,
                "pdf_status": pdf_coverage.get("status"),
                "important_index_without_body": missing_risk_bodies,
                "reason_code": "risk_body_missing"
                if missing_risk_bodies
                else "targeted_official_evidence"
                if status == "available"
                else "official_notice_coverage_unconfirmed",
                "index_detail": index_coverage.get("detail", ""),
            }
        )
    result["records"]["announcements"].sort(
        key=lambda row: (row["ts_code"], row["url"], row["_source_id"])
    )
    audit = {"identity": stable_id([cutoff.isoformat(), config_hash, result]), "result": result}
    _write_once(cache / "collections" / f"{audit['identity']}.json", audit, root=cache)
    return result
