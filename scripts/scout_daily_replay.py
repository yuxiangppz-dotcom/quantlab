"""Replay immutable real inputs/output offline; fixture repairs are NOT new predictions."""

import argparse
import json
from pathlib import Path

from quantlab.scout.daily_budget import DailyResearch
from quantlab.scout.daily_contract import (
    INSTRUCTION,
    VERSION,
    compact,
    render_output,
    research_packet,
    selection_schema,
    validate_output,
)
from quantlab.scout.models import fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--response", type=Path, help="Archived public response for truncated runs")
    args = parser.parse_args()
    original_bytes = args.source.read_bytes()
    source = json.loads(original_bytes)
    saved_packet = source["selection_input_packet"]
    packet = (
        json.loads(json.dumps(saved_packet))
        if "fact_columns" in saved_packet
        else research_packet(saved_packet)
    )
    packet["version"] = VERSION
    raw_output = source["selection_raw"]
    errors = validate_output(raw_output, packet)
    public_response = json.loads(args.response.read_bytes()) if args.response else None
    if raw_output is None and public_response:
        try:
            raw_output = json.loads(public_response["choices"][0]["message"]["content"])
            errors = validate_output(raw_output, packet)
        except (ValueError, TypeError):
            pass
    fixture = {
        "comparisons": [
            {"instrument_id": c["instrument_id"], "analysis": {}} for c in packet["candidates"]
        ]
    }
    for row in fixture["comparisons"]:
        row.pop("quant_claims", None)
        row.update(
            primary_type="insufficient_evidence",
            type_labels=["insufficient_evidence"],
            rank=None,
            final_status="unselected",
            comparator_id=None,
            comparison_strength="insufficient",
            evidence_reliability="insufficient",
            fact_ids=[],
            evidence_ids=["market:" + row["instrument_id"]],
        )
        for key in (
            "thesis",
            "risk",
            "invalidation",
            "difference",
            "independent_basis",
            "unknowns",
        ):
            row[key] = "离线结构演练；原始判断与反证另存，效果未知"
        row["trade_conditions"] = {"known": "日线快照已归档", "unknown": "未知：可成交性与排队"}
        analysis = row["analysis"]
        for key in (
            "incremental_change",
            "economic_link",
            "importance",
            "h5_mechanism",
            "next_node_basis",
        ):
            analysis[key] = "仅离线结构演练，不是新模型判断"
        analysis.update(
            novelty="unknown",
            exposure="unknown",
            event_ids=[],
            scale_fact_ids=[],
            next_observation_date=None,
            next_node_is_hypothesis=False,
        )
    fixture["market_view"] = "离线回放样本，不是新预测"
    # Exercise actual subject/period binding with this real fact table.
    own = fixture["comparisons"][0]
    ref = next(
        k
        for k, v in packet["facts"].items()
        if v[0] == own["instrument_id"] and v[1] == "return_1d"
    )
    own["fact_ids"] = [ref]
    own["thesis"] = "仅演练程序格式：[[" + ref + "]]"

    class Replay:
        model = "offline-real-sample-fixture"

        def __init__(self):
            self.calls = []
            self.outputs = iter([raw_output, fixture])

        def ask(self, prompt, schema):
            self.calls.append({"offline": True})
            value = next(self.outputs)
            if value is None and public_response:
                self.failed_response = {
                    **public_response,
                    "usage": {"total_tokens": 0},
                    "offline_replay": True,
                }
                raise ValueError("Replayed real truncated response")
            return value, {
                "choices": [{"finish_reason": "stop", "message": {"content": compact(value)}}],
                "usage": {"total_tokens": 0},
            }

    args.output.mkdir(parents=True, exist_ok=False)
    if public_response:
        (args.output / "original-response.json").write_bytes(args.response.read_bytes())
    research = DailyResearch(Replay(), journal=args.output / "requests")
    prompt = INSTRUCTION + compact(packet)
    schema = selection_schema(packet["candidates"])
    before_study = {**packet, "investigation_opportunities": []}
    precheck = research.check_input(
        INSTRUCTION + compact(before_study), schema, extra_chars=45000, extra_bytes=100000
    )
    result, _ = research.ask(prompt, schema, validator=lambda v: validate_output(v, packet))
    rendered = render_output(result, packet)
    originals = {e["evidence_id"]: e["body"] for e in source["selection_input_packet"]["evidence"]}
    assert all(originals[e["evidence_id"]] == e["body"] for e in packet["evidence"])
    assert args.source.read_bytes() == original_bytes
    assert validate_output(result, packet) == []
    audit = {
        "kind": "offline_real_sample_replay_not_prediction",
        "paid_requests": 0,
        "source_sha256": fingerprint(source),
        "original_output_sha256": fingerprint(raw_output),
        "original_response_sha256": fingerprint(public_response) if public_response else None,
        "packet_sha256": fingerprint(packet),
        "candidate_count": len(packet["candidates"]),
        "evidence_count": len(packet["evidence"]),
        "facts_count": len(packet["facts"]),
        "original_prompt_chars": len(source["selection_input_prompt"]),
        "new_prompt_chars": len(prompt),
        "new_prompt_bytes": len(prompt.encode()),
        "pre_investigation_reserved_bound": precheck,
        "source_bodies_unchanged": True,
        "legacy_contract_errors": errors,
        "replay_budget": research.summary(),
        "fixture_validation": "passed",
        "rendered_fixture": rendered,
    }
    (args.output / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    (args.output / "research-input.json").write_text(compact(packet))
    print(
        json.dumps(
            {
                k: v
                for k, v in audit.items()
                if k not in {"legacy_contract_errors", "rendered_fixture"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
