"""User-exported public comments are sampled, unverified claims, not order flow."""

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from quantlab.scout.disclosures import read_json
from quantlab.scout.models import Evidence, fingerprint, timestamp, web_url


def load_comments(path: Path, cutoff: datetime, eligible: set[str]) -> tuple[list, dict, dict]:
    data = read_json(path, 2_000_000)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Expected comments schema version 1")
    source, method = data["source"], data["sampling_method"]
    if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in (source, method)):
        raise ValueError("Comment source and sampling method required")
    retrieved = timestamp(data["retrieved_at"])
    start, end = timestamp(data["window_start"]), timestamp(data["window_end"])
    if not start < end <= retrieved <= cutoff:
        raise ValueError("Comment sample time window inconsistent")
    rows = data["comments"]
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("At most 500 comments per import")
    grouped, seen = defaultdict(list), {}
    excluded, duplicate_ids = 0, 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Comment rows must be objects")
        code = row["instrument_id"]
        if not isinstance(code, str) or len(code) > 16:
            raise ValueError("Comment needs a stock code")
        body = row["text"]
        published = timestamp(row["published_at"])
        if not start <= published < end:
            raise ValueError("Comment outside declared sample window")
        if not isinstance(body, str) or not body.strip() or len(body) > 2000:
            raise ValueError("Comment text must contain 1-2000 characters")
        if row.get("url") and not web_url(row["url"]):
            raise ValueError("Comment link must be HTTP(S)")
        author = row.get("author_id")
        if author is not None and (not isinstance(author, str) or len(author) > 200):
            raise ValueError("author_id must be a short string")
        normalized = {
            "instrument_id": code,
            "text": " ".join(body.split()),
            "published_at": published.isoformat(),
            "url": row.get("url"),
            "author_hash": fingerprint([source, author]) if author else None,
        }
        identity = (
            fingerprint([source, row.get("comment_id")])
            if row.get("comment_id")
            else fingerprint(normalized)
        )
        if identity in seen:
            if seen[identity] != normalized:
                raise ValueError("Conflicting content for comment ID")
            duplicate_ids += 1
            continue
        seen[identity] = normalized
        if code not in eligible:
            excluded += 1
            continue
        grouped[code].append(normalized)
    evidence, contexts, admitted = [], {}, []
    for code, comments in sorted(grouped.items()):
        texts = Counter(row["text"] for row in comments)
        authors = {row["author_hash"] for row in comments if row["author_hash"]}
        summary = {
            "source": source,
            "sampling_method": method,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "retrieved_at": retrieved.isoformat(),
            "sample_count": len(comments),
            "unique_text_count": len(texts),
            "repeated_text_fraction": 1 - len(texts) / len(comments),
            "identified_author_count": len(authors),
            "unknown_author_comments": sum(row["author_hash"] is None for row in comments),
            "platform_total": None,
            "heat_growth": None,
            "sentiment_score": None,
            "caution": "用户提供的非代表性样本；时间和来源未独立核实；不推断平台总热度/资金/持仓。",
        }
        refs, picked = [], set()
        for row in sorted(comments, key=lambda x: timestamp(x["published_at"]), reverse=True):
            if row["text"] in picked or len(picked) >= 5:
                continue
            picked.add(row["text"])
            item = Evidence(
                source=source,
                title=f"{code} 讨论样本（未核实）",
                body=row["text"],
                url=row["url"],
                published_at=row["published_at"],
                retrieved_at=retrieved.isoformat(),
                kind="public_comment_unverified",
                instrument_ids=(code,),
            )
            evidence.append(item)
            refs.append(item.evidence_id)
        summary["evidence_ids"] = refs
        summary["texts_omitted_from_ai"] = len(texts) - len(picked)
        contexts[code] = summary
        admitted.extend(comments)
    audit = {
        "source": source,
        "sampling_method": method,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "records": admitted,
        "duplicate_ids": duplicate_ids,
        "ineligible_comments": excluded,
        "input_count": len(rows),
        "records_sha256": fingerprint(admitted),
    }
    return evidence, contexts, audit
