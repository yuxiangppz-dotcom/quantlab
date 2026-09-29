"""Bounded disclosure snapshots, with no inferred publication times or investor identities."""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from quantlab.scout.models import SHANGHAI, Coverage, Evidence, fingerprint, finite, timestamp

DATASETS = ("top_list", "top_inst", "block_trade")
ROW_LIMIT = 1000
FIELDS = {
    "top_list": ("reason", "l_buy", "l_sell", "net_amount", "amount", "net_rate"),
    "top_inst": ("reason", "exalter", "side", "buy", "sell", "net_buy"),
    "block_trade": ("price", "vol", "amount", "buyer", "seller"),
}
STRINGS = {"reason", "exalter", "side", "buyer", "seller"}


def disclosure_window(reason: str | None, trade_date: str) -> dict:
    """Label the observation window; the trade date is only its end date."""
    text = reason or ""
    match = re.search(r"连续([一二三四五六七八九十\d]+)个?交易日", text)
    if match or "累计" in text:
        sessions = match.group(1) if match else None
        span = f"{sessions}个交易日" if sessions else "多日"
        return {
            "window_type": "multi_session",
            "window_sessions": sessions,
            "window_label": f"截至{trade_date}的{span}累计披露，非{trade_date}单日资金",
        }
    if re.search(r"单日|当日|日涨幅|日收盘|日换手|日价格", text):
        return {
            "window_type": "single_session",
            "window_sessions": "一",
            "window_label": f"{trade_date}单日披露",
        }
    return {
        "window_type": "unknown",
        "window_sessions": None,
        "window_label": f"截至{trade_date}的披露，统计窗口未确认",
    }


def read_json(path: Path, max_bytes: int = 10_000_000):
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Source import exceeds size limit")
    return json.loads(raw)


def normalized_rows(dataset: str, rows: list, day: date) -> list[dict]:
    """Keep provider allowlisted fields only; malformed dates/numbers fail the snapshot."""
    if not isinstance(rows, list) or len(rows) > ROW_LIMIT:
        raise ValueError("Disclosure row limit exceeded")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Disclosure rows must be objects")
        code = row["ts_code"]
        actual = date.fromisoformat(str(row["trade_date"]))
        if actual != day or not isinstance(code, str) or len(code) > 16:
            raise ValueError("Disclosure date/code mismatch")
        record = {"instrument_id": code, "trade_date": day.isoformat()}
        for field in FIELDS[dataset]:
            value = row.get(field)
            if value is None or (isinstance(value, float) and value != value):
                value = None
            elif field in STRINGS:
                value = str(value).strip()
                if len(value) > 500:
                    raise ValueError("Disclosure text too long")
            else:
                if isinstance(value, bool):
                    raise ValueError("Boolean is not a financial amount")
                value = float(value)
                if not finite(value):
                    raise ValueError("Nonfinite financial amount")
                if field in {"buy", "sell", "l_buy", "l_sell", "price", "vol", "amount"}:
                    if value < 0:
                        raise ValueError("Negative price, volume or gross amount")
            record[field] = value
        if dataset == "top_inst" and record["side"] not in {"0", "1"}:
            raise ValueError("Unknown seat side")
        if dataset == "block_trade" and (
            record["price"] is None
            or record["price"] <= 0
            or record["vol"] is None
            or record["vol"] <= 0
        ):
            raise ValueError("Block trade needs positive price and volume")
        if dataset == "block_trade" and not finite(record["price"] * record["vol"] * 10000):
            raise ValueError("Block trade notional overflows")
        result.append(record)
    return result


def normalize_snapshot(raw: dict, days: list[date], cutoff: datetime) -> dict:
    dataset = raw["dataset"]
    day = date.fromisoformat(raw["trade_date"])
    retrieved = timestamp(raw["retrieved_at"])
    published = timestamp(raw["published_at"]) if raw.get("published_at") else None
    if dataset not in DATASETS or day not in days:
        raise ValueError("Unexpected disclosure dataset/session")
    if retrieved > cutoff or retrieved.astimezone(SHANGHAI).date() < day:
        raise ValueError("Disclosure retrieval time inconsistent")
    if published and (published > retrieved or published.astimezone(SHANGHAI).date() < day):
        raise ValueError("Disclosure publication time inconsistent")
    rows = normalized_rows(dataset, raw["rows"], day)
    truncated = raw.get("possibly_truncated", False)
    if type(truncated) is not bool:
        raise ValueError("possibly_truncated must be boolean")
    return {
        "dataset": dataset,
        "trade_date": day.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "published_at": published.isoformat() if published else None,
        "possibly_truncated": truncated or len(rows) >= ROW_LIMIT,
        "records": rows,
    }


