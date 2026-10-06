"""A single model-authored patch, limited to reported fields, never a new full decision."""

import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from quantlab.scout.daily_contract import FIELDS, REF, analysis_fields, semantic_shape


def pointer(path):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def get_at(value, path):
    for key in path:
        value = value[key]
    return value


def schema_at(schema, path):
    for key in path:
        schema = schema["items"] if isinstance(key, int) else schema["properties"][key]
    return schema


def patch_plan(previous, errors, schema, *, fact_subjects=None):
    """Only support well-formed comparison rows and explicitly local repair causes.

    Invalid/truncated documents or coupled type/rank changes use the existing one
    full correction instead. Never guess a path or silently edit model decisions.
    """
    if not isinstance(previous, dict):
        return None
    array_key = "comparisons" if "comparisons" in previous else "opportunities"
    if not isinstance(previous.get(array_key), list):
        return None
    row_schema = schema.get("properties", {}).get(array_key, {}).get("items")
    if not isinstance(row_schema, dict):
        return None
    rows = previous[array_key]
    safe_shape = semantic_shape(row_schema)
    if any(not Draft202012Validator(safe_shape).is_valid(row) for row in rows):
        return None
    indices = {r["instrument_id"]: i for i, r in enumerate(rows)}
    if len(indices) != len(rows):
        return None
    targets = {}
    invalid_refs = {}
    for error in errors:
        if error["code"].startswith("unknown_or_wrong_subject_fact:"):
            invalid_refs.setdefault(error["path"][0], set()).add(error["code"].split(":", 1)[1])

    def add(path, op="replace"):
        path = tuple(path)
        try:
            get_at(previous, path)
            node = schema_at(schema, path) if op == "replace" else None
        except (KeyError, IndexError, TypeError):
            return False
        targets[pointer(path)] = {"path": path, "op": op, "schema": node}
        return True

    for error in errors:
        code = error["code"]
        if code == "adapter_invalid_response":
            continue  # The accompanying complete schema/semantic list is required below.
        path = error.get("path", [])
        if code == "schema":
            try:
                actual = get_at(previous, path)
                node = schema_at(schema, path)
            except (KeyError, IndexError, TypeError):
                return None
            if isinstance(actual, dict) and node.get("additionalProperties") is False:
                extras = set(actual) - set(node.get("properties", {}))
                if extras:
                    for key in sorted(extras):
                        add([*path, key], "remove")
                    continue
            if (
                path
                and isinstance(node.get("type"), str)
                and node["type"] in {"string", "array", "integer", "number"}
            ):
                if isinstance(path[-1], int):
                    path = path[:-1]
                if add(path):
                    continue
            return None
        if len(path) != 2 or path[0] not in indices:
            return None
        index, field = indices[path[0]], path[1]
        current_analysis_fields = analysis_fields(rows[index])
        base = [array_key, index]
        if array_key == "opportunities":
            if code.startswith("event_not_shown:") and add([*base, "analysis", "event_ids"]):
                continue
            if code.startswith(("scale_requires_own_fact:", "scale_unit_unverified:")) and add(
                [*base, "analysis", "scale_fact_ids"]
            ):
                continue
            return None
        if code.startswith("placeholder_not_declared:"):
            if not add([*base, "fact_ids"]):
                return None
        elif code.startswith(("scale_requires_declared_own_fact:", "scale_unit_unverified:")):
            if not add([*base, "analysis", "scale_fact_ids"]):
                return None
        elif code.startswith("unknown_or_wrong_subject_fact:"):
            ref = code.split(":", 1)[1]
            if "fact_ids" in rows[index]:
                add([*base, "fact_ids"])
            if ref in rows[index]["analysis"]["scale_fact_ids"]:
                add([*base, "analysis", "scale_fact_ids"])
            for key in (*FIELDS, *current_analysis_fields, "trade_known", "trade_unknown"):
                suffix = (
                    ["analysis", key]
                    if key in current_analysis_fields
                    else ["trade_conditions", key.removeprefix("trade_")]
                    if key.startswith("trade_")
                    else [key]
                )
                if ref in REF.findall(get_at(previous, [*base, *suffix])):
                    add([*base, *suffix])
        elif code == "same_type_unselected_required":
            if not add([*base, "comparator_id"]):
                return None
            if "fact_ids" in rows[index]:
                add([*base, "fact_ids"])
            # Changing the peer requires revising explicit references to that peer.
            peer = rows[index].get("comparator_id")
            for key in (*FIELDS, *current_analysis_fields, "trade_known", "trade_unknown"):
                suffix = (
                    ["analysis", key]
                    if key in current_analysis_fields
                    else ["trade_conditions", key.removeprefix("trade_")]
                    if key.startswith("trade_")
                    else [key]
                )
                text = get_at(previous, [*base, *suffix])
                if isinstance(text, str) and any(
                    (fact_subjects or {}).get(ref) == peer or f"fact:{peer}:" in ref
                    for ref in REF.findall(text)
                ):
                    add([*base, *suffix])
        elif code in {
            "quantitative_prose_requires_fact_placeholder",
            "unsupported_microstructure_assertion",
            "core_fact_clause_requires_neutral_label",
            "fund_improvement_requires_cross_time_facts",
            "fund_consistency_requires_structured_claim",
            "unbound_quantitative_interpretation",
        }:
            suffix = (
                ["analysis", field]
                if field in current_analysis_fields
                else ["trade_conditions", field.removeprefix("trade_")]
                if field in {"trade_known", "trade_unknown"}
                else [field]
            )
            if not add([*base, *suffix]):
                return None
            if "fact_ids" in rows[index]:
                add([*base, "fact_ids"])
        else:
            return None
    if not targets:
        return None
    branches, context = [], []
    for name, target in sorted(targets.items()):
        properties = {"path": {"const": name}, "op": {"const": target["op"]}}
        required = ["path", "op"]
        if target["op"] == "replace":
            properties["value"] = deepcopy(target["schema"])
            path = target["path"]
            row = rows[path[1]] if len(path) >= 3 and path[0] == array_key else None
            peer_changed = row and pointer([array_key, path[1], "comparator_id"]) in targets
            old_peer = row.get("comparator_id") if peer_changed else None
            bad_refs = invalid_refs.get(row["instrument_id"], set()) if row else set()

            def prior_peer_ref(ref, old_peer=old_peer):
                return old_peer and (
                    (fact_subjects or {}).get(ref) == old_peer
                    or ref.startswith(f"fact:{old_peer}:")
                )

            if row and path[-1] == "fact_ids":
                keep = [r for r in row["fact_ids"] if not prior_peer_ref(r) and r not in bad_refs]
                properties["value"]["allOf"] = [{"contains": {"const": ref}} for ref in keep]
            elif isinstance(get_at(previous, path), str):
                keep = [
                    r
                    for r in REF.findall(get_at(previous, path))
                    if not prior_peer_ref(r) and r not in bad_refs
                ]
                properties["value"]["allOf"] = [
                    {"pattern": re.escape("[[" + ref + "]]")} for ref in keep
                ]
            required.append("value")
        branches.append(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
        context.append(
            {"path": name, "op": target["op"], "previous": get_at(previous, target["path"])}
        )
    return {
        "targets": targets,
        "context": context,
        "schema": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": len(targets),
                    "items": {"oneOf": branches},
                }
            },
            "required": ["patches"],
            "additionalProperties": False,
        },
    }


def apply_patch_output(previous, patch, plan):
    errors = [
        {"path": list(e.path), "code": "patch_schema", "detail": e.message[:300]}
        for e in Draft202012Validator(plan["schema"]).iter_errors(patch)
    ]
    if errors:
        return None, errors
    result, changed = deepcopy(previous), set()
    for item in patch["patches"]:
        name = item["path"]
        if name in changed:
            return None, [{"code": "duplicate_patch_path", "path": [name]}]
        changed.add(name)
        path = plan["targets"][name]["path"]
        parent = get_at(result, path[:-1])
        if item["op"] == "remove":
            del parent[path[-1]]
        else:
            parent[path[-1]] = deepcopy(item["value"])
    return result, []
