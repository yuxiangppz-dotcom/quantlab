#!/usr/bin/env python3
"""Resolve issuer-verified, share-only duplicate dividend rows.

The reviewed facts are an explicit input, not an implied provider correction.
Only ordinary capital conversions with zero/empty cash legs are supported.
All other unresolved distributions remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pandas as pd


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile(
    document: dict, facts: dict, raw_root: Path, pdf_root: Path
) -> tuple[dict, list[dict]]:
    if "unresolved" not in document.get("coverage", {}):
        raise ValueError("corporate coverage has no unresolved list")
    events = list(document["events"])
    unresolved = list(document["coverage"]["unresolved"])
    applied = []
    used = set()
    for fact in facts["facts"]:
        if fact.get("kind") != "ordinary_capital_conversion":
            continue
        if fact.get("status") != "source_verified_terms_only":
            raise ValueError("share fact has unexpected review status")
        code = fact["instrument_id"]
        terms = fact["terms"]
        key = (code, terms["record_date"], terms["ex_date"])
        if key in used:
            raise ValueError(f"duplicate reviewed share fact:{key}")
        used.add(key)
        ex_compact = terms["ex_date"].replace("-", "")
        record_compact = terms["record_date"].replace("-", "")
        matches = [
            row for row in unresolved
            if row["instrument_id"] == code
            and row.get("record_date") in (record_compact, terms["record_date"])
            and row.get("ex_date") == terms["ex_date"]
            and row["reason"] == "conflicting_duplicate"
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one conflicting group for issuer share fact:{key}")
        if any(e["instrument_id"] == code and e["ex_date"] == terms["ex_date"] for e in events):
            raise ValueError(f"ex-date already has an executable event:{key}")
        source_id = fact["source_id"].split(":", 1)[-1]
        pdf = pdf_root / f"{source_id}.pdf"
        metadata = pdf_root / f"{source_id}.source.json"
        meta = json.loads(metadata.read_text())
        if meta["sha256"] != digest(pdf) or meta["publication_date"] != fact["publication_date"]:
            raise ValueError(f"issuer source binding mismatch:{source_id}")
        if fact["publication_date"] >= terms["record_date"]:
            raise ValueError(f"issuer source not public by record date:{key}")
        raw_path = raw_root / f"{code}.parquet"
        raw = pd.read_parquet(raw_path)
        rows = raw[
            raw["div_proc"].eq("实施")
            & raw["record_date"].eq(record_compact)
            & raw["ex_date"].eq(ex_compact)
        ]
        if len(rows) < 2:
            raise ValueError(f"expected duplicate implemented vendor rows:{key}")
        ratio = Decimal(terms["shares_per_eligible_share"])
        for row in rows.to_dict("records"):
            if Decimal(str(row["stk_div"])) != ratio:
                raise ValueError(f"share ratio conflicts with issuer:{key}")
            if str(row["div_listdate"]) != terms["share_listing_date"].replace("-", ""):
                raise ValueError(f"share listing date conflicts with issuer:{key}")
            for column in ("cash_div", "cash_div_tax"):
                if pd.notna(row[column]) and Decimal(str(row[column])) != 0:
                    raise ValueError(f"cash leg cannot be reconciled as share-only:{key}")
            if pd.notna(row["pay_date"]):
                raise ValueError(f"unexpected cash payment date:{key}")
        fraction = Fraction(ratio)
        event = {
            "event_id": f"shr:{code}:{terms['ex_date']}",
            "instrument_id": code,
            "kind": "bonus_shares",
            "record_date": terms["record_date"],
            "ex_date": terms["ex_date"],
            "settlement_date": terms["share_listing_date"],
            "source_id": fact["source_id"],
            "share_numerator": fraction.numerator,
            "share_denominator": fraction.denominator,
        }
        events.append(event)
        unresolved.remove(matches[0])
        applied.append({
            "fact_id": fact["fact_id"],
            "event_id": event["event_id"],
            "issuer_pdf_sha256": digest(pdf),
            "issuer_metadata_sha256": digest(metadata),
            "vendor_rows_sha256": digest(raw_path),
            "vendor_row_count": len(rows),
        })
    if len(applied) != 4:
        raise ValueError(f"expected four issuer-verified share events; got {len(applied)}")
    result = {**document, "events": sorted(events, key=lambda e: (e["ex_date"], e["event_id"]))}
    result["coverage"] = {
        **document["coverage"],
        "unresolved": unresolved,
        "share_conversion_reconciliation": {
            "source": "issuer implementation announcements and matching vendor rows",
            "events": len(applied),
            "fractional_entitlement": "replay blocks without issuer/depository allocation",
        },
    }
    return result, applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corporate", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--pdf-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    document, applied = reconcile(
        json.loads(args.corporate.read_text()),
        json.loads(args.facts.read_text()),
        args.raw_root,
        args.pdf_root,
    )
    args.output_dir.mkdir(parents=True)
    out = args.output_dir / "corporate_actions.json"
    out.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    receipt = {
        "schema": "quantlab_share_conversion_reconciliation_v1",
        "inputs": {str(p): digest(p) for p in (args.corporate, args.facts, Path(__file__))},
        "applied": applied,
        "output_sha256": digest(out),
        "remaining_unresolved": len(document["coverage"]["unresolved"]),
    }
    (args.output_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print(f"reconciled={len(applied)} unresolved={receipt['remaining_unresolved']} output={out}")


if __name__ == "__main__":
    main()
