"""Dedicated research input and explicit fact references, separate from legacy prose parsing."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal
from hashlib import sha256

from jsonschema import Draft202012Validator

from quantlab.scout.ai import asserts_unsupported_microstructure
from quantlab.scout.daily_semantics import (
    CLAIM_SCHEMA,
    claim_errors,
    prose_errors,
    render_claim,
    without_technical_labels,
)
from quantlab.scout.daily_semantics import (
    INSTRUCTION as SEMANTIC_INSTRUCTION,
)
from quantlab.scout.decision_contract import (
    RULE_SCHEMA,
    RULES,
    SCHEMA_VERSION,
    SELECTION,
    SHARED,
    legacy_errors,
    rule_text,
)
from quantlab.scout.facts import program_facts
from quantlab.scout.models import fingerprint
from quantlab.scout.opportunity_ai import OPPORTUNITY_SCHEMA

VERSION = "daily_facts_v8_decision_v2"
FIELDS = ("thesis", "risk", "invalidation", "difference", "independent_basis", "unknowns")
ANALYSIS_FIELDS = (
    "incremental_change",
    "economic_link",
    "importance",
    "h5_mechanism",
    "next_node_basis",
)
REF = re.compile(r"\[\[([^\[\]]+)\]\]")
FACT_COLUMNS = ("subject_id", "metric", "period", "value", "unit", "source_ann_date")
INSTRUCTION = """你负责判断与比较，程序负责数字与格式。输入facts是唯一量化事实表。
market_view只写定性概述，不写数值、日期或任何占位符。每项说明只写短句，避免复述输入。
只有fact:开头的事实ID能写成[[fact_id]]；ev-来源ID仅进evidence_ids，不能做事实占位符。
analysis.event_ids只填本股events.record_id（event-开头），不是source_ids或ev-来源ID。
analysis.scale_fact_ids须为本股实际引用的事实；next_observation_date无来源就null。
行业统计可以在正文引用，但不能放进本股scale_fact_ids；没有本股规模依据则该数组为[]。
资金方向由程序事实展示。不要自行写“净流入”“净流出”：写“资金反证：[[对应资金事实ID]]”，不能丢弃负向事实。
每个候选必须输出一次comparisons；只用给定主要类型，证据不足不排名、不入选。
primary_type=insufficient_evidence时rank=null且final_status=unselected；其余候选连续整数排名。
其他股票即使未选也连续排名；最多三重点五观察。入选比较对象须同类型未选者。
所有说明简短，保留最强反证、失效条件、来源缺口，不声称搜索或人工核实。
正文不得自己写报价、指标读数、日期、资金流方向或数量大小比较；数量用[[fact_id]]。
固定技术名称和窗口按术语约定书写，其参数不是指标读数；不能由术语附加未给数值。
不用重复输出fact_ids，程序从正文占位符和scale_fact_ids自动汇总事实清单。
事实卡含主体、期间、值和单位；同行数量的主体不从中文推断。
行业统计只描述当时合格成员，不能证明个股受益。不要将首次采集当首次市场消息。
事件推断引用本股实际展示的event_ids和evidence_ids；标题不是正文、预增不是超预期。
例行日程、重复内容、长期背景或单纯价格异动不能作为event_update的增量事件。
只引用本股、指定比较对象及其行业的事实和来源。未知交易条件写“未知：…”；
没有盘口与逐笔数据，不推断次日可买性、封单强弱或成交保障。
next_observation_date只有实际来源日程才填，否则null。
只输出符合所给schema的数据JSON，不要输出schema定义或多余的type/properties键。
"""
INSTRUCTION = SHARED + INSTRUCTION + SEMANTIC_INSTRUCTION + SELECTION


def selection_instruction(packet):
    if packet.get("prediction_objective") != "next_session":
        return INSTRUCTION
    from quantlab.scout.nextday_contract import SELECTION as NEXT_SELECTION
    from quantlab.scout.nextday_contract import SHARED as NEXT_SHARED

    facts_only = INSTRUCTION[len(SHARED) :].split(SEMANTIC_INSTRUCTION, 1)[0]
    facts_only = facts_only.replace("入选比较对象须同类型未选者。", "比较对象须来自冻结相近集合。")
    facts_only = facts_only.replace(
        "primary_type=insufficient_evidence时rank=null且final_status=unselected；"
        "其余候选连续整数排名。",
        "not_rankable不排名不入选，rankable连续研究排名。",
    )
    encoding = (
        "facts各行按fact_columns读取；fact_dictionaries给subject_id/metric/period/unit列的索引字典。"
        "这些列为整数时按字典还原，不能当事实数值；value保持原值。"
        "technical_fact_indices是facts插入顺序从零计的技术事实索引；"
        "fact_metadata补充非空公告日和缺数状态。\n"
    )
    return (
        NEXT_SHARED
        + facts_only
        + encoding
        + OPPORTUNITY_ENCODING
        + SEMANTIC_INSTRUCTION
        + NEXT_SELECTION
    )


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


OPPORTUNITY_ENCODING = (
    "opportunity_hypotheses仅列本池主体和ID。catalog_row指向opportunity_catalog.rows；"
    "按columns取字段，-1表示字段未提供，其余整数是strings索引（包括嵌套数组），null/bool保持原值，"
    "{$number:值}才是原始数值。完整支持、反证、来源和时间均在此无损目录，"
    "不能把研究假设当已核实催化。\n"
    "candidate_text_fields列出的候选注释数组为candidate_text_dictionary索引；"
    "解码保留完整技术缺口、风险和召回路线，绝不能当事实数值。\n"
)


def compact_candidate_annotations(packet):
    fields = ["cautions", "technical_gaps", "recall_routes", "source_gaps", "type_hints"]
    strings = sorted({s for c in packet["candidates"] for k in fields for s in c.get(k, [])})
    indices = {s: i for i, s in enumerate(strings)}
    for candidate in packet["candidates"]:
        for key in fields:
            if key in candidate:
                candidate[key] = [indices[s] for s in candidate[key]]
    packet["candidate_text_fields"] = fields
    packet["candidate_text_dictionary"] = strings


def unpack_candidate_annotations(packet):
    candidates = deepcopy(packet["candidates"])
    for candidate in candidates:
        for key in packet.get("candidate_text_fields", []):
            if key in candidate:
                candidate[key] = [packet["candidate_text_dictionary"][i] for i in candidate[key]]
    return candidates


def compact_opportunity_catalog(packet):
    """Losslessly encode repeated hypothesis metadata, independently of quantitative facts."""
    rows = packet.get("opportunity_hypotheses", [])
    if not rows:
        return
    columns = sorted(set().union(*(r.keys() for r in rows)) - {"hypothesis_id", "instrument_ids"})
    strings, indices = [], {}

    def encode(value):
        if isinstance(value, str):
            if value not in indices:
                indices[value] = len(strings)
                strings.append(value)
            return indices[value]
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return {"$number": value}
        if isinstance(value, list):
            return [encode(v) for v in value]
        if isinstance(value, dict):
            return {k: encode(v) for k, v in value.items()}
        raise ValueError("Unsupported opportunity metadata")

    metadata = []
    descriptors = []
    for index, row in enumerate(rows):
        # Absent fields must remain absent rather than become explicit null.
        metadata.append([encode(row[k]) if k in row else -1 for k in columns])
        descriptors.append(
            {
                "hypothesis_id": row["hypothesis_id"],
                "instrument_ids": row["instrument_ids"],
                "catalog_row": index,
            }
        )
    packet["opportunity_hypotheses"] = descriptors
    packet["opportunity_catalog"] = {
        "encoding": "text_dictionary_v1",
        "absent": -1,
        "columns": columns,
        "strings": strings,
        "rows": metadata,
    }


def unpack_opportunity_catalog(packet):
    """Restore scoped provenance for audits without changing the model packet."""
    catalog = packet.get("opportunity_catalog")
    if not catalog:
        return deepcopy(packet.get("opportunity_hypotheses", []))

    def decode(value):
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return catalog["strings"][value]
        if isinstance(value, list):
            return [decode(v) for v in value]
        if isinstance(value, dict):
            if "$number" in value:
                return value["$number"]
            return {k: decode(v) for k, v in value.items()}
        raise ValueError("Invalid opportunity catalog cell")

    result = []
    for descriptor in packet.get("opportunity_hypotheses", []):
        cells = catalog["rows"][descriptor["catalog_row"]]
        row = {k: deepcopy(descriptor[k]) for k in ("hypothesis_id", "instrument_ids")}
        for key, value in zip(catalog["columns"], cells, strict=True):
            if value != -1 or isinstance(value, bool):
                row[key] = decode(value)
        result.append(row)
    return result


def clean_paths(value):
    """Remove provenance paths from model input; full provenance remains in the report."""
    if isinstance(value, dict):
        return {
            k: clean_paths(v)
            for k, v in value.items()
            if k not in {"snapshot_refs", "snapshot_path", "local_path", "context"}
        }
    if isinstance(value, list):
        return [clean_paths(v) for v in value]
    return value


def fact_table(candidates, market, background):
    facts = {}
    for candidate in candidates:
        for fact in candidate.get("program_facts", program_facts(candidate, market["session"])):
            item = dict(fact)
            facts[item["fact_id"]] = item
        summary = candidate.get("source_summary") or {}
        # Financial source fields stay in their actual reported period. Unknown units
        # are not silently promoted to CNY, ratios, or a favorable economic meaning.
        for index, row in enumerate(summary.get("fina_indicator", [])):
            for metric, value in row.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                identity = f"fact:{candidate['instrument_id']}:financial:{index}:{metric}"
                facts[identity] = {
                    "fact_id": identity,
                    "subject_id": candidate["instrument_id"],
                    "metric": metric,
                    "period": str(row.get("end_date") or "unknown"),
                    "asof_session": market["session"],
                    "value": str(value),
                    "unit": "provider_unit_unknown",
                    "source_ann_date": row.get("ann_date"),
                }
    for industry, values in background.get("industries", {}).items():
        subject = "industry:" + industry
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            period = next((f"{d}d" for d in (1, 5, 20) if f"{d}d" in key), "asof")
            unit = "count" if "count" in key else "ratio"
            identity = f"fact:{subject}:{key}"
            facts[identity] = {
                "fact_id": identity,
                "subject_id": subject,
                "metric": key,
                "period": period,
                "asof_session": market["session"],
                "value": str(value),
                "unit": unit,
            }
    return facts


def research_packet(packet):
    """Keep evidence once, retain omissions explicitly, avoid full-universe diagnostics."""
    nextday = packet.get("prediction_objective") == "next_session"
    candidates = []
    for row in packet["candidates"]:
        record = row.get("opportunity_record", {})
        events = []
        for event in record.get("events", []):
            events.append(
                {
                    k: clean_paths(event[k])
                    for k in (
                        "record_id",
                        "source_ids",
                        "novelty",
                        "change",
                        "title_only",
                        "source_date",
                        "publication_precision",
                        "relation",
                        "nominal_scale",
                        "previous_record_id",
                        "previous_content_excerpt",
                        "official_source",
                        "published_at",
                        "source_published_at",
                        "first_seen_at",
                        "collected_at",
                        "effective_date",
                        "available_by_cutoff",
                        "visibility_basis",
                        "republication_cluster",
                        "next_session_timely",
                    )
                    if k in event
                }
            )
        candidates.append(
            {
                "instrument_id": row["instrument_id"],
                "name": row.get("name"),
                "recall_routes": row.get("recall_routes", []),
                "cautions": row.get("cautions", []),
                "events": events,
                "type_hints": record.get("type_hints", []),
                "pullback_qualified": record.get("pullback_qualified"),
                "industry": (row.get("source_summary") or {}).get("industry"),
                "omitted_event_ids": record.get("omitted_event_ids", []),
                "source_gaps": (row.get("source_summary") or {}).get("gaps", []),
                "trading_status": (row.get("source_summary") or {}).get("trading_status"),
                "source_summary": {
                    k: (row.get("source_summary") or {}).get(k)
                    for k in ("themes", "trading_status")
                },
                **(
                    {
                        "technical_state": (
                            row.get("technical_snapshot")
                            or row.get("context", {}).get("technical_snapshot", {})
                        ).get("technical_state", "unknown"),
                        "technical_gaps": [
                            k
                            for k, v in (
                                row.get("technical_snapshot")
                                or row.get("context", {}).get("technical_snapshot", {})
                            )
                            .get("metrics", {})
                            .items()
                            if v.get("status") != "available"
                        ],
                    }
                    if nextday
                    else {}
                ),
            }
        )
    # Do not concatenate path-bearing per-query diagnostics. Preserve every status/count.
    coverage = {}
    for row in packet.get("coverage", []):
        key = (row["source"], row["status"])
        item = coverage.setdefault(
            key, {"source": key[0], "status": key[1], "count": 0, "query_count": 0}
        )
        item["count"] += row.get("count", 0)
        item["query_count"] += 1
    market = {k: v for k, v in packet["market"].items() if k != "rejected_by_code"}
    background = packet.get("industry_background", {})
    subjects = {c["industry"] for c in candidates if c["industry"]}
    scoped_background = {
        "industries": {k: v for k, v in background.get("industries", {}).items() if k in subjects}
    }
    facts = fact_table(packet["candidates"], market, scoped_background)
    unknown_unit_counts = {}
    if nextday:
        for identity, fact in list(facts.items()):
            if fact["unit"] == "provider_unit_unknown":
                subject = fact["subject_id"]
                unknown_unit_counts[subject] = unknown_unit_counts.get(subject, 0) + 1
                del facts[identity]
    notes = {}
    shown_ids = {e["evidence_id"] for e in packet.get("evidence", [])}
    for hypothesis in packet.get("hypotheses", []):
        key = (hypothesis.get("summary", ""), hypothesis.get("counterargument", ""))
        note = notes.setdefault(
            key,
            {
                "summary": key[0],
                "counterargument": key[1],
                "instrument_ids": [],
                "evidence_ids": [],
                "omitted_source_ids": [],
            },
        )
        for field in ("instrument_ids", "evidence_ids"):
            for value in hypothesis.get(field, []):
                destination = (
                    "omitted_source_ids"
                    if field == "evidence_ids" and value not in shown_ids
                    else field
                )
                if value not in note[destination]:
                    note[destination].append(value)
    result = {
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "research_rules": RULES,
        "timing": packet["timing"],
        "market": market,
        "candidates": candidates,
        "fact_columns": list(FACT_COLUMNS),
        "facts": {key: [item.get(k) for k in FACT_COLUMNS] for key, item in facts.items()},
        "evidence": clean_paths(packet.get("evidence", [])),
        "coverage": list(coverage.values()),
        "research_notes": list(notes.values()),
        "coverage_limits": [
            "来源为有界抽样，空结果不证明无风险",
            "未知发布时间不得倒填",
            "行业为当前合格成员统计，非历史全市场",
            "省略ID不能引用",
        ],
        "price_reactions": packet.get("price_reactions", []),
        "investigation_opportunities": clean_paths(packet.get("investigation_opportunities", [])),
    }
    if nextday:
        from quantlab.scout.nextday_contract import RULES as NEXT_RULES
        from quantlab.scout.nextday_contract import SCHEMA_VERSION as NEXT_SCHEMA
        from quantlab.scout.nextday_contract import VERSION as NEXT_VERSION

        result.update(
            version=NEXT_VERSION,
            schema_version=NEXT_SCHEMA,
            prediction_objective="next_session",
            research_rules=NEXT_RULES,
            experiment_profile=packet.get("experiment_profile", "fused"),
            omitted_unknown_unit_facts=unknown_unit_counts,
        )
        # Metadata can be inferred from the declared fact family/version; don't
        # repeat the 120-session dependencies on every number in model input.
        result["technical_fact_refs"] = [
            k for k, f in facts.items() if f.get("calculation_version")
        ]
        result["technical_version"] = next(
            (f["calculation_version"] for f in facts.values() if f.get("calculation_version")), None
        )
        result["fact_metadata"] = {
            k: {"status": f.get("status", "unknown")}
            for k, f in facts.items()
            if f.get("value") is None
        }
        result["benchmark_definitions"] = {
            "relative_return": (
                "individual_minus_same_period_mean_of_frozen_eligible_industry_members"
            ),
            "technical_prices": "D0_factor_anchor_adjusted_research_prices_not_executable_prices",
            "data_units": "amount_CNY_volume_shares_turnover_ratio_circ_mv_CNY",
        }
        scoped_codes = {c["instrument_id"] for c in candidates}
        result["opportunity_hypotheses"] = [
            {
                **clean_paths(deepcopy(h)),
                "instrument_ids": sorted(scoped_codes & set(h.get("instrument_ids", []))),
                **(
                    {
                        "research_priority_instrument_ids": sorted(
                            scoped_codes & set(h["research_priority_instrument_ids"])
                        )
                    }
                    if "research_priority_instrument_ids" in h
                    else {}
                ),
            }
            for h in packet.get("opportunity_hypotheses", [])
            if scoped_codes & set(h.get("instrument_ids", []))
        ]
        # Comparator set is frozen from industry/type overlap and score proximity,
        # before final model ranking; an equally strong selected peer is allowed.
        for candidate in candidates:
            peers = [
                c
                for c in candidates
                if c["instrument_id"] != candidate["instrument_id"]
                and (
                    (candidate["industry"] and c["industry"] == candidate["industry"])
                    or set(c["source_summary"].get("themes") or [])
                    & set(candidate["source_summary"].get("themes") or [])
                    or any(
                        {c["instrument_id"], candidate["instrument_id"]}
                        <= set(h.get("instrument_ids", []))
                        for h in result["opportunity_hypotheses"]
                    )
                )
            ]
            candidate["comparable_ids"] = [c["instrument_id"] for c in peers]
        profile = result["experiment_profile"]
        if profile == "event_only":
            result["facts"] = {
                k: v
                for k, v in result["facts"].items()
                if ":technical:" not in k and ":market:" not in k
            }
            for candidate in candidates:
                candidate["technical_state"] = "unknown"
                candidate["technical_gaps"] = ["disabled_by_fixed_event_only_profile"]
        elif profile == "technical_only":
            result["facts"] = {
                k: v
                for k, v in result["facts"].items()
                if ":technical:" in k or ":market:" in k or v[0].startswith("industry:")
            }
            result["evidence"] = []
            result["opportunity_hypotheses"] = []
            result["research_notes"] = []
            result["investigation_opportunities"] = []
            for candidate in candidates:
                candidate["events"] = []
                candidate["type_hints"] = [
                    t for t in candidate["type_hints"] if t != "event_update"
                ]
                candidate["source_summary"]["themes"] = []
        result["technical_fact_refs"] = [
            k for k in result["technical_fact_refs"] if k in result["facts"]
        ]
        result["fact_metadata"] = {
            k: v for k, v in result["fact_metadata"].items() if k in result["facts"]
        }
    return result


def unpack_facts(packet):
    if packet["fact_columns"] not in (list(FACT_COLUMNS), list(FACT_COLUMNS[:-1])):
        raise ValueError("Unknown daily fact table columns")
    technical = set(packet.get("technical_fact_refs", []))
    indices = set(packet.get("technical_fact_indices", []))
    technical.update(ref for index, ref in enumerate(packet["facts"]) if index in indices)
    return {
        ref: {
            "fact_id": ref,
            "asof_session": packet["market"]["session"],
            **{
                key: packet.get("fact_dictionaries", {}).get(key, [])[value]
                if key in packet.get("fact_dictionaries", {}) and isinstance(value, int)
                else value
                for key, value in zip(packet["fact_columns"], values, strict=True)
            },
            **packet.get("fact_metadata", {}).get(ref, {}),
            **(
                {"calculation_version": packet.get("technical_version"), "status": "available"}
                if ref in technical and values[3] is not None
                else {}
            ),
        }
        for ref, values in packet["facts"].items()
    }


def compact_fact_refs(packet):
    """Short stable machine references; never rewrite provider body or prior excerpts."""
    result = deepcopy(packet)
    if result.get("fact_reference_style") == "sha256_12":
        return result, {}
    aliases = {key: "fact:" + sha256(key.encode()).hexdigest()[:12] for key in packet["facts"]}
    if len(set(aliases.values())) != len(aliases):
        raise ValueError("daily_fact_reference_collision")
    result["facts"] = {aliases[key]: value for key, value in result["facts"].items()}
    if "fact_metadata" in result:
        result["fact_metadata"] = {
            aliases[key]: value for key, value in result["fact_metadata"].items()
        }
    if "technical_fact_refs" in result:
        result["technical_fact_refs"] = [aliases[key] for key in result["technical_fact_refs"]]
    for key in ("research_notes", "investigation_opportunities", "price_reactions"):
        result[key] = remap_generated_refs(result.get(key, []), aliases)
    result["fact_reference_style"] = "sha256_12"
    result["version"] = packet.get("version", VERSION)
    if result.get("prediction_objective") == "next_session":
        dictionaries = {
            key: sorted({values[index] for values in result["facts"].values()})
            for index, key in enumerate(FACT_COLUMNS)
            if key in {"subject_id", "metric", "period", "unit"}
        }
        for index, key in enumerate(FACT_COLUMNS):
            if key in dictionaries:
                lookup = {value: i for i, value in enumerate(dictionaries[key])}
                for values in result["facts"].values():
                    values[index] = lookup[values[index]]
        result["fact_encoding"] = "typed_dictionary_v1"
        result["fact_dictionaries"] = dictionaries
        technical = set(result.pop("technical_fact_refs", []))
        result["technical_fact_indices"] = [
            i for i, ref in enumerate(result["facts"]) if ref in technical
        ]
        for ref, values in result["facts"].items():
            ann_date = values.pop()
            if ann_date is not None:
                result.setdefault("fact_metadata", {}).setdefault(ref, {})["source_ann_date"] = (
                    ann_date
                )
        result["fact_columns"] = list(FACT_COLUMNS[:-1])
        result["opportunity_hypotheses"] = remap_generated_refs(
            result.get("opportunity_hypotheses", []), aliases
        )
        compact_opportunity_catalog(result)
        compact_candidate_annotations(result)
    return result, aliases


def remap_generated_refs(value, aliases):
    if isinstance(value, dict):
        return {
            k: v if k in {"body", "previous_content_excerpt"} else remap_generated_refs(v, aliases)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [remap_generated_refs(v, aliases) for v in value]
    if isinstance(value, str):
        return aliases.get(value, REF.sub(lambda m: "[[" + aliases.get(m[1], m[1]) + "]]", value))
    return value


def referenced_facts(row):
    texts = [row[f] for f in FIELDS] + [row["analysis"][f] for f in analysis_fields(row)]
    texts += [row.get(k, "") for k in ("technical_interpretation", "price_reaction")]
    texts += list(row["trade_conditions"].values())
    condition = row.get("next_session_condition")
    if isinstance(condition, dict) and isinstance(condition.get("mechanism"), str):
        texts.append(condition["mechanism"])
    claims = row.get("semantic_claims", [])
    if not Draft202012Validator(CLAIM_SCHEMA).is_valid(claims):
        claims = []
    rule = row.get("invalidation_rule")
    if not isinstance(rule, dict):
        rule = None
    return list(
        dict.fromkeys(
            row.get("fact_ids", [])
            + row.get("technical_fact_ids", [])
            + (row.get("next_session_condition") or {}).get("fact_ids", [])
            + row["analysis"]["scale_fact_ids"]
            + [ref for claim in claims for ref in claim["fact_ids"]]
            + (
                [rule["reference_id"]]
                if rule
                and isinstance(rule.get("reference_id"), str)
                and rule["reference_id"].startswith("fact:")
                else []
            )
            + [ref for text in texts for ref in REF.findall(text)]
        )
    )


def analysis_fields(row):
    return tuple(
        "next_session_thesis"
        if f == "h5_mechanism" and "next_session_thesis" in row["analysis"]
        else f
        for f in ANALYSIS_FIELDS
    )


def selection_schema(candidates, packet=None):
    schema = deepcopy(OPPORTUNITY_SCHEMA)
    schema["$id"] = "urn:quantlab:" + SCHEMA_VERSION + ":selection"
    schema["properties"]["market_view"] = {"type": "string", "minLength": 1, "maxLength": 400}
    rows = schema["properties"]["comparisons"]
    rows.update(minItems=len(candidates), maxItems=len(candidates))
    props = rows["items"]["properties"]
    props["semantic_claims"] = deepcopy(CLAIM_SCHEMA)
    props["invalidation_rule"] = deepcopy(RULE_SCHEMA)
    rows["items"]["required"] += ["semantic_claims", "invalidation_rule"]
    props["instrument_id"] = {
        "type": "string",
        "enum": [row["instrument_id"] for row in candidates],
    }
    for key in FIELDS:
        props[key] = {"type": "string", "minLength": 1, "maxLength": 120}
    for key in ANALYSIS_FIELDS:
        props["analysis"]["properties"][key] = {"type": "string", "minLength": 1, "maxLength": 80}
    for field in ("known", "unknown"):
        props["trade_conditions"]["properties"][field] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
        }
    props["fact_ids"] = {
        "type": "array",
        "maxItems": 16,
        "uniqueItems": True,
        "items": {"type": "string", "pattern": "^fact:"},
    }
    props["evidence_ids"] = {
        "type": "array",
        "maxItems": 5,
        "uniqueItems": True,
        "items": {"type": "string"},
    }
    # References replace model-authored unit conversions and redundant declarations.
    props.pop("quant_claims")
    rows["items"]["required"].remove("quant_claims")
    rows["items"]["required"].remove("fact_ids")
    rows["items"]["allOf"] = [
        {
            "if": {"properties": {"primary_type": {"const": "insufficient_evidence"}}},
            "then": {
                "properties": {"rank": {"type": "null"}, "final_status": {"const": "unselected"}}
            },
            "else": {"properties": {"rank": {"type": "integer", "minimum": 1}}},
        }
    ]
    if packet is not None:
        for candidate in candidates:
            code = candidate["instrument_id"]
            own = [
                key
                for key, values in packet["facts"].items()
                if values[0] == code and values[4] != "provider_unit_unknown"
            ]
            rows["items"]["allOf"].append(
                {
                    "if": {"properties": {"instrument_id": {"const": code}}},
                    "then": {
                        "properties": {
                            "analysis": {
                                "properties": {
                                    "scale_fact_ids": {"items": {"enum": own}}
                                    if own
                                    else {"maxItems": 0}
                                }
                            }
                        }
                    },
                }
            )
    if packet and packet.get("prediction_objective") == "next_session":
        from quantlab.scout.nextday_contract import adapt_schema

        schema = adapt_schema(schema)
    return schema


def validate_output(output, packet):
    """Collect the complete schema and semantic error list without stopping at row one."""
    nextday = packet.get("prediction_objective") == "next_session"
    if nextday:
        from quantlab.scout.nextday_contract import (
            RESEARCH_RULES,
            focus_errors,
            rule_errors,
            rule_schema,
        )

        active_rule_schema = rule_schema(RESEARCH_RULES)
    else:
        from quantlab.scout.decision_contract import focus_errors, rule_errors

        active_rule_schema = RULE_SCHEMA
    errors = [
        {"path": list(e.path), "code": "schema", "detail": e.message[:300]}
        for e in Draft202012Validator(selection_schema(packet["candidates"], packet)).iter_errors(
            output
        )
    ]
    if not isinstance(output, dict) or not isinstance(output.get("comparisons"), list):
        return errors
    candidates = {c["instrument_id"]: c for c in packet["candidates"]}
    facts = unpack_facts(packet)
    shape = semantic_shape(
        selection_schema(packet["candidates"], packet)["properties"]["comparisons"]["items"]
    )
    shape["properties"]["instrument_id"]["enum"] = list(candidates)
    # New fields must not hide all other errors in saved legacy responses.
    optional_shape = ["semantic_claims", "invalidation_rule"]
    if nextday:
        optional_shape += [
            "next_session_condition",
            "technical_fact_ids",
            "participation_cancel_rule",
            "opportunity_ids",
            "ranking_state",
            "participation_status",
            "technical_interpretation",
            "price_reaction",
        ]
    for field in optional_shape:
        shape["properties"].pop(field, None)
        shape["required"].remove(field)
    shape["additionalProperties"] = True
    rows = [r for r in output["comparisons"] if Draft202012Validator(shape).is_valid(r)]
    if nextday:
        # Schema errors retain the original path. A safe semantic projection of
        # malformed new fields lets us collect independent body/reference errors.
        rows = [deepcopy(row) for row in rows]
        new_properties = selection_schema(packet["candidates"], packet)["properties"][
            "comparisons"
        ]["items"]["properties"]
        for row in rows:
            for key in optional_shape[2:]:
                if not Draft202012Validator(new_properties[key]).is_valid(row.get(key)):
                    defaults = {
                        "technical_fact_ids": [],
                        "opportunity_ids": [],
                        "ranking_state": "not_rankable" if row["rank"] is None else "rankable",
                        "participation_status": "observe_only",
                        "technical_interpretation": "未知",
                        "price_reaction": "未知",
                    }
                    row[key] = defaults.get(key)
    by_code = {r["instrument_id"]: r for r in rows}

    def error(code, field, detail, **context):
        errors.append({"path": [code, field], "code": detail, **context})

    if len(by_code) != len(candidates):
        error("all", "comparisons", "duplicate_or_missing_subject")
    ranks = [r["rank"] for r in rows if r["rank"] is not None]
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        error("all", "rank", "ranks_not_unique_contiguous")
    for status, maximum in (("focus", 3), ("watch", 5)):
        if sum(r["final_status"] == status for r in rows) > maximum:
            error("all", status, "display_cap_exceeded")
    for row in rows:
        code = row["instrument_id"]
        peer = row["comparator_id"]
        insufficient = row["primary_type"] == "insufficient_evidence"
        cannot_rank = row.get("ranking_state") == "not_rankable" if nextday else insufficient
        if (
            nextday
            and (insufficient or row["evidence_reliability"] == "insufficient")
            and not cannot_rank
        ):
            error(code, "ranking_state", "insufficient_requires_not_rankable")
        if cannot_rank != (row["rank"] is None) or (
            cannot_rank and row["final_status"] != "unselected"
        ):
            error(code, "rank", "insufficient_cannot_rank_or_select")
        if row["primary_type"] not in row["type_labels"]:
            error(code, "type_labels", "primary_type_not_in_labels")
        comparators = {
            r["instrument_id"]
            for r in rows
            if r["instrument_id"] != code
            and r["primary_type"] == row["primary_type"]
            and (nextday or r["final_status"] == "unselected")
        }
        if nextday:
            comparators = set(candidates[code].get("comparable_ids", []))
        if peer is not None and (peer not in by_code or peer == code):
            error(code, "comparator_id", "comparator_not_shown")
        if row["final_status"] != "unselected" and comparators and peer not in comparators:
            error(
                code,
                "comparator_id",
                "frozen_comparable_required" if nextday else "same_type_unselected_required",
                allowed_comparator_ids=sorted(comparators),
                actual=peer,
            )
        allowed = {code, peer}
        for c in candidates.values():
            if c["instrument_id"] in allowed:
                industry = c.get("industry")
                if industry:
                    allowed.add("industry:" + industry)
        used_facts = referenced_facts(row)
        if Draft202012Validator(CLAIM_SCHEMA).is_valid(row.get("semantic_claims", [])):
            for issue in claim_errors(row.get("semantic_claims", []), facts, allowed):
                error(code, "semantic_claims", issue["code"], claim_index=issue["index"])
        for ref in used_facts:
            if ref not in facts or facts[ref]["subject_id"] not in allowed:
                error(
                    code,
                    "fact_ids",
                    "unknown_or_wrong_subject_fact:" + ref,
                    actual=ref,
                    allowed_fact_ids=[
                        key for key, f in facts.items() if f["subject_id"] in allowed
                    ],
                )
            elif facts[ref].get("value") is None:
                error(code, "fact_ids", "null_fact_cannot_support_quantitative_claim:" + ref)
        for ref in row["analysis"]["scale_fact_ids"]:
            if ref not in facts or facts[ref]["subject_id"] != code:
                error(code, "scale_fact_ids", "scale_requires_declared_own_fact:" + ref)
            elif facts[ref]["unit"] == "provider_unit_unknown":
                error(code, "scale_fact_ids", "scale_unit_unverified:" + ref)
        own_events = {e["record_id"]: e for e in candidates[code]["events"]}
        rule = row.get("invalidation_rule")
        if Draft202012Validator(active_rule_schema).is_valid(rule):
            for issue in rule_errors(
                rule,
                code,
                row["primary_type"],
                facts,
                own_events,
                **({"focus": row["final_status"] == "focus"} if nextday else {}),
            ):
                error(code, "invalidation_rule", issue)
        safe_rule = rule if Draft202012Validator(active_rule_schema).is_valid(rule) else None
        for field, issue in focus_errors(
            {**row, "invalidation_rule": safe_rule},
            facts,
            own_events,
            **({"candidate": candidates[code]} if nextday else {}),
        ):
            error(code, field, issue)
        if nextday:
            from quantlab.scout.nextday_contract import CANCEL_RULES

            cancel = row.get("participation_cancel_rule")
            if Draft202012Validator(rule_schema(CANCEL_RULES)).is_valid(cancel):
                for issue in rule_errors(
                    cancel, code, row["primary_type"], facts, own_events, cancellation=True
                ):
                    error(code, "participation_cancel_rule", issue)
            for ref in row.get("technical_fact_ids", []):
                fact = facts.get(ref, {})
                if fact.get("subject_id") != code or not fact.get("calculation_version"):
                    error(
                        code, "technical_fact_ids", "technical_requires_own_calculated_fact:" + ref
                    )
            opportunities = {
                h["hypothesis_id"]: h
                for h in packet.get("opportunity_hypotheses", [])
                if "hypothesis_id" in h
            }
            for identity in row.get("opportunity_ids", []):
                if identity not in opportunities or code not in opportunities[identity].get(
                    "instrument_ids", []
                ):
                    error(code, "opportunity_ids", "opportunity_not_shown_for_subject:" + identity)
            trading_status = candidates[code].get("trading_status")
            trading_status = (
                trading_status.get("status") if isinstance(trading_status, dict) else trading_status
            )
            if (
                row.get("participation_status") != "restricted"
                and trading_status == "hold_for_official_notice_review"
            ):
                error(code, "participation_status", "known_halt_requires_restricted")
        for field, issue in legacy_errors(row, candidates[code], packet["evidence"]):
            error(code, field, issue)
        for event_id in row["analysis"]["event_ids"]:
            if event_id not in own_events:
                error(code, "event_ids", "event_not_shown_for_subject:" + event_id)
        if row["primary_type"] == "event_update" and not row["analysis"]["event_ids"]:
            error(code, "event_ids", "event_identity_required")
        analysis = row["analysis"]
        selected_events = [own_events[e] for e in analysis["event_ids"] if e in own_events]
        if row["primary_type"] == "event_update" and analysis["novelty"] in {
            "routine_schedule",
            "long_term_background",
            "repeated_content",
            "price_only",
        }:
            error(code, "novelty", "event_requires_increment")
        if analysis["novelty"] in {"new_event", "material_update"} and (
            not selected_events
            or all(
                e.get("novelty") in {"routine_schedule", "long_term_background", "repeated_content"}
                for e in selected_events
            )
        ):
            error(code, "novelty", "old_or_routine_cannot_be_new")
        if analysis["novelty"] == "material_update" and not any(
            e.get("previous_record_id") for e in selected_events
        ):
            error(code, "novelty", "update_without_prior")
        if row["primary_type"] == "event_update" and row["final_status"] == "focus":
            selected_events = [
                own_events[e] for e in row["analysis"]["event_ids"] if e in own_events
            ]
            if not any(not e.get("title_only", True) for e in selected_events):
                error(code, "event_ids", "focus_event_requires_shown_body")
            if (
                analysis["novelty"] not in {"new_event", "material_update"}
                or analysis["exposure"] == "unknown"
            ):
                error(code, "novelty", "focus_event_requires_increment_and_exposure")
        evidence = {e["evidence_id"]: e for e in packet["evidence"]}
        for ref in row["evidence_ids"]:
            if ref == f"market:{code}":
                continue
            e = evidence.get(ref)
            if e is None or (e.get("instrument_ids") and not set(e["instrument_ids"]) <= allowed):
                error(code, "evidence_ids", "source_not_shown_or_wrong_subject:" + ref)
        fields = {
            **{f: row[f] for f in FIELDS},
            **{f: row["analysis"][f] for f in analysis_fields(row)},
            **(
                {k: row[k] for k in ("technical_interpretation", "price_reaction")}
                if nextday
                else {}
            ),
            "trade_known": row["trade_conditions"]["known"],
            "trade_unknown": row["trade_conditions"]["unknown"],
        }
        if nextday and row.get("next_session_condition"):
            fields["condition_mechanism"] = row["next_session_condition"]["mechanism"]
        for field, text in fields.items():
            for issue in prose_errors(text, facts, (code, peer)):
                error(code, field, issue, actual=text)
            prose = REF.sub("事实", text)
            prose = re.sub(
                r"(?<![A-Za-z0-9])[HD](?:1|2|3|5|10)(?![A-Za-z0-9])", "固定观察期限", prose
            )
            prose = without_technical_labels(prose)
            for subject in (code, peer):
                if subject:
                    prose = prose.replace(subject, "证券")
                    # Exchange suffix is optional in prose, but only this row's
                    # own/declared peer ID is an identity, never an arbitrary number.
                    prose = re.sub(
                        r"(?<!\d)"
                        + re.escape(subject.split(".")[0])
                        + r"(?![\d.%元万亿倍手笔天日个])",
                        "证券",
                        prose,
                    )
            if re.search(r"\d|净流[入出]", prose):
                error(code, field, "quantitative_prose_requires_fact_placeholder", actual=text)
            if daily_microstructure_assertion(prose, field):
                error(code, field, "unsupported_microstructure_assertion", actual=text)
        node = row["analysis"]["next_observation_date"]
        if node:
            try:
                date.fromisoformat(node)
            except ValueError:
                error(code, "next_observation_date", "invalid_calendar_date")
            source = " ".join(
                e.get("title", "") + " " + e["body"]
                for e in packet["evidence"]
                if code in e.get("instrument_ids", [])
            )
            if node not in source and node.replace("-", "") not in source:
                error(code, "next_observation_date", "node_not_in_shown_source")
    if isinstance(output.get("market_view"), str):
        if re.search(r"\d|\[\[", without_technical_labels(output["market_view"])):
            error("all", "market_view", "market_view_qualitative_only")
    return errors


def semantic_shape(schema):
    """Check safe field types, not constraints that would hide the row's other errors."""
    if isinstance(schema, list):
        return [semantic_shape(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    return {
        key: semantic_shape(value)
        for key, value in schema.items()
        if key
        not in {
            "allOf",
            "enum",
            "const",
            "pattern",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "uniqueItems",
            "minimum",
            "maximum",
            "additionalProperties",
        }
    }


def daily_microstructure_assertion(text, field):
    # Typed unknown fields list missing dimensions, rather than claiming their values.
    # Keep an explicit positive-assertion guard even under an "unknown" heading.
    if field in {"unknowns", "trade_unknown"}:
        return bool(
            re.search(
                r"封单(?:较|很|特别)?强(?!弱)|(?:可以|能够|保证|必然|容易).{0,4}(?:成交|买到)"
                r"|(?:承接|流动性)(?:强|充足)",
                text,
            )
        )
    return asserts_unsupported_microstructure(text)


def format_fact(fact):
    if fact.get("value") is None:
        return f"{fact['subject_id']} · {fact['period']} · {fact['metric']}：未知/数据不足"
    value = Decimal(fact["value"])
    if fact["unit"] == "ratio":
        number = f"{value * 100:+.2f}%"
    elif fact["unit"] == "CNY":
        number = f"{value / Decimal(100000000):.2f}亿元"
    elif fact["unit"] == "wan_CNY":
        number = f"{value / Decimal(10000):+.2f}亿元"
    else:
        number = (
            f"{value}（原字段单位待核实）"
            if fact["unit"] == "provider_unit_unknown"
            else f"{value:.2f} {fact['unit']}"
        )
    result = f"{fact['subject_id']} · {fact['period']} · {fact['metric']}：{number}"
    if fact["metric"].startswith("relative_return_"):
        result += "（相对本次合格行业成员均值，非指数及历史完整行业）"
    if fact["metric"].startswith("net_"):
        result += "（供应商主动买卖净额，累计窗口可能重叠，不代表机构账户）"
    return result


def render_output(output, packet):
    """Never alter the archived model output; render an independently hashed view."""
    result = deepcopy(output)
    facts = unpack_facts(packet)
    for row in result["comparisons"]:
        row["fact_ids"] = referenced_facts(row)

        def render(text):
            return REF.sub(lambda m: format_fact(facts[m[1]]), text)

        for field in FIELDS:
            row[field] = render(row[field])
        for field in analysis_fields(row):
            row["analysis"][field] = render(row["analysis"][field])
        for field in ("known", "unknown"):
            row["trade_conditions"][field] = render(row["trade_conditions"][field])
        row["quant_claims"] = []  # Legacy field: daily validation uses explicit references.
        row["fact_cards"] = [facts[ref] for ref in row["fact_ids"]]
        row["semantic_summary"] = [
            render_claim(claim, facts, format_fact) for claim in row.get("semantic_claims", [])
        ]
        if packet.get("prediction_objective") == "next_session":
            from quantlab.scout.nextday_contract import current_rule_state
            from quantlab.scout.nextday_contract import rule_text as next_rule_text

            row["invalidation_observation"] = next_rule_text(row.get("invalidation_rule"))
            row["invalidation_state_at_cutoff"] = current_rule_state(
                row.get("invalidation_rule"), facts
            )
            row["participation_cancel_observation"] = next_rule_text(
                row.get("participation_cancel_rule")
            )
            for field in ("technical_interpretation", "price_reaction"):
                row[field] = render(row[field])
            if row.get("next_session_condition"):
                row["next_session_condition"]["mechanism"] = render(
                    row["next_session_condition"]["mechanism"]
                )
        else:
            row["invalidation_observation"] = rule_text(row.get("invalidation_rule"))
    result["render_version"] = packet.get("version", VERSION)
    result["raw_output_sha256"] = fingerprint(output)
    return result
