#!/usr/bin/env python3
"""Build the historical CSI800 evidence artifacts for a v2 project.

Read-only over Canonical and the archived provider observations; provider
requests are explicit (each subcommand states its scope), throttled, archived
with raw responses, and checkpointed. Every artifact is published with a
receipt recording inputs, observation times and known limitations. Nothing
here rewrites sealed data or backdates a publication time.

Usage (from the repository root):
  uv run python scripts/build_csi800_evidence.py --project config/project.local.json membership
  uv run python scripts/build_csi800_evidence.py --project ... availability
  uv run python scripts/build_csi800_evidence.py --project ... execution-policy
  uv run python scripts/build_csi800_evidence.py --project ... industries --fetch
  uv run python scripts/build_csi800_evidence.py --project ... event-coverage --fetch
  uv run python scripts/build_csi800_evidence.py --project ... corporate-actions --fetch
"""

from __future__ import annotations

import os

for _name in (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "ARROW_NUM_THREADS",
):
    os.environ.setdefault(_name, "2")

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import UTC, date, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quantlab.data.security_history import (  # noqa: E402
    code_at_observation_date,
    code_validity_interval,
    load_security_code_changes,
)
from quantlab.data.storage import ParquetStorage  # noqa: E402
from quantlab.pipeline.config import load_project  # noqa: E402
from quantlab.pipeline.evidence import (  # noqa: E402
    availability_frame,
    corporate_coverage,
    corporate_events,
    coverage_intervals,
    execution_policy_document,
    fee_eras,
    industry_intervals,
    membership_changes,
    membership_document,
    merge_industry_sources,
)
from quantlab.pipeline.ingestion import verify_session  # noqa: E402

EVIDENCE = "evidence"


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _output_root(project) -> Path:
    return Path(project.get("evidence_output", project["canonical"].parent / EVIDENCE))


