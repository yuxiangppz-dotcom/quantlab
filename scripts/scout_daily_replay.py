"""Replay immutable real inputs/output offline; fixture repairs are NOT new predictions."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from quantlab.scout.daily_budget import DailyResearch
from quantlab.scout.daily_contract import (
    ANALYSIS_FIELDS,
    FIELDS,
    INSTRUCTION,
    REF,
    VERSION,
    compact,
    compact_fact_refs,
    remap_generated_refs,
    render_output,
    research_packet,
    selection_schema,
    validate_output,
)
from quantlab.scout.daily_correction import get_at, patch_plan
from quantlab.scout.daily_stages import investigation_contract, investigation_errors
from quantlab.scout.models import fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--response", type=Path, help="Archived public response for truncated runs")
    parser.add_argument("--stage", choices=["selection", "investigation"], default="selection")
    parser.add_argument("--request", type=Path, help="Exact archived investigation request")
    args = parser.parse_args()
    original_bytes = args.source.read_bytes()
    source = json.loads(original_bytes)
    saved_packet = source["selection_input_packet"]
    saved_request = None
    if args.stage == "investigation":
        saved_request = json.loads(args.request.read_bytes())
        saved_prompt = saved_request["prompt"]
        saved_packet = json.loads(saved_prompt[saved_prompt.index('{"version"') :])
    packet = (
        json.loads(json.dumps(saved_packet))
        if "fact_columns" in saved_packet
        else research_packet(saved_packet)
    )
    packet, aliases = compact_fact_refs(packet)
    packet["version"] = VERSION
    raw_output = source["selection_raw"]
    public_response = json.loads(args.response.read_bytes()) if args.response else None
    if public_response:
        try:
            raw_output = json.loads(public_response["choices"][0]["message"]["content"])
        except (ValueError, TypeError):
            pass
    original_output_hash = fingerprint(raw_output)
    raw_output = remap_generated_refs(raw_output, aliases)
    schema = (
        investigation_contract(packet["candidates"], packet)
        if args.stage == "investigation"
        else selection_schema(packet["candidates"])
    )
    validator = (
        (lambda v: investigation_errors(v, packet, schema))
        if args.stage == "investigation"
        else (lambda v: validate_output(v, packet))
    )
    errors = validator(raw_output)
    subjects = {key: value[0] for key, value in packet["facts"].items()}
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
    plan = patch_plan(raw_output, errors, schema, fact_subjects=subjects)
    if plan:
        fixture = {"patches": []}
        for name, target in sorted(plan["targets"].items()):
            item = {"path": name, "op": target["op"]}
            path = target["path"]
            if target["op"] == "replace":
                row = raw_output[path[0]][path[1]]
                if path[-1] == "relation":
                    item["value"] = "sentiment"
                elif path[-1] in {"event_ids", "scale_fact_ids"}:
                    item["value"] = []
                elif path[-1] == "fact_ids":
                    texts = [row[f] for f in FIELDS] + [row["analysis"][f] for f in ANALYSIS_FIELDS]
                    texts += list(row["trade_conditions"].values())
                    item["value"] = list(
                        dict.fromkeys(row["fact_ids"] + [r for t in texts for r in REF.findall(t)])
                    )
                elif path[-1] == "comparator_id":
                    issue = next(
                        e
                        for e in errors
                        if e.get("path") == [row["instrument_id"], "comparator_id"]
                    )
                    item["value"] = issue["allowed_comparator_ids"][0]
                else:
                    refs = REF.findall(get_at(raw_output, path))
                    item["value"] = (
                        "；".join("[[" + ref + "]]" for ref in refs)
                        if refs
                        else "仅离线定向修正演练；原判断和反证完整另存，不是预测"
                    )
                assert get_at(raw_output, path) is not None or path[-1] == "comparator_id"
            fixture["patches"].append(item)

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
    before_study = {**packet, "investigation_opportunities": []}
    precheck = research.check_input(
        INSTRUCTION + compact(before_study), schema, extra_chars=45000, extra_bytes=100000
    )
    result, _ = research.ask(prompt, schema, validator=validator, fact_subjects=subjects)
    unpatched_fields_preserved = None
    if plan:
        masked_before, masked_after = deepcopy(raw_output), deepcopy(result)
        for target in plan["targets"].values():
            path = target["path"]
            if target["op"] == "remove":
                del get_at(masked_before, path[:-1])[path[-1]]
                assert path[-1] not in get_at(masked_after, path[:-1])
            else:
                get_at(masked_before, path[:-1])[path[-1]] = "authorized-field-mask"
                get_at(masked_after, path[:-1])[path[-1]] = "authorized-field-mask"
        assert masked_before == masked_after
        unpatched_fields_preserved = True
    rendered = result if args.stage == "investigation" else render_output(result, packet)
    originals = {e["evidence_id"]: e["body"] for e in saved_packet["evidence"]}
    assert all(originals[e["evidence_id"]] == e["body"] for e in packet["evidence"])
    assert args.source.read_bytes() == original_bytes
    assert validator(result) == []
    audit = {
        "kind": "offline_real_sample_replay_not_prediction",
        "paid_requests": 0,
        "source_sha256": fingerprint(source),
        "original_output_sha256": original_output_hash,
        "stage": args.stage,
        "reference_translation_only": True,
        "original_request_sha256": fingerprint(saved_request) if saved_request else None,
        "original_response_sha256": fingerprint(public_response) if public_response else None,
        "packet_sha256": fingerprint(packet),
        "candidate_count": len(packet["candidates"]),
        "evidence_count": len(packet["evidence"]),
        "facts_count": len(packet["facts"]),
        "original_prompt_chars": len(
            saved_request["prompt"] if saved_request else source["selection_input_prompt"]
        ),
        "new_prompt_chars": len(prompt),
        "new_prompt_bytes": len(prompt.encode()),
        "pre_investigation_reserved_bound": precheck,
        "source_bodies_unchanged": True,
        "unpatched_fields_preserved": unpatched_fields_preserved,
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
