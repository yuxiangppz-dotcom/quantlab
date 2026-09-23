"""Historical CSI800 membership -> auditable, dated research and holding eligibility.

Membership evidence is a COMPLETE constituent list with an explicit effective
interval and publication time. Monthly index_weight observations alone do not
establish those intervals. Raw storage is never filtered to today's members.
"""

import json
import shutil
from bisect import bisect_left, bisect_right
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quantlab.data.security_history import load_security_code_changes
from quantlab.pipeline.ingestion import calendar_days, verify_session
from quantlab.research.alpha158_store import exclusive_job
from quantlab.research.dataset import _build_list_dates
from quantlab.research.ml.artifacts import complete, verify_completed
from quantlab.research.ml.io import sha256, write_json

FLAGS = ["can_open", "must_exit", "soft_exit"]


def available_row(rows, day, hour, *, label):
    selected = [r for r in rows if r["start"] <= str(day) <= r["end"]]
    if len(selected) != 1:
        raise ValueError(f"missing/overlapping {label}:{day}")
    row = selected[0]
    cutoff = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=hour)
    if row.get("known_at") is None:
        raise ValueError(f"unavailable {label}:{day}:historical publication unknown")
    known = pd.Timestamp(row["known_at"])
    if (
        not row.get("source_id")
        or not row.get("revision_id")
        or known.tzinfo is None
        or known > cutoff
    ):
        raise ValueError(f"unavailable {label}:{day}")
    return row


