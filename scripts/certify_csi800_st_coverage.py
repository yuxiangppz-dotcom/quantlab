"""Reconcile historical CSI800 ST flags without changing sealed daily partitions.

The output is scoped to the historical CSI800 research/holding pool.  It binds
the complete daily transport audit, issuer-event review, every archived name
history response, and independent BaoStock update-day comparisons.  Only four
verified one-day omissions are added; no existing ST row is ever removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from quantlab.data.storage import ParquetStorage


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path, bindings: dict[str, str]) -> dict:
    bindings[str(path.resolve())] = sha(path)
    return json.loads(path.read_text())


def build(
    membership_path: Path,
    transport_path: Path,
    issuer_path: Path,
    name_audit_path: Path,
    name_root: Path,
    baostock_path: Path,
    proof_root: Path,
    canonical: Path,
    sessions_root: Path,
    output: Path,
) -> dict:
    bindings: dict[str, str] = {}
    membership = read(membership_path, bindings)
    transport = read(transport_path, bindings)
    issuer = read(issuer_path, bindings)
    names = read(name_audit_path, bindings)
    independent = read(baostock_path, bindings)
    if membership["schema"] != "quantlab_index_membership_v1":
        raise ValueError("dated research membership required")
    if transport["issues"] or transport["raw_session_count"] != 2354:
        raise ValueError("daily ST transport audit not complete")
    if issuer["verified_event_count"] != 338 or issuer["unresolved_count"] != 11:
        raise ValueError("issuer ST transition review changed")
    if names["input_namechange_files"] != 1474 or names["daily_sessions"] != 2354:
        raise ValueError("name-history census changed")
    for basename, expected in names["input_file_hashes"].items():
        path = name_root / basename
        if sha(path) != expected:
            raise ValueError(f"name history response changed:{path}")
        bindings[str(path.resolve())] = expected
    storage = ParquetStorage(canonical)
    securities = {item.instrument_id: item for item in storage.load_securities()}
    mismatches = []
    for item in names["interior_mismatches"] + names["boundary_mismatches"]:
        code = item["instrument_id"]
        security = securities.get(code)
        day = date.fromisoformat(item["day"])
        if security is None:
            continue
        if security.list_date <= day and (
            security.delist_date is None or day < security.delist_date
        ):
            mismatches.append((str(day), code, item["name_risk_status"], item["daily_st_status"]))
    expected = {
        ("2017-05-05", "000629.SZ", True, False),
        ("2017-06-23", "600733.SH", True, False),
        ("2019-05-13", "000939.SZ", True, False),
        ("2020-01-03", "600074.SH", True, False),
    }
    if set(mismatches) != expected or len(mismatches) != len(expected):
        raise ValueError(f"unreconciled active-listing ST mismatches:{mismatches}")
    proof = {
        "1204273610.pdf": "f4a7d569d9cd3306207abb6fc1c0889267916aefc55e1451d086fd485ffde866",
        "1205107880.pdf": "137adcc8a8bc877f7c6f663f9d992015b1ff66eeab04a99d2f89c13061fd7aa2",
        "1206257692.pdf": "7b97108e9b91a6be930dce9fd3575e4c1da3aa725c6d5ec1e5c698e1732eae59",
    }
    for filename, expected_hash in proof.items():
        path = proof_root / filename
        if sha(path) != expected_hash:
            raise ValueError(f"issuer ST proof changed:{path}")
        bindings[str(path.resolve())] = expected_hash

    snapshots = membership["snapshots"]
    seen = set()
    independent_count = 0
    for record in independent["records"]:
        dates = record.get("baostock_update_dates", [])
        if record["date"] < "2018-01-01" or len(dates) != 1:
            continue
        if "at_update_only_tushare" not in record:
            continue
        day = dates[0]
        seen = set().union(*(set(row["members"]) for row in snapshots if row["start"] <= day))
        missing = set(record["at_update_only_baostock"]) & seen
        excess = set(record["at_update_only_tushare"]) & seen
        if missing == {"000939.SZ"} and day == "2019-05-13":
            missing.clear()  # sourced additive correction below
        if missing or excess:
            raise ValueError(f"independent ST mismatch in research pool:{day}:{missing}:{excess}")
        for path, expected_hash in zip(
            record["baostock_files"], record["baostock_hashes"], strict=True
        ):
            if sha(Path(path)) != expected_hash:
                raise ValueError(f"independent ST source changed:{path}")
            bindings[str(Path(path).resolve())] = expected_hash
        if record.get("tushare_file"):
            if sha(Path(record["tushare_file"])) != record["tushare_sha256"]:
                raise ValueError(f"daily ST comparison source changed:{day}")
        independent_count += 1
    if independent_count != 106:
        raise ValueError(f"independent update-day comparison count changed:{independent_count}")

    days = sorted(
        date.fromisoformat(path.stem)
        for path in sessions_root.glob("*.json")
        if "2018-01-01" <= path.stem <= "2026-09-10"
    )
    if len(days) != 2110:
        raise ValueError(f"sealed study-session count changed:{len(days)}")
    source = "tushare_daily+issuer_transitions+name_history+baostock_update_checks"
    rows = []
    for day in days:
        stamp = datetime.combine(day, time(18), tzinfo=ZoneInfo("Asia/Shanghai"))
        rows.append(
            {
                "start": str(day),
                "end": str(day),
                "known_at": stamp.isoformat(),
                "source_id": source,
                "revision_id": "st-reconciliation-v1",
                "complete": True,
            }
        )
    additions = [
        {
            "trade_date": "2019-05-13",
            "instrument_id": "000939.SZ",
            "source_ids": ["namechange:000939.SZ", "cninfo:1205107880", "cninfo:1206257692"],
            "reason": "suspension day retained *ST name; daily status omitted it",
        },
        {
            "trade_date": "2020-01-03",
            "instrument_id": "600074.SH",
            "source_ids": ["namechange:600074.SH", "cninfo:1204273610"],
            "reason": "single-day response omission without ST removal or normal-name interval",
        },
    ]
    if output.exists():
        raise FileExistsError(output)
    payload = {
        "schema": "quantlab_event_coverage_v1",
        "scope": "historical_csi800_seen_pool",
        "stock_st": rows,
        "stock_st_additions": additions,
        "verification": {
            "daily_sessions": len(days),
            "issuer_events_checked": 349,
            "issuer_events_directly_verified": 338,
            "name_history_instruments": 1474,
            "independent_update_days_matching_after_correction": independent_count,
            "active_listing_false_negative_days": len(expected),
            "research_interval_corrections": len(additions),
        },
        "input_sha256": {**bindings, str(Path(__file__).resolve()): sha(Path(__file__))},
        "limitation": (
            "Research-pool coverage is corroborated by sealed daily responses, issuer "
            "events, complete retrieved name-history responses, and 106 independent "
            "update-day comparisons. This does not certify all-market historical ST status "
            "or exact intraday publication timestamps."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "membership",
        "transport",
        "issuer",
        "name-audit",
        "name-root",
        "baostock",
        "proof-root",
        "canonical",
        "sessions-root",
        "output",
    ):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    result = build(
        args.membership,
        args.transport,
        args.issuer,
        args.name_audit,
        args.name_root,
        args.baostock,
        args.proof_root,
        args.canonical,
        args.sessions_root,
        args.output,
    )
    print(json.dumps(result["verification"]))


if __name__ == "__main__":
    main()
