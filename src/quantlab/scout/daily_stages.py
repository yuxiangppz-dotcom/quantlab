"""Research-stage contracts using the same facts as the final comparison."""

from copy import deepcopy

from jsonschema import Draft202012Validator

from quantlab.scout.ai import DISCOVERY_SCHEMA
from quantlab.scout.daily_contract import (
    REF,
    compact,
    compact_fact_refs,
    research_packet,
    unpack_facts,
)
from quantlab.scout.daily_semantics import INSTRUCTION as SEMANTIC_INSTRUCTION
from quantlab.scout.daily_semantics import prose_errors
from quantlab.scout.decision_contract import INVESTIGATION, SCHEMA_VERSION, SHARED
from quantlab.scout.opportunity_ai import investigation_schema


def discovery_contract(*, nextday=False):
    schema = deepcopy(DISCOVERY_SCHEMA)
    schema["$id"] = "urn:quantlab:" + SCHEMA_VERSION + ":discovery"
    if nextday:
        from quantlab.scout.nextday_contract import SCHEMA_VERSION as NEXT_SCHEMA

        schema["$id"] = "urn:quantlab:" + NEXT_SCHEMA + ":discovery"
    rows = schema["properties"]["hypotheses"]
    rows["maxItems"] = 12
    props = rows["items"]["properties"]
    for key in ("summary", "counterargument"):
        props[key] = {"type": "string", "minLength": 1, "maxLength": 180}
    props["instrument_ids"] = {
        "type": "array",
        "maxItems": 4,
        "items": {"type": "string", "maxLength": 16},
    }
    props["source_urls"] = {
        "type": "array",
        "maxItems": 3,
        "items": {"type": "string", "maxLength": 512},
    }
    return schema


def packet_for_pool(
    pool,
    evidence,
    market,
    background,
    coverage,
    timing,
    hypotheses=(),
    *,
    nextday=False,
    experiment_profile="fused",
    opportunities=(),
):
    return compact_fact_refs(
        research_packet(
            {
                "candidates": pool,
                "evidence": evidence,
                "market": market,
                "industry_background": background,
                "coverage": coverage,
                "timing": timing,
                "hypotheses": list(hypotheses),
                **(
                    {
                        "prediction_objective": "next_session",
                        "experiment_profile": experiment_profile,
                        "opportunity_hypotheses": list(opportunities),
                    }
                    if nextday
                    else {}
                ),
            }
        )
    )[0]


def investigation_contract(pool, packet=None):
    schema = deepcopy(investigation_schema(pool))
    schema["$id"] = "urn:quantlab:" + SCHEMA_VERSION + ":investigation"
    hypotheses = schema["properties"]["hypotheses"]
    hypotheses["maxItems"] = 12
    # Fresh dicts avoid changing aliased STRING objects in the old schema.
    properties = hypotheses["items"]["properties"]
    for key in ("summary", "counterargument"):
        properties[key] = {"type": "string", "minLength": 1, "maxLength": 180}
    properties["instrument_ids"] = {
        "type": "array",
        "maxItems": 4,
        "items": {"type": "string", "maxLength": 16},
    }
    properties["source_urls"] = {
        "type": "array",
        "maxItems": 3,
        "items": {"type": "string", "maxLength": 512},
    }
    analysis = schema["properties"]["opportunities"]["items"]["properties"]["analysis"]
    analysis["properties"]["next_observation_date"] = {
        "type": ["string", "null"],
        "maxLength": 10,
        "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
    }
    for key in (
        "incremental_change",
        "economic_link",
        "importance",
        "h5_mechanism",
        "next_node_basis",
    ):
        analysis["properties"][key] = {"type": "string", "minLength": 1, "maxLength": 80}
    for key in ("event_ids", "scale_fact_ids"):
        analysis["properties"][key] = {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "maxLength": 160},
        }
    if packet is not None:
        facts = unpack_facts(packet)
        by_code = {c["instrument_id"]: c for c in packet["candidates"]}
        row = schema["properties"]["opportunities"]["items"]
        row["allOf"] = []
        for candidate in pool:
            code = candidate["instrument_id"]
            events = [e["record_id"] for e in by_code[code]["events"]]
            own_facts = [
                key
                for key, f in facts.items()
                if f["subject_id"] == code and f["unit"] != "provider_unit_unknown"
            ]
            row["allOf"].append(
                {
                    "if": {"properties": {"instrument_id": {"const": code}}},
                    "then": {
                        "properties": {
                            "analysis": {
                                "properties": {
                                    "event_ids": {"items": {"enum": events}}
                                    if events
                                    else {"maxItems": 0},
                                    "scale_fact_ids": {"items": {"enum": own_facts}}
                                    if own_facts
                                    else {"maxItems": 0},
                                }
                            }
                        }
                    },
                }
            )
    if packet and packet.get("prediction_objective") == "next_session":
        from quantlab.scout.nextday_contract import SCHEMA_VERSION as NEXT_SCHEMA

        schema["$id"] = "urn:quantlab:" + NEXT_SCHEMA + ":investigation"
        props = analysis["properties"]
        props["next_session_thesis"] = props.pop("h5_mechanism")
        analysis["required"] = [
            "next_session_thesis" if k == "h5_mechanism" else k for k in analysis["required"]
        ]
        # Subject/identity validation is done against the actual packet, once.
        schema["properties"]["opportunities"]["items"].pop("allOf", None)
    return schema