def compile_universe(
    storage, receipts, availability_path, strategy, output, start, end, *, hour, code_changes_path
):
    """Build once, verify on retry. Missing evidence blocks instead of clearing ST.

    Liquidity affects new buys only. ST is a daily hard exit; index removal is a
    discretionary buffered exit. A halt never deletes a position or creates a fill.
    """
    paths = [strategy[k] for k in ("membership", "industries", "event_coverage")]
    paths += [availability_path, storage.calendar_path, storage.securities_path, code_changes_path]
    sources = {str(p): sha256(p) for p in paths}
    intent = {
        "compiler_sha256": sha256(Path(__file__)),
        "sources": sources,
        "policy": strategy["policy_sha256"],
        "start": str(start),
        "end": str(end),
        "hour": hour,
    }
    output = Path(output)
    with exclusive_job(output.parent):
        if output.exists():
            verify_completed(output)
            if json.loads((output / "intent.json").read_text()) != intent:
                raise ValueError("universe evidence changed; use a new workspace snapshot")
            for day, digest in json.loads((output / "receipts.json").read_text()).items():
                verify_session(storage, receipts, date.fromisoformat(day))
                if sha256(Path(receipts) / "sessions" / f"{day}.json") != digest:
                    raise ValueError("universe source receipt changed")
            return output
        membership = json.loads(strategy["membership"].read_text())
        if (
            membership.get("schema") != "quantlab_index_membership_v1"
            or membership.get("index") != "000906.SH"
            or membership.get("semantics") != "published_effective_intervals"
        ):
            raise ValueError("certified historical CSI800 effective membership required")
        snapshots = membership["snapshots"]
        for row in snapshots:
            if row.get("source_id") == "csi_index_weight_monthly_observation":
                raise ValueError("monthly observations cannot certify effective CSI800 membership")
            members = row["members"]
            if (
                row.get("complete") is not True
                or len(members) != 800
                or len(set(members)) != 800
                or any(not isinstance(c, str) or not c.endswith((".SH", ".SZ")) for c in members)
            ):
                raise ValueError("CSI800 snapshot requires exactly 800 distinct complete members")
        industries = json.loads(strategy["industries"].read_text())["intervals"]
        by_code = {}
        for row in industries:
            by_code.setdefault(row["instrument_id"], []).append(row)
        coverage = json.loads(strategy["event_coverage"].read_text())
        if coverage.get("schema") != "quantlab_event_coverage_v1":
            raise ValueError("explicit event coverage certification required")
        if any(
            r.get("source_id") == "tushare_stock_st_daily_sealed"
            for r in coverage["stock_st"]
        ):
            raise ValueError("sealed ST partitions and samples do not certify coverage")
        calendar = storage.load_trading_calendar()
        # Use the stored calendar, including listing-age and feature warmup history.
        all_days = sorted({c.trade_date for c in calendar})
        days = calendar_days(calendar, all_days[0], all_days[-1])
        requested = [d for d in days if start <= d <= end]
        if not requested:
            raise ValueError("no sessions in universe interval")
        first = days.index(requested[0])
        window = strategy["liquidity_window"]
        if first < window - 1:
            raise ValueError("insufficient universe liquidity warmup")
        listings = _build_list_dates(
            storage.load_securities(), load_security_code_changes(code_changes_path)
        )
        available = pd.read_parquet(availability_path).copy()
        available["trade_date"] = pd.to_datetime(available.trade_date).dt.date
        if available.trade_date.duplicated().any():
            raise ValueError("duplicate source availability")
        available = available.set_index("trade_date")
        history, price_history, benchmark_history, known_history, bindings, counts = (
            [],
            [],
            [],
            [],
            {},
            [],
        )
        # Keep removed constituents in the daily context for valuation/exit handling.
        seen = set()
        for snapshot in snapshots:
            if snapshot["start"] <= str(start):
                seen.update(snapshot["members"])
        with TemporaryDirectory(dir=output.parent) as temp, ExitStack() as stack:
            stage = Path(temp) / "universe"
            stage.mkdir()
            for number, path in enumerate(paths):
                shutil.copyfile(path, stage / f"source-{number}{Path(path).suffix}")
            writers = {}
            for day in days[max(0, first - max(window - 1, 60)) : days.index(requested[-1]) + 1]:
                verify_session(storage, receipts, day)
                bindings[str(day)] = sha256(Path(receipts) / "sessions" / f"{day}.json")
                if day not in available.index:
                    raise ValueError(f"publication evidence missing:{day}")
                observed = pd.Timestamp(available.loc[day, "known_at"])
                if observed.tzinfo is None or pd.isna(observed):
                    raise ValueError(f"invalid publication timestamp:{day}")
                known_history.append(observed)
                known_history = known_history[-max(61, window) :]
                bars = storage.load_daily_bars_by_date(day)
                history.append({b.instrument_id: b.amount for b in bars})
                history = history[-window:]
                factors = {
                    f.instrument_id: f.adj_factor for f in storage.load_adj_factors_by_date(day)
                }
                price_history.append(
                    {b.instrument_id: b.close * factors.get(b.instrument_id, np.nan) for b in bars}
                )
                price_history = price_history[-61:]
                index = [
                    r
                    for r in storage.load_index_daily_by_date(day)
                    if r.instrument_id == "000906.SH"
                ]
                benchmark_history.append(
                    index[0].close / index[0].pre_close - 1
                    if len(index) == 1 and index[0].pre_close > 0
                    else np.nan
                )
                benchmark_history = benchmark_history[-60:]
                if day < start:
                    continue
                member = available_row(snapshots, day, hour, label="CSI800 membership")
                event = available_row(coverage["stock_st"], day, hour, label="ST coverage")
                if event.get("complete") is not True:
                    raise ValueError(f"ST history unknown:{day}")
                st = {r.instrument_id for r in storage.load_stock_st_v1_by_date(day)}
                if len(st) >= 1000:
                    raise ValueError("ST response reaches provider limit")
                members = set(member["members"])
                seen.update(members)
                if day not in available.index:
                    raise ValueError(f"publication evidence missing:{day}")
                known = max(known_history)
                cutoff = pd.Timestamp(day).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=hour)
                if pd.Timestamp(known).tzinfo is None or pd.Timestamp(known) > cutoff:
                    raise ValueError(f"publication evidence unavailable:{day}")
                basics = {b.instrument_id: b for b in storage.load_daily_basic_by_date(day)}
                rows, exposures = [], []
                count = {
                    "trade_date": str(day),
                    "members": len(members),
                    "research": 0,
                    "can_open": 0,
                }
                for code in sorted(seen):
                    intervals = by_code.get(code, [])
                    if code not in members and not any(
                        r["start"] <= str(day) <= r["end"] for r in intervals
                    ):
                        # A long-removed, unheld name need not retain a fictitious
                        # current industry. If held, portfolio validation will
                        # explicitly block on this unknown value.
                        group = {"industry": None, "known_at": known}
                    else:
                        group = available_row(intervals, day, hour, label=f"industry:{code}")
                    if code in members and (
                        not isinstance(group.get("industry"), str) or not group["industry"].strip()
                    ):
                        raise ValueError(f"unknown industry:{code}:{day}")
                    listed = listings.get(code)
                    if listed is None:
                        raise ValueError(f"unknown listing date:{code}")
                    # If listing predates available calendar, only the observed lower bound counts.
                    age = bisect_right(days, day) - bisect_left(days, listed)
                    research = (
                        code in members
                        and code not in st
                        and age >= strategy["min_listing_sessions"]
                    )
                    amounts = [h.get(code, np.nan) for h in history]
                    liquid = len(amounts) == window and np.isfinite(amounts).all()
                    median = float(np.median(amounts)) if liquid else np.nan
                    can_open = research and liquid and median >= strategy["min_median_amount_cny"]
                    reasons = []
                    for failed, reason in (
                        (code not in members, "outside_csi800"),
                        (code in st, "st"),
                        (age < strategy["min_listing_sessions"], "listing_age"),
                        (not liquid, "liquidity_unknown"),
                        (liquid and median < strategy["min_median_amount_cny"], "liquidity"),
                    ):
                        if failed:
                            reasons.append(reason)
                    row = {
                        "trade_date": pd.Timestamp(day),
                        "instrument_id": code,
                        "eligible": research,
                        "can_open": bool(can_open),
                        "must_exit": code in st,
                        "soft_exit": code not in members,
                        "industry": group["industry"],
                        "known_at": max(
                            pd.Timestamp(r)
                            for r in (
                                known,
                                member["known_at"],
                                event["known_at"],
                                group["known_at"],
                            )
                        ),
                        "source_id": "compiled_csi800",
                        "revision_id": strategy["policy_sha256"],
                        "eligibility_reason": ",".join(reasons) or "eligible",
                        "universe_policy_sha256": strategy["policy_sha256"],
                    }
                    rows.append(row)
                    count["research"] += research
                    count["can_open"] += bool(can_open)
                    basic = basics.get(code)
                    mv = getattr(basic, "circ_mv", None)
                    closes = np.array([h.get(code, np.nan) for h in price_history])
                    vol = beta = np.nan
                    if len(closes) == 61 and np.isfinite(closes).all() and (closes > 0).all():
                        returns = closes[1:] / closes[:-1] - 1
                        vol = float(returns.std(ddof=1) * np.sqrt(252))
                        if (
                            np.isfinite(benchmark_history).all()
                            and np.var(benchmark_history, ddof=1) > 0
                        ):
                            beta = float(
                                np.cov(returns, benchmark_history, ddof=1)[0, 1]
                                / np.var(benchmark_history, ddof=1)
                            )
                    exposures.append(
                        {
                            "session": str(day),
                            "instrument_id": code,
                            "available_at": row["known_at"],
                            "log_float_market_cap": np.log(mv) if mv and mv > 0 else np.nan,
                            "log_median_amount": np.log(median) if median > 0 else np.nan,
                            "volatility_60d": vol,
                            "beta_csi800_60d": beta,
                        }
                    )
                counts.append(count)
                for name, records in (("context", rows), ("exposures", exposures)):
                    frame = pd.DataFrame(records)
                    timestamp = "known_at" if name == "context" else "available_at"
                    frame[timestamp] = pd.to_datetime(frame[timestamp], utc=True)
                    table = pa.Table.from_pandas(frame, preserve_index=False)
                    if name not in writers:
                        writers[name] = stack.enter_context(
                            pq.ParquetWriter(stage / f"{name}.parquet", table.schema)
                        )
                    writers[name].write_table(table)
            if any(sha256(Path(p)) != digest for p, digest in sources.items()):
                raise ValueError("universe sources changed during compilation")
            for number, path in enumerate(paths):
                if sha256(stage / f"source-{number}{Path(path).suffix}") != sources[str(path)]:
                    raise ValueError("universe source copy changed")
            stack.close()
            write_json(
                stage / "quality.json",
                {
                    "sessions": counts,
                    "membership_source": "effective_intervals",
                    "historical_publication_independently_verified": False,
                },
            )
            write_json(stage / "intent.json", intent)
            write_json(stage / "receipts.json", bindings)
            complete(stage)
            stage.rename(output)
        return output
