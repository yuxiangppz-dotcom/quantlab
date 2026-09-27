"""The issuer-backed 600733 conversion must clear both identical vendor issues."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/reconcile_csi800_share_conversions.py"
SPEC = importlib.util.spec_from_file_location("share_conversion_reconcile", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_issuer_share_conversion_clears_only_matching_issues(tmp_path):
    specs = (
        ("000528.SZ", "20181025", "20181026", "0.3", "20181026", 2),
        ("000939.SZ", "20170619", "20170620", "0.5", "20170620", 2),
        ("688516.SH", "20221121", "20221122", "0.2", "20221122", 2),
        ("688516.SH", "20231116", "20231117", "0.4", "20231117", 2),
        ("600733.SH", "20180918", "20180919", "2.5", "20180920", 3),
    )
    facts, issues = [], []
    rows_by_code = {}
    for index, (code, record, ex, ratio, listing, copies) in enumerate(specs):
        source_id = str(index)
        pdf = tmp_path / f"{source_id}.pdf"
        pdf.write_bytes(f"reviewed issuer notice {source_id}".encode())
        (tmp_path / f"{source_id}.source.json").write_text(
            json.dumps({"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                        "publication_date": "2016-01-01"})
        )
        ex_iso = f"{ex[:4]}-{ex[4:6]}-{ex[6:]}"
        record_iso = f"{record[:4]}-{record[4:6]}-{record[6:]}"
        facts.append({
            "fact_id": f"test:{index}", "kind": "ordinary_capital_conversion",
            "status": "source_verified_terms_only", "instrument_id": code,
            "source_id": f"cninfo:{source_id}", "publication_date": "2016-01-01",
            "terms": {"record_date": record_iso, "ex_date": ex_iso,
                      "share_listing_date": f"{listing[:4]}-{listing[4:6]}-{listing[6:]}",
                      "shares_per_eligible_share": ratio},
        })
        issue = {"instrument_id": code, "record_date": record,
                 "ex_date": ex_iso, "reason": "conflicting_duplicate"}
        issues.extend([issue.copy()] * (2 if code == "600733.SH" else 1))
        rows_by_code.setdefault(code, []).extend(
            {"div_proc": "实施", "record_date": record, "ex_date": ex,
             "stk_div": float(ratio), "div_listdate": listing,
             "cash_div": 0.0, "cash_div_tax": 0.0, "pay_date": None}
            for _ in range(copies)
        )
    for code, rows in rows_by_code.items():
        pd.DataFrame(rows).to_parquet(tmp_path / f"{code}.parquet")
    unrelated = {"instrument_id": "300116.SZ", "reason": "missing_div_listdate"}
    document = {"events": [], "coverage": {"unresolved": [*issues, unrelated]}}
    result, applied = MODULE.reconcile(document, {"facts": facts}, tmp_path, tmp_path)
    assert len(applied) == 5
    assert result["coverage"]["unresolved"] == [unrelated]
    event = next(e for e in result["events"] if e["instrument_id"] == "600733.SH")
    assert (event["share_numerator"], event["share_denominator"]) == (5, 2)
    assert event["settlement_date"] == "2018-09-20"
    assert document["coverage"]["unresolved"] == [*issues, unrelated]

    bad = {"events": [], "coverage": {"unresolved": [*issues[:-1], unrelated]}}
    with pytest.raises(ValueError, match="unexpected conflicting groups"):
        MODULE.reconcile(bad, {"facts": facts}, tmp_path, tmp_path)