def investigation_errors(output, packet, schema):
    errors = [
        {"path": list(e.path), "code": "schema", "detail": e.message[:300]}
        for e in Draft202012Validator(schema).iter_errors(output)
    ]
    if not isinstance(output, dict) or not isinstance(output.get("opportunities"), list):
        return errors
    by_code = {c["instrument_id"]: c for c in packet["candidates"]}
    facts = unpack_facts(packet)
    counts = {}
    for row in output["opportunities"]:
        if not isinstance(row, dict):
            continue
        code = row.get("instrument_id")
        counts[code] = counts.get(code, 0) + 1
        if code not in by_code or not isinstance(row.get("analysis"), dict):
            continue
        events = {e["record_id"] for e in by_code[code]["events"]}
        allowed = {code, "industry:" + str(by_code[code].get("industry"))}
        for ref in row["analysis"].get("event_ids", []):
            if ref not in events:
                errors.append(
                    {
                        "path": [code, "event_ids"],
                        "code": "event_not_shown:" + ref,
                        "allowed_event_ids": sorted(events),
                        "actual": ref,
                    }
                )
        for ref in row["analysis"].get("scale_fact_ids", []):
            if ref not in facts or facts[ref]["subject_id"] != code:
                errors.append(
                    {"path": [code, "scale_fact_ids"], "code": "scale_requires_own_fact:" + ref}
                )
            elif facts[ref]["unit"] == "provider_unit_unknown":
                errors.append(
                    {"path": [code, "scale_fact_ids"], "code": "scale_unit_unverified:" + ref}
                )
        for key, value in row["analysis"].items():
            if isinstance(value, str):
                for issue in prose_errors(value, facts, (code,)):
                    errors.append({"path": [code, key], "code": issue, "actual": value})
                for ref in REF.findall(value):
                    if ref not in facts or facts[ref]["subject_id"] not in allowed:
                        errors.append({"path": [code, key], "code": "unknown_fact:" + ref})
    if set(counts) != set(by_code) or any(n != 1 for n in counts.values()):
        errors.append(
            {
                "path": ["opportunities"],
                "code": "coverage_incomplete_or_duplicate",
                "missing": sorted(set(by_code) - set(counts)),
            }
        )
    return errors


def investigation_prompt(packet):
    shared, stage = SHARED, INVESTIGATION
    if packet.get("prediction_objective") == "next_session":
        from quantlab.scout.nextday_contract import INVESTIGATION as NEXT_INVESTIGATION
        from quantlab.scout.nextday_contract import SHARED as NEXT_SHARED

        shared, stage = NEXT_SHARED, NEXT_INVESTIGATION
        stage += (
            "facts各行按fact_columns读取；subject_id/metric/period/unit整数是fact_dictionaries索引，"
            "按字典还原不能当数值；value保持原值。technical_fact_indices按事实行从零索引。\n"
        )
    return (
        shared
        + stage
        + SEMANTIC_INSTRUCTION
        + "你负责判断，程序负责数字格式；量化值仅用[[fact_id]]引用统一facts表，不能自行写数字。"
        "当前仅调查，不输出最终排名或分级。保留反证和来源缺口；首次采集不等于市场新消息，标题不是正文。"
        "event_ids只填本股events的record_id（event-开头），绝不能填ev-来源ID；"
        "若本股events为空，event_ids必须[]；不得自行生成或猜测ID。"
        "scale_fact_ids只填本股facts中的事实ID。未知日程为null，所有说明保持短句。"
        "opportunities覆盖全部候选；hypotheses最多十二条，两个数组限制不同。"
        "hypotheses.relation只能是direct/supply_chain/theme/sentiment；纯量价假设使用sentiment，"
        "price_only只用于analysis.exposure或novelty。不要把两个字段枚举混用。"
        "分组假设的summary/counterargument保持短句，事实引用使用表内短ID；不重复复述全文。"
        "说明简短，量化值不自行改写，只使用事实引用。\n" + compact(packet)
    )
