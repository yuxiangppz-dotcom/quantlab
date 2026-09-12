"""Verify the saved term adapter from parent fields without calling its mapper."""

import hashlib
import json
import math
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from quantlab.research.alpha158_store import atomic_seal
from quantlab.research.round2_dataset import sealed_read


def aggregate(rows):
    result = {"occurrences": len(rows), "codes": len({r["instrument_id"] for r in rows})}
    for key in ("cash_state", "quantity_state", "breakdown_state"):
        result[key] = dict(Counter(r[key] for r in rows))
    for key in (
        "cash_fields_complete",
        "quantity_fields_complete",
        "numerical_date_bundle_complete",
    ):
        result[key] = sum(r[key] for r in rows)
    result["blockers"] = dict(
        Counter(
            x
            for r in rows
            for key in ("common_blockers", "cash_blockers", "quantity_blockers")
            for x in r[key]
        )
    )
    return result


def check(root):
    started = time.monotonic()
    config = json.loads((root / "config/corporate_minimum_terms_v1.json").read_text())
    for name, entry in config["inputs"].items():
        raw = (root / name).read_bytes()
        assert len(raw) == entry["bytes"] and hashlib.sha256(raw).hexdigest() == entry["sha256"]
    out = root / config["output"]
    report = sealed_read(out / "report.json")
    raw = (out / "terms.parquet").read_bytes()
    assert report["artifact"] == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    parent = pq.read_table(root / config["paths"]["occurrences"], use_threads=False).to_pylist()
    source = {r["observation_id"]: r for r in parent if r["observed_date_scope"] == "in_window"}
    rows = pq.read_table(out / "terms.parquet", use_threads=False).to_pylist()
    assert len(rows) == len(source) == 945
    assert {r["observation_id"] for r in rows} == set(source)
    for row in rows:
        prior = source[row["observation_id"]]
        for key in (
            "content_id",
            "response_fingerprint",
            "instrument_id",
            "observed_at",
            "availability_bound_source",
            "record_date",
            "ex_date",
            "pay_date",
        ):
            assert row[key] == prior[key]
        assert row["share_listing_date"] == prior["div_listdate"]
        assert row["source_stage"] == prior["normalized_status"]
        for left, right in (
            ("cash_before_tax", "cash_div_tax"),
            ("total_stock_ratio", "stk_div"),
            ("bonus_ratio", "stk_bo_rate"),
            ("conversion_ratio", "stk_co_rate"),
        ):
            assert row[left] == (None if prior[right] is None else str(prior[right]))
        total, bonus, conversion, cash = [
            prior[k] for k in ("stk_div", "stk_bo_rate", "stk_co_rate", "cash_div_tax")
        ]
        common = {
            f
            for f in json.loads(prior["quality_flags"])
            if f.startswith("malformed_") or f == "unknown_status"
        }
        if prior["normalized_status"] != "实施" or not prior["implementation_candidate"]:
            common.add("not_implementation")
        if prior["candidate_conflict"] or prior["possible_event_conflict"]:
            common.add("unresolved_source_conflict")
        record, ex_day, pay, listing = [
            prior[k] for k in ("record_date", "ex_date", "pay_date", "div_listdate")
        ]
        if record is None or ex_day is None:
            common.add("record_or_ex_unknown")
        elif record >= ex_day:
            common.add("record_not_before_ex")
        cash_issues, share_issues = set(), set()
        if cash is None:
            cash_issues.add("cash_before_tax_unknown")
        elif cash > 0 and pay is None:
            cash_issues.add("positive_cash_pay_date_unknown")
        if pay and ex_day and pay < ex_day:
            cash_issues.add("payment_before_ex")
        breakdown = "total_unknown"
        if total is None:
            share_issues.add("total_stock_ratio_unknown")
        else:
            parts = [v for v in (bonus, conversion) if v is not None]
            inconsistent = (
                any(v > total for v in parts)
                or sum(parts) > total + 1e-8
                or len(parts) == 2
                and not math.isclose(total, sum(parts), rel_tol=0, abs_tol=1e-8)
            )
            if inconsistent:
                share_issues.add("observed_stock_components_inconsistent")
                breakdown = "inconsistent"
            else:
                breakdown = (
                    "not_required_for_zero_total"
                    if total == 0
                    else "complete_observed"
                    if len(parts) == 2
                    else "partial_unknown"
                )
            if total > 0 and listing is None:
                share_issues.add("positive_stock_listing_date_unknown")
        if listing and ex_day and listing < ex_day:
            share_issues.add("listing_before_ex")
        assert row["common_blockers"] == sorted(common)
        assert row["cash_blockers"] == sorted(cash_issues)
        assert row["quantity_blockers"] == sorted(share_issues)
        assert row["breakdown_state"] == breakdown
        assert row["cash_state"] == (
            "unknown" if cash is None else "known_zero" if cash == 0 else "known_positive"
        )
        assert row["quantity_state"] == (
            "unknown"
            if total is None
            else "known_no_quantity_change"
            if total == 0
            else "known_positive_ratio"
        )
        assert row["cash_fields_complete"] == (not common and not cash_issues)
        assert row["quantity_fields_complete"] == (not common and not share_issues)
        assert row["numerical_date_bundle_complete"] == (
            not common and not cash_issues and not share_issues
        )
        assert all(
            row[k] is False
            for k in ("cashflow_eligible", "historical_pit_certified", "execution_authority")
        )
        assert "unique_economic_event" in row["required_context"]
        assert "entitled_record_date_holdings" in row["required_context"]
        assert "tax_and_cash_posting_rules" in row["required_context"]
        assert ("stock_distribution_tax_breakdown" in row["required_context"]) == (
            bool(total) and total > 0 and breakdown != "complete_observed"
        )
    assert report["all"] == aggregate(rows)
    assert report["implementation"] == aggregate([r for r in rows if r["source_stage"] == "实施"])
    proof = atomic_seal(
        out / "independent_proof.json",
        {
            "at": datetime.now(UTC).isoformat(),
            "report_fingerprint": report["fingerprint"],
            "checked_rows": len(rows),
            "all_checks_passed": True,
            "method": (
                "separate parent-field arithmetic and complete row-set comparison; same-agent check"
            ),
            "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "seconds": time.monotonic() - started,
        },
    )
    print(json.dumps(proof))


if __name__ == "__main__":
    check(Path.cwd())
