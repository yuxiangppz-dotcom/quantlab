"""Validate a saved incomplete Scout model result without new provider or model calls."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time
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


def revalidate(
    source: Path, canonical_dir: Path, output_root: Path, validator_commit: str | None = None
) -> Path:
    raw_report = (source / "report.json").read_text(encoding="utf-8")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads(raw_report)
    if fingerprint(report) != manifest["report_sha256"]:
        raise ValueError("Source report hash mismatch")
    response_path = source / "ai_responses.json"
    if not response_path.is_file() or "ai_responses_sha256" not in manifest:
        raise ValueError("Source AI response or manifest hash missing")
    raw_responses = json.loads(response_path.read_text(encoding="utf-8"))
    if fingerprint(raw_responses) != manifest["ai_responses_sha256"]:
        raise ValueError("Source AI response hash mismatch")
    if report["status"] not in {"incomplete", "live_research_unvalidated"} or not report.get(
        "selection_raw"
    ):
        raise ValueError("Only a run with archived original model output can be revalidated")
    if (
        report["status"] == "live_research_unvalidated"
        and report["selection"]["selected"]
        and not report.get("selection_input_packet")
    ):
        raise ValueError("A nonempty source selection needs the exact final packet")
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
    exact_packet = report.get("selection_input_packet")
    if exact_packet and exact_packet.get("evidence") != shown_packet:
        raise ValueError("Archived final packet differs from exact prompt evidence")
    cutoff = timestamp(report["timing"]["information_cutoff"])
    if any(timestamp(item["retrieved_at"]) > cutoff for item in shown_packet):
        raise ValueError("Saved prompt includes evidence later than input cutoff")
    evidence = [ShownEvidence.from_packet(row) for row in shown_packet]
    packet_candidates = exact_packet["candidates"] if exact_packet else report["candidates"]
    typed_compatible = report.get("schema_version", 0) >= 4 and all(
        isinstance(row.get("quant_claims"), list)
        for row in report["selection_raw"].get("selected", [])
    )
    selected, rejected = retain_valid_selection(
        report["selection_raw"], packet_candidates, evidence, report["market"]
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
    source_target = report["timing"]["target_session"]
    same_target_before_open = (
        exact_packet
        and typed_compatible
        and source_target is not None
        and timing["target_session"] == source_target
        and now < datetime.combine(date.fromisoformat(source_target), time(9, 30), SHANGHAI)
    )
    if not same_target_before_open:
        timing = {
            **report["timing"],
            "generated_at": now.isoformat(),
            "report_kind": "posthoc_engineering_audit",
            "primary_eligible": False,
        }
    timing["information_cutoff"] = cutoff.isoformat()
    old_selection = report.get("selection") or {"selected": []}
    report.update(
        {
            "schema_version": 4,
            "run_id": f"{now:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}",
            "status": "live_research_unvalidated"
            if same_target_before_open
            else "posthoc_engineering_audit",
            "finished_at": now.isoformat(),
            "timing": timing,
            "failure": None,
            "selection": displayed,
            "selection_validation": {
                "rejected": rejected,
                "validated_before_presentation": True,
                "shown_evidence_ids": sorted(shown),
                "revalidated_from_run_id": source.name,
                "validator_version": "scout_core_facts_v4",
                "compatibility": (
                    "typed_quant_claims_v1"
                    if typed_compatible
                    else "legacy_unstructured_model_output"
                ),
            },
            "selection_presentation": (
                "saved original model output; legacy core numeric prose remains unstructured"
                if not typed_compatible
                else "saved original model output; typed core facts validated before presentation"
            ),
            "revalidated_from_run_id": source.name,
            "revalidation_provenance": {
                "source_run_id": source.name,
                "source_report_sha256": manifest["report_sha256"],
                "source_ai_responses_sha256": manifest["ai_responses_sha256"],
                "validator_version": "scout_core_facts_v4",
                "validator_code_commit": validator_commit or "unrecorded",
                "schema_version": 4,
                "compatibility": (
                    "typed_quant_claims_v1"
                    if typed_compatible
                    else "legacy_unstructured_model_output"
                ),
                "old_selected": [
                    (x["instrument_id"], x["status"]) for x in old_selection.get("selected", [])
                ],
                "new_selected": [
                    (x["instrument_id"], x["status"]) for x in displayed.get("selected", [])
                ],
                "full_final_packet_archived": bool(exact_packet),
            },
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
    parser.add_argument("--validator-commit", default=None)
    args = parser.parse_args()
    print(revalidate(args.source_run, args.canonical_dir, args.output_dir, args.validator_commit))
