"""Read-only saved-input review and separately labelled presentation, with no network."""

import argparse
import json
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from quantlab.scout.concise_report import VIEW_VERSION, html, markdown
from quantlab.scout.daily_budget import DailyResearch
from quantlab.scout.daily_contract import (
    INSTRUCTION,
    VERSION,
    compact,
    selection_schema,
    validate_output,
)
from quantlab.scout.daily_stages import investigation_prompt
from quantlab.scout.decision_contract import RULES, SCHEMA_VERSION
from quantlab.scout.models import fingerprint
from quantlab.scout.selection_control import freeze_control, observe_control


def review_saved(run, output):
    run, output = Path(run), Path(output)
    if output.resolve().is_relative_to(run.resolve()):
        raise ValueError("Review output must be outside original run")
    if output.exists():
        raise ValueError("Review directory already exists; preserve prior artifacts")
    original_hashes = {
        p.name: sha256(p.read_bytes()).hexdigest() for p in run.iterdir() if p.is_file()
    }
    report = json.loads((run / "report.json").read_text())
    manifest = json.loads((run / "manifest.json").read_text())
    if fingerprint(report) != manifest["report_sha256"]:
        raise ValueError("Source report integrity mismatch")
    raw = report["selection_raw"]
    raw_origin = "saved_selection_raw"
    if not isinstance(raw, dict):
        # Preserve the last fully saved comparison even when validation failed;
        # it is evidence for review, never an accepted recommendation.
        responses = json.loads((run / "ai_responses.json").read_text())
        for response in reversed(responses):
            response = response.get("response", response)
            try:
                parsed = json.loads(response["choices"][0]["message"]["content"])
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("comparisons"), list):
                raw = parsed
                raw_origin = "saved_failed_response_not_accepted_selection"
                break
    if not isinstance(raw, dict) or not isinstance(raw.get("comparisons"), list):
        raise ValueError("No complete saved comparison to review; no replacement response")
    packet = deepcopy(report["selection_input_packet"])
    packet.update(version=VERSION, schema_version=SCHEMA_VERSION, research_rules=RULES)
    schema = selection_schema(packet["candidates"], packet)
    prompt = INSTRUCTION + compact(packet)
    # This checks the exact final input before any paid work; no client is called.
    budget = DailyResearch(SimpleNamespace(model="offline-no-client", calls=[]))
    reserved = budget.check_input(prompt, schema)
    errors = validate_output(raw, packet)
    candidates = {c["instrument_id"]: c for c in report["candidates"]}
    universe = {
        code: SimpleNamespace(score=c["score"], metrics=c["metrics"])
        for code, c in candidates.items()
    }
    control = freeze_control(
        raw["comparisons"], universe, packet["candidates"], packet.get("coverage", [])
    )
    pending = observe_control(
        control,
        [
            {"instrument_id": r["instrument_id"], "status": "d_open_not_yet_due"}
            for r in control["rows"]
        ],
    )
    audit = {
        "kind": "posthoc_engineering_review_not_new_forecast",
        "primary_eligible": False,
        "raw_output_origin": raw_origin,
        "source_run_id": report["run_id"],
        "source_report_sha256": fingerprint(report),
        "source_prompt_version": report["prompt_version"],
        "review_prompt_version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "model": report["ai_model"],
        "config_sha256": report["config_sha256"],
        "errors": errors,
        "error_counts": dict(Counter(e["code"] for e in errors)),
        "rows": [
            {
                "instrument_id": r["instrument_id"],
                "original_grade": r["final_status"],
                "original_rank": r["rank"],
                "new_grade": None,
                "review_status": "legacy_output_missing_new_contract_not_regraded",
                "errors": [
                    e
                    for e in errors
                    if r["instrument_id"] in e.get("path", [])
                    or (
                        len(e.get("path", [])) > 1
                        and e["path"][0] == "comparisons"
                        and e["path"][1] == i
                    )
                ],
            }
            for i, r in enumerate(raw["comparisons"])
        ],
        "final_prompt_chars": len(prompt),
        "reserved_input_output_tokens": reserved,
        "prompt_text_sha256": sha256(prompt.encode()).hexdigest(),
        "schema_sha256": fingerprint(schema),
        "provider_calls": 0,
        "model_calls": 0,
        "control_kind": "posthoc_saved_data_protocol_demo_not_original_prospective_freeze",
        "h5_state": "pending_no_future_price_fabrication",
    }
    display = deepcopy(report)
    display["display_replay"] = {"source_run_id": report["run_id"], "version": VIEW_VERSION}
    output.mkdir(parents=True)
    for name, value in (
        ("review.json", audit),
        ("review-input.json", packet),
        ("review-schema.json", schema),
        ("score-control-posthoc.json", control),
        ("score-observation-pending.json", pending),
    ):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (output / "selection-prompt-v2.txt").write_text(prompt, encoding="utf-8")
    (output / "investigation-prompt-v2.txt").write_text(
        investigation_prompt(packet), encoding="utf-8"
    )
    (output / "display-replay.html").write_text(html(display), encoding="utf-8")
    (output / "display-replay.md").write_text(markdown(display), encoding="utf-8")
    if any(
        sha256((run / name).read_bytes()).hexdigest() != value
        for name, value in original_hashes.items()
    ):
        raise ValueError("Original artifacts changed during review")
    receipt = {
        "source_files_sha256": original_hashes,
        "source_unchanged": True,
        "output_files_sha256": {
            p.name: sha256(p.read_bytes()).hexdigest() for p in output.iterdir()
        },
        "provider_calls": 0,
        "model_calls": 0,
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2))
    return {
        "output": str(output),
        "candidate_count": len(raw["comparisons"]),
        "original_files_unchanged": True,
        "errors": len(errors),
        "final_prompt_chars": len(prompt),
        "model_calls": 0,
        "markdown_chars": len(markdown(display)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(review_saved(args.run, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
