"""Validate a saved incomplete Scout model result without new provider or model calls."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from quantlab.data.storage import ParquetStorage
from quantlab.scout.ai import ShownEvidence, retain_valid_selection
from quantlab.scout.models import SHANGHAI, Evidence, fingerprint, timestamp
from quantlab.scout.pipeline import candidate_diagnostics, report_timing
from quantlab.scout.report import (
    hold_candidate_pool,
    present_selection,
    screen_notice_risks,
    write_report,
)


def revalidate(source: Path, canonical_dir: Path, output_root: Path) -> Path:
    raw_report = (source / "report.json").read_text(encoding="utf-8")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads(raw_report)
    if fingerprint(report) != manifest["report_sha256"]:
        raise ValueError("Source report hash mismatch")
    if (
        report["status"] not in {"incomplete", "live_research_unvalidated"}
        or not report.get("selection_raw")
        or (report["status"] == "live_research_unvalidated" and report["selection"]["selected"])
    ):
        raise ValueError("Only an incomplete or empty-selection run can be revalidated")
    shown_packet = report.get("selection_input_evidence")
    if not isinstance(shown_packet, list) or not shown_packet:
        raise ValueError("Source run did not archive the exact final prompt evidence")
    shown = {row["evidence_id"] for row in shown_packet}
    audited = next(
        set(row["evidence_ids"])
        for row in report["prompt_evidence_audit"]
        if row["stage"] == "selection"
    )
    if shown != audited:
        raise ValueError("Archived prompt evidence differs from its audit")
    cutoff = timestamp(report["timing"]["information_cutoff"])
    if any(timestamp(item["retrieved_at"]) > cutoff for item in shown_packet):
        raise ValueError("Saved prompt includes evidence later than input cutoff")
    evidence = [ShownEvidence.from_packet(row) for row in shown_packet]
    selected, rejected = retain_valid_selection(
        report["selection_raw"], report["candidates"], evidence, report["market"]
    )
    displayed = screen_notice_risks(
        present_selection(selected, report["candidates"]), report["evidence"]
    )
    now = datetime.now(SHANGHAI)
    calendar = [
        row.trade_date
        for row in ParquetStorage(canonical_dir).load_trading_calendar()
        if row.exchange == "SSE" and row.is_open
    ]
    timing = report_timing(calendar, now, date.fromisoformat(report["market"]["session"]), True)
    if timing["target_session"] != report["timing"]["target_session"]:
        raise ValueError("Revalidation crossed into a different target session")
    timing["information_cutoff"] = cutoff.isoformat()
    report.update(
        {
            "run_id": f"{now:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}",
            "status": "live_research_unvalidated",
            "finished_at": now.isoformat(),
            "timing": timing,
            "failure": None,
            "selection": displayed,
            "selection_validation": {
                "rejected": rejected,
                "validated_before_presentation": True,
                "shown_evidence_ids": sorted(shown),
                "revalidated_from_run_id": source.name,
            },
            "selection_presentation": (
                "saved original model reasoning; validated before presentation"
            ),
            "revalidated_from_run_id": source.name,
            "candidates": hold_candidate_pool(report["candidates"], displayed),
            "coverage": [row for row in report["coverage"] if row["source"] != "AI_pipeline"]
            + [
                {
                    "source": "saved_model_revalidation",
                    "status": "ok",
                    "count": len(selected["selected"]),
                    "detail": "No new market/provider/model call; original incomplete run retained",
                }
            ],
            "limitations": report["limitations"]
            + ["此版只重新验证已保存的原始模型输出；没有新增取数或模型调用；原失败版保留。"],
        }
    )
    report["candidate_diagnostics"] = candidate_diagnostics(
        {code: _candidate_from_dict(row) for code, row in report["market_universe"].items()},
        [
            report["market_universe"][code]
            for code in report["candidate_stages"]["cheap_candidates"]
        ],
        report["candidates"],
        displayed,
        [
            Evidence(**{key: value for key, value in row.items() if key != "evidence_id"})
            for row in report["evidence"]
        ],
        report["industry_memberships"],
    )
    raw_responses = json.loads((source / "ai_responses.json").read_text(encoding="utf-8"))
    return write_report(output_root, report, raw_responses)


def _candidate_from_dict(row: dict):
    from quantlab.scout.models import Candidate

    return Candidate(
        **{
            key: row[key]
            for key in (
                "instrument_id",
                "name",
                "metrics",
                "score",
                "routes",
                "evidence_ids",
                "cautions",
                "context",
            )
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--canonical-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(revalidate(args.source_run, args.canonical_dir, args.output_dir))
