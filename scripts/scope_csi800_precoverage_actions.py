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


def scope(document: dict, facts: dict, raw_root: Path, source_root: Path) -> tuple[dict, list]:
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
        rows = rows[rows["div_proc"].eq("实施") & rows["record_date"].eq(fact["record_date"])]
        if len(rows) != 1 or pd.notna(rows.iloc[0]["ex_date"]):
            raise ValueError(f"historical vendor group changed:{code}")
        if rows.iloc[0]["div_listdate"] != fact["completed_by"].replace("-", ""):
            raise ValueError(f"historical delivery date changed:{code}")
        if any(date.fromisoformat(fact[k]) >= cutoff for k in ("completed_by", "record_date_iso")):
            raise ValueError(f"historical event overlaps coverage:{code}")
        if fact["record_date_iso"].replace("-", "") != fact["record_date"]:
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
    if seen != {"000403.SZ", "000703.SZ", "600176.SH", "600537.SH"}:
        raise ValueError("expected exactly four issuer-verified precoverage actions")
    result = {
        **document,
        "coverage": {
            **coverage,
            "unresolved": unresolved,
            "out_of_scope_historical_issues": scoped,
        },
    }
    return result, scoped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corporate", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    result, scoped = scope(
        json.loads(args.corporate.read_text()), json.loads(args.facts.read_text()),
        args.raw_root, args.source_root,
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
