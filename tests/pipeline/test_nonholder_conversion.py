"""A restructuring share increase need not be a shareholder distribution."""

import hashlib
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts/reconcile_csi800_nonholder_conversions.py"
_SPEC = importlib.util.spec_from_file_location("nonholder_reconciliation", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
reconcile = _MODULE.reconcile


def test_issuer_proven_nonholder_conversion_preserves_other_gaps(tmp_path):
    source = tmp_path / "issuer.pdf"
    source.write_bytes(b"issuer notice fixture")
    raw = tmp_path / "002309.SZ.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施",
                "record_date": "20241223",
                "ex_date": "20241224",
                "stk_div": 2.45,
                "div_listdate": None,
                "cash_div": None,
                "cash_div_tax": None,
            }
        ]
    ).to_parquet(raw)
    fact = {
        "instrument_id": "002309.SZ",
        "record_date": "2024-12-23",
        "ex_date": "2024-12-24",
        "vendor_stk_div": "2.45",
        "ordinary_holder_share_ratio": "0",
        "source_file": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "vendor_rows_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "status": "issuer_verified_no_ordinary_holder_entitlement",
    }
    issue = {
        "instrument_id": "002309.SZ",
        "record_date": "20241223",
        "ex_date": "2024-12-24",
        "reason": "missing_div_listdate",
    }
    advisor = tmp_path / "advisor.pdf"
    advisor.write_bytes(b"financial advisor fixture")
    bird_raw = tmp_path / "603555.SH.parquet"
    pd.DataFrame(
        [
            {
                "div_proc": "实施", "record_date": "20210608", "ex_date": "20210609",
                "stk_div": 1.5, "div_listdate": "20210610", "cash_div": None,
                "cash_div_tax": None,
            },
            {
                "div_proc": "实施", "record_date": "20210608", "ex_date": "20210609",
                "stk_div": 1.5, "div_listdate": "20210610", "cash_div": 0.0,
                "cash_div_tax": 0.0,
            },
        ]
    ).to_parquet(bird_raw)
    bird_fact = {
        "instrument_id": "603555.SH",
        "record_date": "2021-06-08", "ex_date": "2021-06-09",
        "vendor_stk_div": "1.5", "ordinary_holder_share_ratio": "0",
        "source_file": advisor.name,
        "source_sha256": hashlib.sha256(advisor.read_bytes()).hexdigest(),
        "vendor_rows_sha256": hashlib.sha256(bird_raw.read_bytes()).hexdigest(),
        "status": "issuer_verified_no_ordinary_holder_entitlement",
    }
    bird_issue = {
        "instrument_id": "603555.SH", "record_date": "20210608",
        "ex_date": "2021-06-09", "reason": "conflicting_duplicate",
    }
    more_facts = []
    more_issues = []
    for code, record, ex, ratio, listing in (
        ("002122.SZ", "20221221", "20221222", 0.628, "20221222"),
        ("002157.SZ", "20231207", "20231208", 1.623, "20231208"),
    ):
        source_path = tmp_path / f"{code}.pdf"
        source_path.write_bytes(f"issuer notice {code}".encode())
        raw_path = tmp_path / f"{code}.parquet"
        pd.DataFrame(
            [
                {
                    "div_proc": "实施", "record_date": record, "ex_date": ex,
                    "stk_div": ratio, "div_listdate": listing, "cash_div": cash,
                    "cash_div_tax": cash,
                }
                for cash in (None, 0.0)
            ]
        ).to_parquet(raw_path)
        more_facts.append(
            {
                "instrument_id": code,
                "record_date": f"{record[:4]}-{record[4:6]}-{record[6:]}",
                "ex_date": f"{ex[:4]}-{ex[4:6]}-{ex[6:]}",
                "vendor_stk_div": str(ratio), "ordinary_holder_share_ratio": "0",
                "source_file": source_path.name,
                "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "vendor_rows_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "status": "issuer_verified_no_ordinary_holder_entitlement",
            }
        )
        more_issues.append(
            {
                "instrument_id": code,
                "record_date": record,
                "ex_date": f"{ex[:4]}-{ex[4:6]}-{ex[6:]}",
                "reason": "conflicting_duplicate",
            }
        )
    other = {"instrument_id": "300116.SZ", "reason": "missing_div_listdate"}
    all_issues = [issue, bird_issue, *more_issues, other]
    document = {"events": [], "coverage": {"unresolved": all_issues}}
    all_facts = [fact, bird_fact, *more_facts]

    updated, applied = reconcile(document, {"facts": all_facts}, tmp_path, tmp_path)
    assert updated["events"] == []
    assert updated["coverage"]["unresolved"] == [other]
    assert {item["classification"] for item in applied} == {"no_ordinary_holder_entitlement"}
    assert document["coverage"]["unresolved"] == all_issues
    assert {item["instrument_id"] for item in applied} == {
        "002309.SZ", "603555.SH", "002122.SZ", "002157.SZ"
    }

    bad_fact = {**fact, "ordinary_holder_share_ratio": "0.05"}
    with pytest.raises(ValueError, match="nonzero holder entitlement"):
        reconcile(document, {"facts": [bad_fact, bird_fact, *more_facts]}, tmp_path, tmp_path)
    source.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="issuer source changed"):
        reconcile(document, {"facts": all_facts}, tmp_path, tmp_path)
