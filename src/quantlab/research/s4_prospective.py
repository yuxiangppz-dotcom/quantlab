"""One weekend-registered price hypothesis; separate from same-day Forward Shadow."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from datetime import time as daytime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quantlab.data.dividend_raw import WireClient, strict_json
from quantlab.data.models import DataValidationError, canonical_payload_fingerprint
from quantlab.data.program_intake import Journal, acquire
from quantlab.data.tushare_provider import adj_factor_from_row, daily_bar_from_row
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import InputBinding, code_binding, sealed_read, verify_entries
from quantlab.research.s4_pilot import KEYS, adjusted_price, exact_grid
from quantlab.research.stock_replay_inputs import partition

CONFIG = "config/s4_prospective_observation_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/s4_prospective_20260913"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,vol,amount"
ADJ_FIELDS = "ts_code,trade_date,adj_factor"
SELECTION = "data/products/research_program/launch_20260912/selection.json"
CALENDAR = "data/canonical/calendar/calendar.parquet"
PARENT_CONFIG = "config/s4_conditional_reversal_diagnostics_v1.json"
PUBLIC_SOURCES = (
    "data/products/research_program/launch_20260912/s4_prospective_sources_20260913/public_read.txt"
)


def requests_for():
    return [
        {
            "id": api,
            "row_cap": 6000,
            "parameters": {
                "api_name": api,
                "params": {"trade_date": "20260911"},
                "fields": fields,
            },
        }
        for api, fields in (("daily", DAILY_FIELDS), ("adj_factor", ADJ_FIELDS))
    ]


def profile(raw, request):
    try:
        p = strict_json(raw)
        if type(p.get("code")) is not int:
            raise ValueError
        if p["code"] != 0:
            return {"status": "provider_error", "server_code": p["code"], "rows": None}
        names, rows = p["data"]["fields"], p["data"]["items"]
        required = request["parameters"]["fields"].split(",")
        if (
            not isinstance(names, list)
            or not all(isinstance(n, str) for n in names)
            or len(names) != len(set(names))
            or not set(required).issubset(names)
            or not isinstance(rows, list)
        ):
            raise ValueError
        keys = []
        for values in rows:
            if not isinstance(values, list) or len(values) != len(names):
                raise ValueError
            row = dict(zip(names, values, strict=True))
            if (
                not isinstance(row["ts_code"], str)
                or re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row["ts_code"]) is None
                or row["trade_date"] != "20260911"
            ):
                raise ValueError
            for name in required[2:]:
                value = row[name]
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value)
                ):
                    raise ValueError
            keys.append((row["ts_code"], row["trade_date"]))
        if len(keys) != len(set(keys)):
            raise ValueError
        saturated = len(rows) >= request["row_cap"]
        for obj in (p, p["data"]):
            saturated |= bool(obj.get("has_more") or obj.get("truncated"))
            saturated |= isinstance(obj.get("total"), (int, float)) and obj["total"] > len(rows)
        return {
            "status": "saturated"
            if saturated
            else ("nonempty" if rows else "empty_response_terminal"),
            "rows": len(rows),
            "server_code": 0,
        }
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return {"status": "schema_error", "rows": None}


def calendar_window(frame):
    if frame[["exchange", "trade_date", "is_open"]].isna().any().any():
        raise DataValidationError("calendar contains unknown states")
    if frame.duplicated(["exchange", "trade_date"]).any():
        raise DataValidationError("duplicate calendar day")
    days = {}
    for exchange in ("SSE", "SZSE"):
        g = frame[frame.exchange.eq(exchange)].copy()
        if not g.is_open.map(lambda x: type(x) is bool).all():
            raise DataValidationError("calendar open state must be boolean")
        g["trade_date"] = pd.to_datetime(g.trade_date)
        # Require both exchanges' complete daily calendar over the bounded window.
        g = g[g.trade_date.between("2026-08-14", "2026-09-21")]
        if sorted(g.trade_date.tolist()) != list(pd.date_range("2026-08-14", "2026-09-21")):
            raise DataValidationError("calendar daily coverage gap")
        days[exchange] = sorted(g[g.is_open].trade_date.dt.strftime("%Y-%m-%d").tolist())
    if days["SSE"] != days["SZSE"]:
        raise DataValidationError("exchange calendars disagree")
    sessions = days["SSE"]
    i = sessions.index("2026-09-11")
    window, future = sessions[: i + 1], sessions[i + 1 :]
    if (
        len(window) != 21
        or len(future) != 6
        or future[0] != "2026-09-14"
        or future[-1] != "2026-09-21"
    ):
        raise DataValidationError("fixed feature/future reference dates changed")
    return window, future


def timing(created_at, source_at):
    if (
        not isinstance(created_at, datetime)
        or not isinstance(source_at, datetime)
        or created_at.utcoffset() is None
        or source_at.utcoffset() is None
    ):
        raise DataValidationError("observation timestamps must be aware")
    if created_at < source_at:
        raise DataValidationError("observation predates its inputs")
    if created_at.astimezone(SHANGHAI).date() != date(2026, 9, 13):
        raise DataValidationError("observation is outside its preregistered publication day")
    if source_at < datetime(2026, 9, 11, 16, tzinfo=SHANGHAI):
        raise DataValidationError("new observation needs a current source availability bound")
    cutoff = datetime.combine(date(2026, 9, 14), daytime(9, 30), SHANGHAI)
    if created_at >= cutoff:
        raise DataValidationError("future observation window has already begun")
    return {
        "policy": "one_weekend_registration_before_future_reference_window_v1",
        "created_at": created_at.isoformat(),
        "source_available_at": source_at.isoformat(),
        "price_as_of": "2026-09-11",
        "publication_cutoff": cutoff.isoformat(),
        "old_same_day_forward_shadow_eligible": False,
        "future_window_not_started": True,
    }


def score_observation(frame, dates, codes):
    if len(dates) != 21 or dates[-1] != "2026-09-11":
        raise DataValidationError("observation requires its fixed 21-session trailing window")
    g = exact_grid(frame, dates, codes, allow_missing=True)
    prices = adjusted_price(g).to_numpy().reshape(len(codes), 21)
    changes = {}
    for n in (3, 20):
        part = prices[:, -n - 1 :]
        valid = np.isfinite(part).all(axis=1)
        values = np.full(len(codes), np.nan)
        values[valid] = part[valid, -1] / part[valid, 0] - 1
        values[~np.isfinite(values)] = np.nan
        changes[n] = values
    finite = changes[3][np.isfinite(changes[3])]
    median = np.median(finite) if len(finite) else np.nan
    scores = pd.DataFrame(
        {
            "instrument_id": codes,
            "price_as_of": dates[-1],
            "change3": changes[3],
            "change20": changes[20],
            "S4-A": median - changes[3],
            "reference_reversal20": -changes[20],
        }
    )
    scores["S4_A_known"] = scores["S4-A"].notna()
    scores["reference20_known"] = scores.reference_reversal20.notna()
    return g, scores


def _wire_frame(path, api):
    p = strict_json(path.read_bytes())["data"]
    rows = [dict(zip(p["fields"], values, strict=True)) for values in p["items"]]
    convert = daily_bar_from_row if api == "daily" else adj_factor_from_row
    return pd.DataFrame([asdict(convert(row)) for row in rows])


def validate_config(c):
    fixed = {
        "schema": "s4_prospective_observation_v1",
        "output": OUTPUT,
        "price_as_of": "2026-09-11",
        "publication_day": "2026-09-13",
        "max_requests": 2,
        "max_body_bytes": 16777216,
        "per_response_bytes": 8388608,
        "row_cap": 6000,
        "max_actual_attempts": 1,
        "economic_paths": 0,
        "model_fits": 0,
        "endpoint": "https://api.tushare.pro",
        "calendar_path": CALENDAR,
        "selection_path": SELECTION,
        "public_sources_path": PUBLIC_SOURCES,
    }
    if any(c.get(k) != v or type(c.get(k)) is not type(v) for k, v in fixed.items()):
        raise DataValidationError("unreviewed prospective contract")
    expected_inputs = set(c["canonical_paths"]) | {
        CALENDAR,
        SELECTION,
        PUBLIC_SOURCES,
        PARENT_CONFIG,
    }
    if set(c["inputs"]) != expected_inputs or len(c["canonical_paths"]) != 40:
        raise DataValidationError("prospective inputs must exactly bind all sources")
    for entry in c["inputs"].values():
        if (
            set(entry) != {"bytes", "sha256"}
            or type(entry["bytes"]) is not int
            or entry["bytes"] <= 0
            or not isinstance(entry["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
        ):
            raise DataValidationError("invalid prospective input binding")
    if c["resources"] != {
        "max_generated_bytes": 64 * 1024**2,
        "max_rss_bytes": 2 * 1024**3,
        "max_wakeup_seconds": 900,
        "next_partition_time_reserve_seconds": 60,
        "reserve_host_D_bytes": 8 * 1024**3,
    }:
        raise DataValidationError("prospective resource limits changed")


def load_contract(root):
    c = json.loads((root / CONFIG).read_text())
    validate_config(c)
    verify_entries(root, c["inputs"])
    if any((root / p).stat().st_size != e["bytes"] for p, e in c["inputs"].items()):
        raise DataValidationError("prospective bound input byte count changed")
    window, future = calendar_window(pd.read_parquet(root / c["calendar_path"]))
    expected_paths = [
        f"data/canonical/{kind}/year={d[:4]}/month={d[5:7]}/{d}.parquet"
        for d in window[:-1]
        for kind in ("daily", "adj_factor")
    ]
    if (
        c["window_dates"] != window
        or c["label_dates"] != future
        or c["canonical_paths"] != expected_paths
    ):
        raise DataValidationError("bound price/calendar window changed")
    selection = sealed_read(root / c["selection_path"])
    expected = "04a36754511be4136685a3ce378d60aa8c746ca218da7fa66d62df23b2e5840e"
    if selection["fingerprint"] != expected or c["selection_fingerprint"] != expected:
        raise DataValidationError("prospective cohort differs from frozen 2019 cohort")
    codes = selection["instrument_ids"]
    if len(codes) != 256 or sorted(set(codes)) != codes:
        raise DataValidationError("prospective cohort grid invalid")
    return c, codes


def require_unused(out):
    if any(p.name != "worker.lock" for p in out.iterdir()):
        raise DataValidationError("prospective actual attempt already consumed")


def run(root, *, client_factory=WireClient):
    c, codes = load_contract(root)
    binding = InputBinding(root)
    head = code_binding(root, binding)
    if (
        head
        != subprocess.check_output(["git", "rev-parse", "@{upstream}"], cwd=root, text=True).strip()
    ):
        raise DataValidationError("prospective source must be pushed")
    now = datetime.now(UTC)
    timing(now, now)
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise DataValidationError("configured provider credential unavailable to this worker")
    out = root / OUTPUT
    with exclusive_job(root / "data/runtime/research/heavy_job"), exclusive_job(out):
        require_unused(out)
        budget = Budget(out, c["resources"])
        budget.check(projected_bytes=20 * 1024**2, projected_memory=256 * 1024**2)
        intent = atomic_seal(
            out / "started.json",
            {
                "at": now.isoformat(),
                "source_head": head,
                "config_sha256": _sha(root / CONFIG),
                "code_inputs": binding.entries,
            },
        )
        identity = canonical_payload_fingerprint({"config": c, "source_head": head})
        journal = Journal(out, c, requests_for(), identity, inspector=profile)
        client = None
        try:
            client = client_factory(token, c["endpoint"])
            with budget.watchdog():
                stop = acquire(journal, budget, client, gap_seconds=60.1 / 30)
                intake = atomic_seal(
                    out / "intake_report.json", {**journal.summary(), "stop": stop}
                )
                if len(journal.records) != 2 or any(
                    r["status"] != "nonempty" for _, _, r in journal.records
                ):
                    raise DataValidationError(
                        "fresh input responses incomplete; no observation published"
                    )
                pieces = {"daily": [], "adj_factor": []}
                for path in c["canonical_paths"]:
                    kind = "adj_factor" if "/adj_factor/" in path else "daily"
                    day = date.fromisoformat(path.rsplit("/", 1)[-1].removesuffix(".parquet"))
                    f = partition(root / path, day)
                    pieces[kind].append(f[f.instrument_id.isin(codes)])
                for api in pieces:
                    f = _wire_frame(out / "attempts" / api / "response.body", api)
                    pieces[api].append(f[f.instrument_id.isin(codes)])
                combined = {api: pd.concat(parts) for api, parts in pieces.items()}
                for f in combined.values():
                    f["trade_date"] = pd.to_datetime(f.trade_date)
                joined = combined["daily"].merge(
                    combined["adj_factor"], on=KEYS, how="outer", validate="one_to_one"
                )
                inputs, scores = score_observation(joined, c["window_dates"], codes)
                inputs.to_parquet(out / "bound_price_inputs.parquet", index=False)
                scores.to_parquet(out / "scores.parquet", index=False)
                binding.check()
                verify_entries(root, c["inputs"])
                available = max(datetime.fromisoformat(r["at"]) for _, _, r in journal.records)
                created = datetime.now(UTC)
                admission = timing(created, max(now, available))
                report = atomic_seal(
                    out / "observation.json",
                    {
                        "schema": "s4_weekend_price_observation_v1",
                        "source_head": head,
                        "intent_fingerprint": intent["fingerprint"],
                        "intake_fingerprint": intake["fingerprint"],
                        "timing": admission,
                        "label_dates": c["label_dates"],
                        "rows": len(scores),
                        "known_S4_A": int(scores["S4-A"].notna().sum()),
                        "known_reference20": int(scores.reference_reversal20.notna().sum()),
                        "future_diagnostic_pending": True,
                        "minimum_future_pairs": 30,
                        "files": {
                            name: {"sha256": _sha(out / name), "bytes": (out / name).stat().st_size}
                            for name in ("bound_price_inputs.parquet", "scores.parquet")
                        },
                        "broker_order": False,
                        "performance_evidence": False,
                        "execution_authority": False,
                        "economic_paths": 0,
                        "model_fits": 0,
                        "candidate_promoted": False,
                        "resources": {
                            "seconds": time.monotonic() - budget.started,
                            "peak_rss_bytes": peak_rss_bytes(),
                            "bytes": budget.check(),
                        },
                    },
                )
                return report
        except Exception as exc:
            atomic_seal(
                out / "failed.json",
                {
                    "error_type": type(exc).__name__,
                    "intent_fingerprint": intent["fingerprint"],
                    "provider_requests": len(journal.records),
                    "observation_published": (out / "observation.json").exists(),
                },
            )
            raise
        finally:
            if client is not None:
                client.close()
