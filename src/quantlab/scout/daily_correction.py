"""A single model-authored patch, limited to reported fields, never a new full decision."""

import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from quantlab.scout.daily_contract import ANALYSIS_FIELDS, FIELDS, REF, semantic_shape


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


def patch_plan(previous, errors, schema):
    """Only support well-formed comparison rows and explicitly local repair causes.

    Invalid/truncated documents or coupled type/rank changes use the existing one
    full correction instead. Never guess a path or silently edit model decisions.
    """
    if not isinstance(previous, dict) or not isinstance(previous.get("comparisons"), list):
        return None
    row_schema = schema.get("properties", {}).get("comparisons", {}).get("items")
    if not isinstance(row_schema, dict):
        return None
    rows = previous["comparisons"]
    safe_shape = semantic_shape(row_schema)
    if any(not Draft202012Validator(safe_shape).is_valid(row) for row in rows):
        return None
    indices = {r["instrument_id"]: i for i, r in enumerate(rows)}
    if len(indices) != len(rows):
        return None
    targets = {}

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
            return None
        if len(path) != 2 or path[0] not in indices:
            return None
        index, field = indices[path[0]], path[1]
        base = ["comparisons", index]
        if code.startswith("placeholder_not_declared:"):
            if not add([*base, "fact_ids"]):
                return None
        elif code == "same_type_unselected_required":
            if not add([*base, "comparator_id"]) or not add([*base, "fact_ids"]):
                return None
            # Changing the peer requires revising explicit references to that peer.
            peer = rows[index].get("comparator_id")
            for key in (*FIELDS, *ANALYSIS_FIELDS, "trade_known", "trade_unknown"):
                suffix = (
                    ["analysis", key]
                    if key in ANALYSIS_FIELDS
                    else ["trade_conditions", key.removeprefix("trade_")]
                    if key.startswith("trade_")
                    else [key]
                )
                text = get_at(previous, [*base, *suffix])
                if isinstance(text, str) and any(
                    f"fact:{peer}:" in ref for ref in REF.findall(text)
                ):
                    add([*base, *suffix])
        elif code in {
            "quantitative_prose_requires_fact_placeholder",
            "unsupported_microstructure_assertion",
        }:
            suffix = (
                ["analysis", field]
                if field in ANALYSIS_FIELDS
                else ["trade_conditions", field.removeprefix("trade_")]
                if field in {"trade_known", "trade_unknown"}
                else [field]
            )
            if not add([*base, *suffix]) or not add([*base, "fact_ids"]):
                return None
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
            row = rows[path[1]] if len(path) >= 3 and path[0] == "comparisons" else None
            peer_changed = row and pointer(["comparisons", path[1], "comparator_id"]) in targets
            old_peer = row.get("comparator_id") if peer_changed else None
            if row and path[-1] == "fact_ids":
                keep = [
                    r
                    for r in row["fact_ids"]
                    if not old_peer or not r.startswith(f"fact:{old_peer}:")
                ]
                properties["value"]["allOf"] = [{"contains": {"const": ref}} for ref in keep]
            elif isinstance(get_at(previous, path), str):
                keep = [
                    r
                    for r in REF.findall(get_at(previous, path))
                    if not old_peer or not r.startswith(f"fact:{old_peer}:")
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
