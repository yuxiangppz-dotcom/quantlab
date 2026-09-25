#!/usr/bin/env python3
"""Resolve issuer-proven restructurings that grant no shares to ordinary holders.

This does not discard the source row: it binds a reviewed issuer notice and
records the non-entitlement in the output receipt. Other unknown events stay
unresolved. The backtest continues to mark existing shares from real prices.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile(
    document: dict, facts: dict, raw_root: Path, source_root: Path
) -> tuple[dict, list[dict]]:
    if "unresolved" not in document.get("coverage", {}):
        raise ValueError("corporate coverage has no unresolved list")
    unresolved = list(document["coverage"]["unresolved"])
    events = document["events"]
    applied = []
    for fact in facts["facts"]:
        if fact["status"] != "issuer_verified_no_ordinary_holder_entitlement":
            continue
        code = fact["instrument_id"]
        ex_date = fact["ex_date"]
        record_date = fact["record_date"]
        if fact["ordinary_holder_share_ratio"] != "0":
            raise ValueError(f"nonzero holder entitlement:{code}:{ex_date}")
        matching_issues = [
            issue
            for issue in unresolved
            if issue["instrument_id"] == code
            and issue["ex_date"] == ex_date
            and issue["record_date"] in {record_date, record_date.replace("-", "")}
            and issue["reason"] == "missing_div_listdate"
        ]
        if len(matching_issues) != 1:
            raise ValueError(f"expected one missing listing issue:{code}:{ex_date}")
        if any(
            event["instrument_id"] == code and event["ex_date"] == ex_date
            for event in events
        ):
            raise ValueError(f"event already exists:{code}:{ex_date}")
        source = source_root / fact["source_file"]
        raw_path = raw_root / f"{code}.parquet"
        if digest(source) != fact["source_sha256"]:
            raise ValueError(f"issuer source changed:{code}:{ex_date}")
        if digest(raw_path) != fact["vendor_rows_sha256"]:
            raise ValueError(f"vendor rows changed:{code}:{ex_date}")
        raw = pd.read_parquet(raw_path)
        matching_rows = raw[
            raw["div_proc"].eq("实施")
            & raw["record_date"].eq(record_date.replace("-", ""))
            & raw["ex_date"].eq(ex_date.replace("-", ""))
        ]
        if len(matching_rows) != 1:
            raise ValueError(f"expected one vendor row:{code}:{ex_date}")
        row = matching_rows.iloc[0]
        if (
            str(row["stk_div"]) != fact["vendor_stk_div"]
            or pd.notna(row["div_listdate"])
            or pd.notna(row["cash_div"])
            or pd.notna(row["cash_div_tax"])
        ):
            raise ValueError(f"vendor terms changed:{code}:{ex_date}")
        unresolved.remove(matching_issues[0])
        applied.append(
            {
                "instrument_id": code,
                "ex_date": ex_date,
                "classification": "no_ordinary_holder_entitlement",
                "issuer_source_sha256": digest(source),
                "vendor_rows_sha256": digest(raw_path),
            }
        )
    if len(applied) != 1 or applied[0]["instrument_id"] != "002309.SZ":
        raise ValueError("expected exactly the reviewed 002309 non-entitlement")
    result = {**document, "coverage": {**document["coverage"], "unresolved": unresolved}}
    result["coverage"]["nonholder_conversion_reconciliation"] = applied
    return result, applied


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
    result, applied = reconcile(
        json.loads(args.corporate.read_text()),
        json.loads(args.facts.read_text()),
        args.raw_root,
        args.source_root,
    )
    args.output_dir.mkdir(parents=True)
    output = args.output_dir / "corporate_actions.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    receipt = {
        "schema": "quantlab_nonholder_conversion_reconciliation_v1",
        "inputs": {
            str(path): digest(path)
            for path in (args.corporate, args.facts, Path(__file__))
        },
        "applied": applied,
        "output_sha256": digest(output),
        "remaining_unresolved": len(result["coverage"]["unresolved"]),
    }
    (args.output_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(f"reconciled={len(applied)} unresolved={receipt['remaining_unresolved']} output={output}")


if __name__ == "__main__":
    main()