def _write_receipt(project, name: str, payload: dict) -> None:
    out = _output_root(project) / "receipts"
    out.mkdir(parents=True, exist_ok=True)
    payload = {"receipt": name, "recorded_at": _now(), **payload}
    if "artifact" in payload:
        artifact_bytes = Path(payload["artifact"]).read_bytes()
        payload["artifact_sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    (out / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"receipt: {out / (name + '.json')}")


def _load_observations(project) -> list[tuple[date, frozenset[str]], ]:
    root = project["raw"] / "csi800_weights"
    folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    if not folders:
        raise SystemExit(
            "no index observations; run sync-index-observations --execute first"
        )
    observations = []
    changes = load_security_code_changes(ROOT / "config/security_code_changes.csv")
    for folder in folders:
        frame = pd.read_parquet(folder / "weights.parquet")
        receipt = json.loads((folder / "observation.json").read_text())
        for path in receipt["raw_responses"]:
            if not (folder / "raw" / Path(path).name).exists() and not Path(path).exists():
                raise SystemExit(f"archived raw response missing:{path}")
        stamp = pd.to_datetime(frame.trade_date, format="%Y%m%d").dt.date
        for day, group in frame.assign(_d=stamp).groupby("_d"):
            raw_codes = list(group.con_code)
            codes = frozenset(code_at_observation_date(c, day, changes) for c in raw_codes)
            if len(codes) != len(raw_codes):
                raise ValueError(f"code-history normalization collapsed observation:{day}")
            observations.append((day, codes))
    return observations


def _calendar(project) -> list[date]:
    storage = ParquetStorage(project["canonical"])
    return sorted(
        {
            row.trade_date
            for row in storage.load_trading_calendar()
            if getattr(row, "is_open", True)
        }
    )


def _member_codes(observations) -> tuple[list[str], list[date]]:
    codes: set[str] = set()
    days: list[date] = []
    for day, members in observations:
        codes |= members
        days.append(day)
    return sorted(codes), sorted(days)


def cmd_membership(project) -> None:
    observations = _load_observations(project)
    calendar = _calendar(project)
    intervals, changes = membership_changes(observations, calendar)
    observation_dates = [day for day, _ in observations]
    revision = hashlib.sha256(
        json.dumps([(str(d), sorted(m)) for d, m in sorted(observations)]).encode()
    ).hexdigest()
    document = membership_document(
        intervals,
        observation_dates=observation_dates,
        revision_id=revision,
    )
    out = _output_root(project) / "csi800_membership.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    scheduled = [c for c in changes if c.dating_basis == "scheduled_semiannual_effective"]
    bounded = [c for c in changes if c.dating_basis != "scheduled_semiannual_effective"]
    _write_receipt(
        project,
        "membership",
        {
            "artifact": str(out),
            "inputs": {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted((project["raw"] / "csi800_weights").glob("*/*"))
                if p.is_file()
            },
            "code_changes_sha256": hashlib.sha256(
                (ROOT / "config/security_code_changes.csv").read_bytes()
            ).hexdigest(),
            "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "security_history_sha256": hashlib.sha256(
                (ROOT / "src/quantlab/data/security_history.py").read_bytes()
            ).hexdigest(),
            "intervals": len(intervals),
            "scheduled_changes": len(scheduled),
            "observation_bounded_changes": [
                {
                    "observed": str(c.observed_date),
                    "effective": str(c.effective),
                    "basis": c.dating_basis,
                    "added": len(c.added),
                    "removed": len(c.removed),
                    "added_codes": c.added[:20],
                    "removed_codes": c.removed[:20],
                }
                for c in bounded
            ],
            "first_observation": str(observation_dates[0]),
            "limitations": document["known_limitations"],
            "code_identity_note": (
                "Successor codes observed before their dated effective date are mapped "
                "to the predecessor; original observations remain unchanged. "
                "This does not certify membership timing."
            ),
        },
    )
    print(
        json.dumps(
            {
                "intervals": len(intervals),
                "scheduled": len(scheduled),
                "observation_bounded": len(bounded),
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_availability(project) -> None:
    receipts = project["receipts"] / "sessions"
    storage = ParquetStorage(project["canonical"])
    sessions = []
    for path in sorted(receipts.glob("*.json")):
        day = date.fromisoformat(path.stem)
        verify_session(storage, project["receipts"], day)
        sessions.append((day, path.name))
    frame = availability_frame(sessions)
    out = _output_root(project) / "source_availability.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out, index=False)
    _write_receipt(
        project,
        "source_availability",
        {
            "artifact": str(out),
            "sessions": len(frame),
            "publication_policy": "declared_same_day_close_publication_v1 (17:00 Asia/Shanghai)",
            "limitations": [
                "known_at is a declared post-close publication bound verified only "
                "by exchange publication practice and sample checks; actual vendor "
                "delivery times are not historically certified.",
                "The actual local download time of every session stays in its "
                "ingestion receipt and bounds forward decisions only.",
            ],
        },
    )
    print(f"availability sessions: {len(frame)} -> {out}")


def cmd_execution_policy(project) -> None:
    observations = _load_observations(project)
    codes, _ = _member_codes(observations)
    start = _as_date(project["start"])
    end = _as_date(project["end"])
    document = execution_policy_document(
        codes, fee_eras(start, end), start=start, end=end
    )
    out = _output_root(project) / "execution_policy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    _write_receipt(
        project,
        "execution_policy",
        {
            "artifact": str(out),
            "instruments": len(codes),
            "policies": len(document["policies"]),
            "eras": [
                {"start": str(e.start), "end": str(e.end), "known_at": str(e.known_at)}
                for e in fee_eras(start, end)
            ],
            "limitations": document["known_limitations"],
            "code_changes_sha256": hashlib.sha256(
                (ROOT / "config/security_code_changes.csv").read_bytes()
            ).hexdigest(),
        },
    )
    print(f"execution policies: {len(document['policies'])} -> {out}")


def _sw_cache(project, codes: list[str], fetch: bool = False) -> tuple[pd.DataFrame, dict]:
    """Per-stock SW membership history with L1 name resolution.

    index_member(ts_code=...) is the only shape that returns the complete
    per-instrument stint history; per-industry filters return recent rows and
    unfiltered paging hits the provider offset cap. Rows whose index_code is
    not an L1 code of either taxonomy are dropped.
    """
    raw = project["canonical"].parent / EVIDENCE / "raw" / "sw"
    raw.mkdir(parents=True, exist_ok=True)
    archive = project["canonical"].parent / EVIDENCE / "raw" / "provider_archive"
    client = None
    if fetch:
        from quantlab.data.tushare_provider import TushareProvider

        client = TushareProvider(archive=archive, interval=0.35)._pro
    names = {}
    for taxonomy in ("SW2021", "SW2014"):
        if client is not None:
            classify = client.index_classify(level="L1", src=taxonomy)
        else:
            candidates = []
            for path in sorted((archive / "index_classify").glob("*.json")):
                item = json.loads(path.read_text())
                if item.get("parameters") != {"level": "L1", "src": taxonomy}:
                    continue
                response = item["response"]
                digest = hashlib.sha256(json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode()).hexdigest()
                if digest != item["response_sha256"]:
                    raise ValueError(f"classification archive hash mismatch:{path}")
                if item["row_count"] != len(response["data"]):
                    raise ValueError(f"classification archive row count mismatch:{path}")
                candidates.append((item["observed_at"], str(path), response))
            if not candidates:
                raise ValueError(
                    f"cached L1 classification missing:{taxonomy}; no request without --fetch"
                )
            response = max(candidates)[2]
            classify = pd.DataFrame(response["data"], columns=response["columns"])
        for _, row in classify.iterrows():
            code = str(row["index_code"]).strip()
            names.setdefault(code, {})[taxonomy] = str(row["industry_name"]).strip()
    # SW2021 launch renamed the shared code set; a stint's label follows the
    # taxonomy active at its in_date.
    boundary = pd.Timestamp("2021-12-13")
    frames = []
    for number, code in enumerate(codes):
        path = raw / f"{code}.parquet"
        if not path.exists():
            if client is None:
                raise ValueError(f"SW cache missing:{code}; no request without --fetch")
            member = client.index_member(ts_code=code)
            member.to_parquet(path, index=False)
            time.sleep(0.05)
        row = pd.read_parquet(path)
        row = row.copy()
        row["con_code"] = row.con_code.astype(str).str.strip()
        row["index_code"] = row.index_code.astype(str).str.strip()
        row = row[row.index_code.isin(names)]
        if row.empty:
            continue
        labels = []
        for _, r in row.iterrows():
            per_taxonomy = names[r["index_code"]]
            taxonomy = "SW2014" if pd.to_datetime(str(r["in_date"])) < boundary else "SW2021"
            labels.append(per_taxonomy.get(taxonomy, list(per_taxonomy.values())[0]))
        row["l1_name"] = labels
        frames.append(row)
        if number % 100 == 0:
            print(f"sw cache progress: {number}/{len(codes)}")
    table = pd.concat(frames, ignore_index=True)
    table = table.drop_duplicates(subset=["con_code", "in_date", "out_date", "index_code"])
    return table, names


def _snapshot_calendar(project) -> dict[date, bool]:
    """Only dates explicitly agreeing across both exchanges certify a closure."""
    rows = ParquetStorage(project["canonical"]).load_trading_calendar()
    by_day: dict[date, dict[str, set[bool]]] = {}
    for row in rows:
        by_day.setdefault(row.trade_date, {}).setdefault(row.exchange, set()).add(row.is_open)
    return {
        day: next(iter(exchanges["SSE"]))
        for day, exchanges in by_day.items()
        if exchanges.get("SSE") == exchanges.get("SZSE")
        and len(exchanges.get("SSE", set())) == 1
    }


def _bak_basic_fills(
    project, codes: list[str], fetch: bool, calendar: dict[date, bool]
) -> list[dict]:
    """Daily vendor industry snapshots fill gaps the SW stint table leaves.

    bak_basic keeps per-date snapshots, so a gap-day industry label is an
    actually observed value, not a backfilled classification. Stints cover
    the whole observed range (including days after a name left the index but
    while it may still be held); the merge clips them against SW coverage.
    """
    raw = project["canonical"].parent / EVIDENCE / "raw" / "bak_basic"
    raw.mkdir(parents=True, exist_ok=True)
    provider = None
    client = None
    fills: list[dict] = []
    for number, code in enumerate(codes):
        path = raw / f"{code}.parquet"
        if not path.exists():
            if not fetch:
                continue
            if provider is None:
                from quantlab.data.tushare_provider import TushareProvider

                provider = TushareProvider(
                    archive=project["canonical"].parent
                    / EVIDENCE
                    / "raw"
                    / "provider_archive",
                    interval=0.35,
                )
                client = provider._pro
            try:
                frame = client.bak_basic(
                    ts_code=code,
                    fields="ts_code,trade_date,name,industry,list_date",
                )
            except Exception as exc:
                print(f"bak_basic fetch failed:{code}:{type(exc).__name__}")
                continue
            frame.to_parquet(path, index=False)
            time.sleep(0.05)
        frame = pd.read_parquet(path)
        frame["trade_date"] = pd.to_datetime(frame.trade_date, format="%Y%m%d", errors="coerce")
        frame["industry"] = frame.industry.fillna("").astype(str).str.strip()
        frame = frame.dropna(subset=["trade_date"]).sort_values("trade_date")
        frame = frame[frame.industry.ne("") & frame.industry.ne("nan")]
        if frame.empty:
            continue
        streak_state = {"start": None, "label": None, "last": None}

        def _close(code=code, state=streak_state):
            if state["label"] is not None:
                fills.append(
                    {
                        "instrument_id": code,
                        "start": state["start"].isoformat(),
                        "end": state["last"].isoformat(),
                        "known_at": f'{state["start"].isoformat()}T00:00:00+08:00',
                        "source_id": "tushare_bak_basic_daily_snapshot",
                        "revision_id": "bak_basic_v1",
                        "industry": f'bak_basic:{state["label"]}',
                    }
                )

        for _, row in frame.iterrows():
            day = row["trade_date"].date()
            if streak_state["last"] is not None:
                # Every intervening calendar date needs explicit closed-day
                # evidence. An absent date is UNKNOWN, never a holiday.
                cursor = streak_state["last"] + timedelta(days=1)
                gap = False
                while cursor < day:
                    if calendar.get(cursor) is not False:
                        gap = True
                        break
                    cursor += timedelta(days=1)
                if gap:
                    _close(code=code, state=streak_state)
                    streak_state = {"start": None, "label": None, "last": None}
            if row["industry"] != streak_state["label"]:
                _close(code=code, state=streak_state)
                streak_state["start"], streak_state["label"] = day, row["industry"]
            streak_state["last"] = day
        _close(code=code, state=streak_state)
        if number % 25 == 0:
            print(f"bak_basic progress: {number}/{len(codes)}")
    return fills


def _evidence_input_hashes(project, codes, *, corporate: bool) -> dict[str, str]:
    """Bind the local source snapshots used in this compilation, not PIT certify them."""
    raw = project["canonical"].parent / EVIDENCE / "raw"
    folders = ["dividends"] if corporate else ["sw", "bak_basic"]
    paths = [raw / folder / f"{code}.parquet" for folder in folders for code in codes]
    paths.extend((project["raw"] / "csi800_weights").glob("*/*.parquet"))
    paths.extend((project["raw"] / "csi800_weights").glob("*/observation.json"))
    if not corporate:
        paths.extend((raw / "provider_archive/index_classify").glob("*.json"))
        paths.append(ParquetStorage(project["canonical"]).calendar_path)
    paths.extend([
        Path(__file__), ROOT / "src/quantlab/pipeline/evidence.py",
        ROOT / "src/quantlab/data/security_history.py",
    ])
    paths.append(ROOT / "config/security_code_changes.csv")
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(set(paths)) if p.exists()}


def cmd_industries(project, fetch: bool) -> None:
    observations = _load_observations(project)
    codes, _ = _member_codes(observations)
    end = _as_date(project["end"])
    table, _names = _sw_cache(project, codes, fetch)
    intervals, issues = industry_intervals(
        table, set(codes), end=end, taxonomy="SW"
    )
    # Fill from the daily snapshot source for EVERY code with cached or
    # fetchable snapshots. A name that left the index but remains held still
    # needs a sourced industry; the merge keeps SW stints primary and uses
    # snapshots only where SW has no coverage, so whole-range fills cannot
    # override better evidence.
    fills = _bak_basic_fills(project, codes, fetch, _snapshot_calendar(project))
    intervals, merge_issues = merge_industry_sources(intervals, fills, end=end)
    issues = issues + merge_issues
    changes = load_security_code_changes(ROOT / "config/security_code_changes.csv")
    dated_intervals = []
    for row in intervals:
        valid_start, valid_end = code_validity_interval(row["instrument_id"], changes)
        start_day = max(date.fromisoformat(row["start"]), valid_start)
        end_day = min(date.fromisoformat(row["end"]), valid_end)
        if start_day <= end_day:
            dated_intervals.append({**row, "start": str(start_day), "end": str(end_day)})
    intervals = dated_intervals
    document = {"intervals": intervals}
    out = _output_root(project) / "industry_intervals.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    _write_receipt(
        project,
        "industry_intervals",
        {
            "artifact": str(out),
            "codes": len(codes),
            "intervals": len(intervals),
            "inputs": _evidence_input_hashes(project, codes, corporate=False),
            "end": str(end),
            "historical_publication_certified": False,
            "issues": issues,
            "limitations": [
                "Industry labels come from the vendor's per-instrument SW "
                "membership history (in/out dates); a stint is labeled with the "
                "taxonomy active at its in_date (SW2014 names before the "
                "2021-12-13 relabeling, SW2021 names after).",
                "Conflicting primary labels remain industry=null and cannot be "
                "replaced by fallback. Snapshot gaps require explicit closed-day "
                "calendar evidence to bridge; absent calendar dates remain unknown.",
                "Days the SW stint table does not cover (mostly STAR names "
                "between listing and the vendor's earliest retained stint, and "
                "any period after a name left the index) are filled from the "
                "vendor's per-date snapshot archive and labeled 'bak_basic:...' "
                "so the two sources stay distinguishable.",
                "known_at equals the membership effective date; contemporaneous "
                "publication timestamps are not independently certified.",
                "Codes with no covering interval on a member day surface as an "
                "explicit universe compilation block, never a defaulted label.",
                "Industry constraint consistency caveat: labels mix SW2014, "
                "SW2021 and bak_basic taxonomies, so a max-industry-weight cap "
                "groups by literal label string and is NOT a fully consistent "
                "single-taxonomy classification across the whole window.",
            ],
        },
    )
    print(
        json.dumps(
            {
                "codes": len(codes),
                "intervals": len(intervals),
                "issues": len(issues),
                "output": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def cmd_event_coverage(project, fetch: bool) -> None:
    storage = ParquetStorage(project["canonical"])
    receipts = project["receipts"] / "sessions"
    days = []
    st_flips: dict[str, list[tuple[date, bool]]] = {}
    previous_st: dict[str, bool] = {}
    for path in sorted(receipts.glob("*.json")):
        day = date.fromisoformat(path.stem)
        verify_session(storage, project["receipts"], day)
        days.append(day)
        current = {row.instrument_id for row in storage.load_stock_st_v1_by_date(day)}
        for code in current | set(previous_st):
            was = previous_st.get(code, False)
            now = code in current
            if was != now:
                st_flips.setdefault(code, []).append((day, now))
        previous_st = {code: True for code in current}
    # File integrity and a convenience sample do not prove absence of missing ST events.
    complete = False
    samples: list[dict] = []
    if fetch and st_flips:
        from quantlab.data.tushare_provider import TushareProvider

        provider = TushareProvider(
            archive=project["canonical"].parent / EVIDENCE / "raw" / "provider_archive",
            interval=0.35,
        )
        client = provider._pro
        picked = sorted(st_flips, key=lambda c: -len(st_flips[c]))[:20]
        matched = 0
        for code in picked:
            try:
                changes = client.namechange(
                    ts_code=code,
                    start_date=(days[0]).strftime("%Y%m%d"),
                    end_date=days[-1].strftime("%Y%m%d"),
                )
            except Exception as exc:
                samples.append({"code": code, "error": type(exc).__name__})
                continue
            ann = sorted(
                pd.to_datetime(changes.ann_date, format="%Y%m%d", errors="coerce").dropna()
            )
            for flip_day, entering in st_flips[code]:
                window = [
                    a
                    for a in ann
                    if abs((a.date() - flip_day).days) <= 10
                ]
                ok = bool(window)
                matched += ok
                samples.append(
                    {
                        "code": code,
                        "flip": str(flip_day),
                        "entering": entering,
                        "namechange_match": ok,
                    }
                )
            time.sleep(0.05)
        rate = matched / max(1, sum(1 for s in samples if "flip" in s))
        print(f"ST sample agreement (diagnostic only): {rate:.6f}")
    revision = f"sealed_sessions_{len(days)}"
    coverage = coverage_intervals(
        days,
        source_id="tushare_stock_st_daily_sealed",
        revision_id=revision,
        complete=complete,
    )
    document = {"schema": "quantlab_event_coverage_v1", "stock_st": coverage}
    out = _output_root(project) / "event_coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    _write_receipt(
        project,
        "event_coverage",
        {
            "artifact": str(out),
            "sessions": len(days),
            "interval_count": len(coverage),
            "complete": complete,
            "sample_size": sum(1 for s in samples if "flip" in s),
            "samples": samples,
            "limitations": [
                "Neither nonempty sealed partitions nor name-change samples establish "
                "exhaustive daily ST coverage. complete remains false pending a dated "
                "source reconciliation including empty, missing and truncated responses.",
                "Vendor ST coverage depth before the first sealed session is "
                "unknown and intentionally not certified.",
            ],
        },
    )
    print(
        json.dumps(
            {"sessions": len(days), "complete": complete, "samples": len(samples)},
            ensure_ascii=False,
        )
    )


def _dividend_cache(project, codes: list[str], fetch: bool) -> tuple[dict[str, pd.DataFrame], dict]:
    raw = project["canonical"].parent / EVIDENCE / "raw" / "dividends"
    raw.mkdir(parents=True, exist_ok=True)
    frames: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    from quantlab.data.tushare_provider import TushareProvider

    provider = (
        TushareProvider(
            archive=project["canonical"].parent / EVIDENCE / "raw" / "provider_archive",
            interval=0.35,
        )
        if fetch
        else None
    )
    for number, code in enumerate(codes):
        path = raw / f"{code}.parquet"
        if path.exists():
            frames[code] = pd.read_parquet(path)
            continue
        if provider is None:
            errors[code] = "cache_missing"
            continue
        try:
            frame = provider._pro.dividend(ts_code=code)
        except Exception as exc:
            errors[code] = type(exc).__name__
            time.sleep(1.0)
            continue
        frame.to_parquet(path, index=False)
        frames[code] = frame
        if number % 100 == 0:
            print(f"dividend fetch progress: {number}/{len(codes)}")
    return frames, errors


def cmd_corporate_actions(project, fetch: bool) -> None:
    observations = _load_observations(project)
    codes, _ = _member_codes(observations)
    start = _as_date(project["start"])
    end = _as_date(project["end"])
    cache_start = date(start.year - 1, start.month, 1)
    frames, errors = _dividend_cache(project, codes, fetch)
    events: list[dict] = []
    skipped: list[str] = []
    unresolved: list[dict] = []
    changes = load_security_code_changes(ROOT / "config/security_code_changes.csv")
    for code in codes:
        frame = frames.get(code)
        if frame is None:
            continue
        implemented = frame[frame.get("div_proc", pd.Series(dtype=str)).astype(str).eq("实施")]
        if implemented.empty:
            continue
        code_events, code_skipped, code_unresolved = corporate_events(
            implemented, code, start=cache_start, end=end, source_id="tushare_dividend_observation"
        )
        valid_start, valid_end = code_validity_interval(code, changes)
        events.extend(
            event for event in code_events
            if valid_start <= date.fromisoformat(event["ex_date"]) <= valid_end
        )
        skipped.extend(code_skipped)
        for record in code_unresolved:
            if record.get("ex_date") and not (
                valid_start <= date.fromisoformat(record["ex_date"]) <= valid_end
            ):
                continue
            unresolved.append({"instrument_id": code, **record})
    unresolved_instruments = [
        {"instrument_id": code, "reason": reason} for code, reason in sorted(errors.items())
    ]
    document = {
        "coverage": corporate_coverage(
            codes,
            start=cache_start,
            end=end,
            source_id="tushare_dividend_observation",
            unresolved=unresolved,
            unresolved_instruments=unresolved_instruments,
        ),
        "events": events,
    }
    out = _output_root(project) / "corporate_actions.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    issues_path = out.parent / "corporate_actions_issues.json"
    issues_path.write_text(
        json.dumps(
            {
                "schema": "quantlab_corporate_unresolved_v1",
                "unresolved": unresolved,
                "skipped": skipped,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    _write_receipt(
        project,
        "corporate_actions",
        {
            "artifact": str(out),
            "codes": len(codes),
            "cached_frames": len(frames),
            "inputs": _evidence_input_hashes(project, codes, corporate=True),
            "start": str(cache_start), "end": str(end),
            "historical_publication_certified": False,
            "fetch_errors": errors,
            "events": len(events),
            "cash_dividend_events": sum(1 for e in events if e["kind"] == "cash_dividend"),
            "share_events": sum(1 for e in events if e["kind"] == "bonus_shares"),
            "unresolved_events": len(unresolved),
            "issues_file": str(
                _output_root(project) / "corporate_actions_issues.json"
            ),
            "limitations": [
                "cash_dividend uses the vendor per-share post-withholding rate; "
                "the holding-period differential dividend tax charged at disposal "
                "(0/10/20% by holding period) is a separate, currently unsupported "
                "cost and understates realized costs for short holding periods.",
                "rights issues, merger consideration and delisting settlements are "
                "not expressible events and must surface as explicit account blocks.",
            ],
        },
    )
    print(
        json.dumps(
            {
                "codes": len(codes),
                "events": len(events),
                "fetch_errors": len(errors),
                "output": str(out),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "membership", "availability", "execution-policy", "industries",
        "event-coverage", "corporate-actions",
    ):
        child = sub.add_parser(name)
        if name in {"industries", "event-coverage", "corporate-actions"}:
            child.add_argument("--fetch", action="store_true")
        child.add_argument(
            "--output-dir", type=Path,
            help="New evidence namespace; reuse existing input caches",
        )
    args = parser.parse_args()
    project = load_project(args.project)
    if getattr(args, "output_dir", None) is not None:
        project["evidence_output"] = args.output_dir.resolve()
    # Evidence is immutable. A re-run must target a new isolated namespace.
    outputs = {
        "membership": "csi800_membership.json",
        "availability": "source_availability.parquet",
        "execution-policy": "execution_policy.json",
        "industries": "industry_intervals.json",
        "event-coverage": "event_coverage.json",
        "corporate-actions": "corporate_actions.json",
    }
    target = _output_root(project) / outputs[args.command]
    targets = [target, target.parent / "receipts" / (("membership" if args.command == "membership"
                                              else target.stem) + ".json")]
    if args.command == "corporate-actions":
        targets.append(target.parent / "corporate_actions_issues.json")
    for path in targets:
        if path.exists():
            raise SystemExit(f"evidence already exists; use a new namespace:{path}")
    if args.command == "membership":
        cmd_membership(project)
    elif args.command == "availability":
        cmd_availability(project)
    elif args.command == "execution-policy":
        cmd_execution_policy(project)
    elif args.command == "industries":
        cmd_industries(project, args.fetch)
    elif args.command == "event-coverage":
        cmd_event_coverage(project, args.fetch)
    else:
        cmd_corporate_actions(project, args.fetch)


if __name__ == "__main__":
    main()