def collect_disclosures(
    days: list[date],
    online: bool,
    enabled: bool,
    snapshot_path: Path | None = None,
) -> tuple[list[dict], list[Coverage]]:
    snapshots, coverage = [], []
    if snapshot_path:
        imported = read_json(snapshot_path)
        if (
            not isinstance(imported, dict)
            or imported.get("schema_version") != 1
            or not isinstance(imported.get("snapshots"), list)
        ):
            raise ValueError("Expected disclosure snapshot schema version 1")
        if len(imported["snapshots"]) > len(days) * len(DATASETS):
            raise ValueError("Too many disclosure snapshots")
        cutoff = datetime.now(SHANGHAI)
        snapshots = [normalize_snapshot(row, days, cutoff) for row in imported["snapshots"]]
        keys = [(s["dataset"], s["trade_date"]) for s in snapshots]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate dataset/session snapshot")
        for day in days:
            for dataset in DATASETS:
                if (dataset, day.isoformat()) not in keys:
                    coverage.append(Coverage(f"{dataset}:{day}", "not_provided"))
    elif not online or not enabled or not os.environ.get("TUSHARE_TOKEN"):
        return [], [Coverage("trading_disclosures", "disabled", detail="offline/config/token")]
    else:
        import tushare as ts

        try:
            api = ts.pro_api(os.environ["TUSHARE_TOKEN"], timeout=20)
        except Exception as exc:
            return [], [Coverage("trading_disclosures", "failed", detail=type(exc).__name__)]
        for day in days:
            for dataset in DATASETS:
                try:
                    frame = getattr(api, dataset)(trade_date=day.strftime("%Y%m%d"))
                    rows = frame.to_dict("records")
                    raw = {
                        "dataset": dataset,
                        "trade_date": day.isoformat(),
                        "retrieved_at": datetime.now(SHANGHAI).isoformat(),
                        "published_at": None,
                        "rows": rows[:ROW_LIMIT],
                        "possibly_truncated": len(rows) >= ROW_LIMIT,
                    }
                    snapshots.append(normalize_snapshot(raw, days, datetime.now(SHANGHAI)))
                except Exception as exc:
                    coverage.append(
                        Coverage(f"{dataset}:{day}", "failed", detail=type(exc).__name__)
                    )
    for snap in snapshots:
        status = "possibly_truncated" if snap["possibly_truncated"] else "ok"
        if not snap["records"]:
            status = "empty_unconfirmed"
        snap["origin"] = "user_import_unverified" if snapshot_path else "tushare"
        coverage.append(
            Coverage(
                f"{snap['dataset']}:{snap['trade_date']}",
                status,
                len(snap["records"]),
                f"{snap['origin']}; empty is not proof of absence; publication may be unknown",
            )
        )
    return snapshots, coverage


def disclosure_context(snapshots: list[dict], universe: dict, closes: dict) -> tuple[list, dict]:
    """Per-stock evidence; never sum overlapping leaderboard windows or identify real people."""
    grouped = defaultdict(list)
    for snap in snapshots:
        for row in snap["records"]:
            if row["instrument_id"] in universe:
                grouped[(row["instrument_id"], snap["dataset"])].append((row, snap))
    evidence, contexts = [], defaultdict(dict)
    for (code, dataset), pairs in sorted(grouped.items()):
        records, seen = [], {}
        duplicate = 0
        for row, snap in pairs:
            # A seat may appear in both buy/sell rankings. Merge rank-side membership only.
            identity = fingerprint({k: v for k, v in row.items() if k != "side"})
            if identity in seen and dataset != "block_trade":
                duplicate += 1
                if dataset == "top_inst" and row["side"] not in seen[identity]["rank_sides"]:
                    seen[identity]["rank_sides"].append(row["side"])
                continue
            value = dict(row)
            if dataset in {"top_list", "top_inst"}:
                value.update(disclosure_window(row.get("reason"), row["trade_date"]))
            if dataset == "top_list":
                value["amount_unit"] = "provider raw; unverified unit; not cross-source summed"
            if dataset == "top_inst":
                value["rank_sides"] = [value.pop("side")]
                value["amount_unit"] = "CNY"
            if dataset == "block_trade":
                close = closes.get((code, row["trade_date"]))
                value["volume_shares"] = row["vol"] * 10000
                value["derived_notional_cny"] = row["price"] * value["volume_shares"]
                close = close if finite(close) and close > 0 else None
                value["close_reference"] = close
                value["premium_to_close_pct"] = (row["price"] / close - 1) * 100 if close else None
                value["amount_unit"] = "provider amount unit unspecified; not summed"
            value["retrieved_at"] = snap["retrieved_at"]
            value["published_at"] = snap["published_at"]
            value["origin"] = snap["origin"]
            seen[identity] = value
            records.append(value)
        records.sort(key=lambda r: r["trade_date"], reverse=True)
        dates = sorted({row["trade_date"] for row, _ in pairs})
        caution = (
            "披露样本，不代表全部资金；原始reason保留，不合计单日/多日或不同上榜原因；"
            "相同记录可能重复或无法区分，不识别具体投资者；大宗折溢价不直接表示利好利空。"
        )
        payload = {
            "dataset": dataset,
            "observed_trade_dates": dates,
            "records": records[:20],
            "record_count": len(records),
            "duplicate_or_indistinguishable_rows": duplicate,
            "omitted_from_text": max(0, len(records) - 20),
            "caution": caution,
            "possibly_truncated": any(s["possibly_truncated"] for _, s in pairs),
        }
        item = Evidence(
            source=f"disclosure:{dataset}",
            title=f"{code} {dataset} 披露记录",
            body=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            url=None,
            published_at=None,
            retrieved_at=max((s["retrieved_at"] for _, s in pairs), key=timestamp),
            kind="trading_disclosure",
            instrument_ids=(code,),
            event_dates=tuple(dates),
        )
        evidence.append(item)
        contexts[code][dataset] = {
            "observed_trade_dates": dates,
            "record_count": len(records),
            "duplicate_or_indistinguishable_rows": duplicate,
            "evidence_id": item.evidence_id,
            "records": records[:20],
            "omitted_from_summary": max(0, len(records) - 20),
            "possibly_truncated": any(s["possibly_truncated"] for _, s in pairs),
        }
    return evidence, dict(contexts)
