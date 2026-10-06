"""Read-only saved-pool replay with real D0 technical data; never a new prediction."""

import argparse
import json
from copy import deepcopy
from datetime import date
from hashlib import sha256
from pathlib import Path

from quantlab.scout.daily_budget import DailyResearch
from quantlab.scout.daily_contract import (
    compact,
    compact_fact_refs,
    research_packet,
    selection_instruction,
    selection_schema,
    validate_output,
)
from quantlab.scout.daily_stages import investigation_contract, investigation_prompt
from quantlab.scout.market import scan_market
from quantlab.scout.models import fingerprint
from quantlab.scout.nextday_contract import DISCOVERY, SHARED, VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source_files = {
        p.name: sha256(p.read_bytes()).hexdigest() for p in args.source_run.iterdir() if p.is_file()
    }
    report = json.loads((args.source_run / "report.json").read_text(encoding="utf-8"))
    day = date.fromisoformat(report["market"]["session"])
    universe, market = scan_market(
        args.data_dir, day, 100_000_000, technical_enabled=True
    )
    saved = report["selection_input_packet"]
    candidates = deepcopy(report["candidates"])
    for row in candidates:
        row.pop("program_facts", None)
        code = row["instrument_id"]
        row["technical_snapshot"] = (
            universe[code].context.get("technical_snapshot", {}) if code in universe else {}
        )
    packet, _ = compact_fact_refs(
        research_packet(
            {
                "prediction_objective": "next_session",
                "experiment_profile": "fused",
                "candidates": candidates,
                "market": saved["market"],
                "timing": saved["timing"],
                "evidence": saved["evidence"],
                "coverage": saved["coverage"],
                "industry_background": report.get("opportunity", {}).get("industry_background", {}),
            }
        )
    )
    packet["replay_classification"] = "posthoc_saved_pool_real_D0_features_not_prediction"
    schema = selection_schema(packet["candidates"], packet)
    prompt = selection_instruction(packet) + compact(packet)

    class Offline:
        model = "no_transport_offline"

    budget = DailyResearch(Offline())
    preflight = budget.check_input(prompt, schema, extra_chars=36000, extra_bytes=85000)
    # Preserve the saved model's decisions. Newly mandatory fields are diagnosed,
    # not silently authored or upgraded by this deterministic replay.
    errors = validate_output(report.get("selection_raw"), packet)
    args.output.mkdir(parents=True, exist_ok=False)
    files = {
        "selection-input.json": packet,
        "selection-schema.json": schema,
        "investigation-schema.json": investigation_contract(candidates, packet),
        "saved-output-review.json": errors,
        "technical-snapshots.json": {
            c: universe[c].context.get("technical_snapshot", {})
            for c in (r["instrument_id"] for r in candidates)
            if c in universe
        },
    }
    for name, value in files.items():
        (args.output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    for name, text in {
        "selection-request.txt": prompt,
        "investigation-request.txt": investigation_prompt(packet),
        "discovery-template.txt": SHARED + DISCOVERY,
    }.items():
        (args.output / name).write_text(text, encoding="utf-8")
    unchanged = source_files == {
        p.name: sha256(p.read_bytes()).hexdigest() for p in args.source_run.iterdir() if p.is_file()
    }
    if not unchanged:
        raise ValueError("Original saved run changed during replay")
    receipt = {
        "classification": packet["replay_classification"],
        "version": VERSION,
        "model_calls": 0,
        "provider_calls": 0,
        "candidate_count": len(candidates),
        "new_prompt_chars": len(prompt),
        "new_prompt_bytes": len(prompt.encode()),
        "preflight_reserved_bound": preflight,
        "diagnostic_count": len(errors),
        "source_files_sha256": source_files,
        "source_files_unchanged": unchanged,
        "market_history_sessions": market["technical_history_sessions"],
        "technical_states": {
            r["instrument_id"]: r["technical_state"] for r in packet["candidates"]
        },
        "packet_sha256": fingerprint(packet),
        "schema_sha256": fingerprint(schema),
        "request_text_sha256": sha256(prompt.encode()).hexdigest(),
        "note": "Saved grades unchanged. Actual new model acceptance and D1 results pending.",
    }
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in receipt.items()
                if k not in {"source_files_sha256", "technical_states"}
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
