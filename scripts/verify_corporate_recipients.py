"""Apply the recipient guard once to the frozen twenty manually reviewed events."""

import json
from collections import Counter
from dataclasses import asdict, fields
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from quantlab.data.models import DataValidationError
from quantlab.research.alpha158_store import Budget, atomic_seal, exclusive_job, peak_rss_bytes
from quantlab.research.cohort_dividends import pushed_head
from quantlab.research.corporate_recipients import RecipientEvidence, recipient_guard
from quantlab.research.corporate_terms import RESOURCES, MinimumCorporateTerms
from quantlab.research.input_audit import _sha
from quantlab.research.round2_dataset import sealed_read, verify_entries

CONFIG = "config/corporate_recipient_review_v1.json"
OUTPUT = "data/products/research_program/launch_20260912/corporate_20_primary_review"


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / CONFIG).read_text())
    verify_entries(root, config["inputs"])
    if config["output"] != OUTPUT or config["schema"] != "corporate_recipient_review_v1":
        raise DataValidationError("unreviewed recipient run contract")
    out = root / OUTPUT
    scope = sealed_read(out / "scope.json")
    review = sealed_read(out / "reviewed_annotations.json")
    downloads = sealed_read(out / "downloads.json")
    if (
        scope["fingerprint"] != config["scope_fingerprint"]
        or review["fingerprint"] != config["annotations_fingerprint"]
        or review["scope_fingerprint"] != scope["fingerprint"]
        or review["downloads_fingerprint"] != downloads["fingerprint"]
        or review["sources"] != {s["id"]: s for s in downloads["sources"]}
    ):
        raise DataValidationError("recipient source lineage changed")
    ids = {r["observation_id"] for r in scope["occurrences"]}
    if (
        len(ids) != 20
        or len(review["rows"]) != 20
        or {r["observation_id"] for r in review["rows"]} != ids
    ):
        raise DataValidationError("recipient occurrence scope changed")
    head, config_sha = pushed_head(root), _sha(root / CONFIG)
    with exclusive_job(root / "data/runtime/research/heavy_job"):
        budget = Budget(out, RESOURCES)
        atomic_seal(
            out / "guard_started.json",
            {
                "at": datetime.now(UTC).isoformat(),
                "source_head": head,
                "config_sha256": config_sha,
                "attempt": 1,
            },
        )
        with budget.watchdog():
            budget.check()
            raw_terms = pq.read_table(root / config["terms"], use_threads=False).to_pylist()
            selected = {r["observation_id"]: r for r in raw_terms if r["observation_id"] in ids}
            if set(selected) != ids:
                raise DataValidationError("minimum-term observations missing")
            results = []
            for row in review["rows"]:
                source_term = selected[row["observation_id"]]
                kwargs = {
                    f.name: source_term[f.name] for f in fields(MinimumCorporateTerms) if f.init
                }
                for key in ("record_date", "ex_date", "pay_date", "share_listing_date"):
                    kwargs[key] = None if kwargs[key] is None else date.fromisoformat(kwargs[key])
                for key in (
                    "cash_before_tax",
                    "total_stock_ratio",
                    "bonus_ratio",
                    "conversion_ratio",
                ):
                    kwargs[key] = None if kwargs[key] is None else Decimal(kwargs[key])
                for key in (
                    "common_blockers",
                    "cash_blockers",
                    "quantity_blockers",
                    "required_context",
                ):
                    kwargs[key] = tuple(kwargs[key])
                term = MinimumCorporateTerms(**kwargs)
                if term.cash_before_tax is not None:
                    raise DataValidationError("unknown cash source was overwritten")
                evidence, pdf = None, None
                if row["source_id"] is not None:
                    source = review["sources"][row["source_id"]]
                    pdf = (root / source["path"]).read_bytes()
                    evidence = RecipientEvidence(
                        row["observation_id"],
                        row["instrument_id"],
                        date.fromisoformat(row["record_date"]) if row["record_date"] else None,
                        date.fromisoformat(row["ex_date"]) if row["ex_date"] else None,
                        Decimal(row["total_stock_ratio"]) if row["total_stock_ratio"] else None,
                        row["share_classification"],
                        row["source_kind"],
                        source["url"],
                        source["sha256"],
                        tuple(row["recipient_pages"]),
                    )
                result = asdict(recipient_guard(term, evidence, pdf))
                result.update(
                    instrument_id=term.instrument_id,
                    provider_cash_before_tax=None,
                    cash_notice_annotation=row["cash_classification"],
                )
                results.append(result)
            verify_entries(root, config["inputs"])
            if pushed_head(root) != head or _sha(root / CONFIG) != config_sha:
                raise DataValidationError("recipient source changed during evaluation")
            # Frozen source-reading conclusions, not a second run or independent proof.
            passes = [r for r in results if r["ordinary_recipient_evidence_complete"]]
            if len(passes) != 1 or passes[0]["instrument_id"] != "300795.SZ":
                raise DataValidationError("guard disagrees with fixed manual review")
            report = atomic_seal(
                out / "guard_report.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "source_head": head,
                    "config_sha256": config_sha,
                    "scope_fingerprint": scope["fingerprint"],
                    "annotations_fingerprint": review["fingerprint"],
                    "results": results,
                    "cash_annotations": dict(
                        Counter(r["cash_classification"] for r in review["rows"])
                    ),
                    "share_annotations": dict(
                        Counter(r["share_classification"] for r in review["rows"])
                    ),
                    "ordinary_recipient_evidence_complete": len(passes),
                    "ordinary_recipient_evidence_blocked": 20 - len(passes),
                    "verification": "hashes, exact20scope, manual-reading cross-check; same agent",
                    "provider_requests": 0,
                    "economic_paths": 0,
                    "model_fits": 0,
                    "cashflow_eligible": False,
                    "historical_pit_certified": False,
                    "execution_authority": False,
                    "peak_rss_bytes": peak_rss_bytes(),
                    "artifact_bytes_before_report": budget.check(),
                },
            )
            print(json.dumps({k: v for k, v in report.items() if k != "results"}))


if __name__ == "__main__":
    main()
