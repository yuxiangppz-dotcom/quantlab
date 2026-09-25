#!/usr/bin/env python3
"""Scope issuer-verified, completed precoverage share actions out of replay.

The original issue and vendor row remain in the receipt. This makes no claim
about who was entitled at the time; the research account starts after the
shares were delivered and was flat before inception.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scope(
    document: dict, facts: dict, raw_root: Path, source_root: Path,
    project_start: date | None = None,
) -> tuple[dict, list]:
    coverage = document["coverage"]
    cutoff = date.fromisoformat(coverage["start"])
    if "unresolved" not in coverage or "out_of_scope_historical_issues" in coverage:
        raise ValueError("corporate coverage lacks unresolved or is already scoped")
    unresolved = list(coverage["unresolved"])
    scoped = []
    seen = set()
    for fact in facts["facts"]:
        code = fact["instrument_id"]
        if code in seen or fact["status"] != "issuer_verified_completed_precoverage":
            raise ValueError(f"duplicate or unreviewed historical fact:{code}")
        seen.add(code)
        if digest(source_root / fact["source_file"]) != fact["source_sha256"]:
            raise ValueError(f"issuer source changed:{code}")
        raw_path = raw_root / f"{code}.parquet"
        if digest(raw_path) != fact["vendor_rows_sha256"]:
            raise ValueError(f"vendor observations changed:{code}")
        rows = pd.read_parquet(raw_path)
        implemented = rows["div_proc"].eq("实施")
        if fact["record_date"] is None:
            if code != "600556.SH" or fact["end_date"] != "20081218":
                raise ValueError(f"unexpected missing historical record date:{code}")
            rows = rows[implemented & rows["record_date"].isna()
                    & rows["end_date"].eq(fact["end_date"])]
        else:
            rows = rows[implemented & rows["record_date"].eq(fact["record_date"])]
        if len(rows) != 1 or pd.notna(rows.iloc[0]["ex_date"]):
            raise ValueError(f"historical vendor group changed:{code}")
        if rows.iloc[0]["div_listdate"] != fact["completed_by"].replace("-", ""):
            raise ValueError(f"historical delivery date changed:{code}")
        dates = [fact["completed_by"]]
        if fact["record_date_iso"] is not None:
            dates.append(fact["record_date_iso"])
        if any(date.fromisoformat(value) >= cutoff for value in dates):
            raise ValueError(f"historical event overlaps coverage:{code}")
        if (fact["record_date_iso"] is None) != (fact["record_date"] is None):
            raise ValueError(f"historical record-date mismatch:{code}")
        if (
            fact["record_date_iso"] is not None
            and fact["record_date_iso"].replace("-", "") != fact["record_date"]
        ):
            raise ValueError(f"historical record-date mismatch:{code}")
        matching = [
            issue for issue in unresolved
            if issue["instrument_id"] == code
            and issue["reason"] == "missing_ex_date"
            and issue["record_date"] == fact["record_date"]
            and issue["ex_date"] is None
        ]
        if len(matching) != 1:
            raise ValueError(f"expected one precoverage issue:{code}")
        if any(event["instrument_id"] == code and event["record_date"] == fact["record_date_iso"]
               for event in document["events"]):
            raise ValueError(f"historical action already executable:{code}")
        unresolved.remove(matching[0])
        scoped.append({
            "issue": matching[0],
            "classification": "completed_before_corporate_coverage_start",
            "completed_by": fact["completed_by"],
            "issuer_source_sha256": fact["source_sha256"],
            "vendor_rows_sha256": fact["vendor_rows_sha256"],
        })
    if seen != {"000403.SZ", "000703.SZ", "600176.SH", "600537.SH", "600556.SH"}:
        raise ValueError("expected exactly five issuer-verified precoverage actions")
    flat_facts = facts.get("flat_inception_facts", [])
    if flat_facts and (project_start is None or project_start <= cutoff):
        raise ValueError("flat-inception scope requires a later project start")
    for fact in flat_facts:
        code = fact["instrument_id"]
        record = fact["record_date"]
        key = (code, record)
        if fact["status"] != "issuer_verified_account_flat_on_record_date":
            raise ValueError(f"unreviewed flat-inception fact:{key}")
        if date.fromisoformat(record) >= project_start:
            raise ValueError(f"record date is not before flat inception:{key}")
        source = source_root / fact["source_file"]
        raw_path = raw_root / f"{code}.parquet"
        if (
            digest(source) != fact["source_sha256"]
            or digest(raw_path) != fact["vendor_rows_sha256"]
        ):
            raise ValueError(f"historical source changed:{key}")
        rows = pd.read_parquet(raw_path)
        rows = rows[rows["div_proc"].eq("实施") & rows["record_date"].eq(record.replace("-", ""))]
        if len(rows) != fact["vendor_row_count"]:
            raise ValueError(f"vendor record group changed:{key}")
        matches = [
            issue for issue in unresolved
            if issue["instrument_id"] == code
            and issue["record_date"] in {record, record.replace("-", "")}
            and issue["reason"] == fact["reason"]
        ]
        if len(matches) != fact["issue_count"]:
            raise ValueError(f"historical issue group changed:{key}")
        if any(event["instrument_id"] == code and event["record_date"] == record
               for event in document["events"]):
            raise ValueError(f"historical group already executable:{key}")
        for issue in matches:
            unresolved.remove(issue)
            scoped.append({
                "issue": issue,
                "classification": "no_account_holding_on_preinception_record_date",
                "project_start": project_start.isoformat(),
                "issuer_source_sha256": fact["source_sha256"],
                "vendor_rows_sha256": fact["vendor_rows_sha256"],
            })
    result_coverage = {
        **coverage,
        "unresolved": unresolved,
        "out_of_scope_historical_issues": scoped,
    }
    if flat_facts:
        result_coverage["minimum_replay_date"] = project_start.isoformat()
    result = {
        **document,
        "coverage": result_coverage,
    }
    return result, scoped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corporate", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project-start", type=date.fromisoformat)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    result, scoped = scope(
        json.loads(args.corporate.read_text()), json.loads(args.facts.read_text()),
        args.raw_root, args.source_root, args.project_start,
    )
    args.output_dir.mkdir(parents=True)
    output = args.output_dir / "corporate_actions.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    receipt = {
        "schema": "quantlab_corporate_precoverage_scope_v1",
        "inputs": {
            str(path): digest(path)
            for path in (args.corporate, args.facts, Path(__file__))
        },
        "scoped": scoped,
        "output_sha256": digest(output),
        "remaining_unresolved": len(result["coverage"]["unresolved"]),
    }
    (args.output_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(f"scoped={len(scoped)} unresolved={receipt['remaining_unresolved']} output={output}")


if __name__ == "__main__":
    main()
