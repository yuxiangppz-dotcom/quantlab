"""Dedicated research input and explicit fact references, separate from legacy prose parsing."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal

from jsonschema import Draft202012Validator

from quantlab.scout.ai import asserts_unsupported_microstructure
from quantlab.scout.facts import program_facts
from quantlab.scout.models import fingerprint
from quantlab.scout.opportunity_ai import OPPORTUNITY_SCHEMA

VERSION = "daily_facts_v1"
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
每个候选必须输出一次comparisons；只用给定主要类型，证据不足不排名、不入选。
其他股票即使未选也连续排名；最多三重点五观察。入选比较对象须同类型未选者。
所有说明简短，保留最强反证、失效条件、来源缺口，不声称搜索或人工核实。
正文不得自己写任何数字、日期、资金流方向或数量大小比较；需要数量时使用[[fact_id]]。
fact_ids列出实际用于判断的事实。事实卡含主体、期间、值和单位；同行数量的主体不从中文推断。
行业统计只描述当时合格成员，不能证明个股受益。不要将首次采集当首次市场消息。
事件推断引用本股实际展示的event_ids和evidence_ids；标题不是正文、预增不是超预期。
只引用本股、指定比较对象及其行业的事实和来源。未知交易条件写“未知：…”；
没有盘口与逐笔数据，不推断次日可买性、封单强弱或成交保障。
next_observation_date只有实际来源日程才填，否则null。只输出schema JSON。
"""


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


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
        for fact in program_facts(candidate, market["session"]):
            item = {
                k: fact[k]
                for k in (
                    "fact_id",
                    "subject_id",
                    "metric",
                    "period",
                    "asof_session",
                    "value",
                    "unit",
                )
            }
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
    return {
        "version": VERSION,
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


def unpack_facts(packet):
    if packet["fact_columns"] != list(FACT_COLUMNS):
        raise ValueError("Unknown daily fact table columns")
    return {
        ref: {
            "fact_id": ref,
            "asof_session": packet["market"]["session"],
            **dict(zip(FACT_COLUMNS, values, strict=True)),
        }
        for ref, values in packet["facts"].items()
    }


def selection_schema(candidates):
    schema = deepcopy(OPPORTUNITY_SCHEMA)
    schema["properties"]["market_view"] = {"type": "string", "minLength": 1, "maxLength": 400}
    rows = schema["properties"]["comparisons"]
    rows.update(minItems=len(candidates), maxItems=len(candidates))
    props = rows["items"]["properties"]
    props["instrument_id"] = {
        "type": "string",
        "enum": [row["instrument_id"] for row in candidates],
    }
    for key in FIELDS:
        props[key] = {"type": "string", "minLength": 1, "maxLength": 260}
    for key in ANALYSIS_FIELDS:
        props["analysis"]["properties"][key] = {"type": "string", "minLength": 1, "maxLength": 180}
    # References replace model-authored unit conversions and redundant declarations.
    props.pop("quant_claims")
    rows["items"]["required"].remove("quant_claims")
    return schema


def validate_output(output, packet):
    """Collect the complete schema and semantic error list without stopping at row one."""
    errors = [
        {"path": list(e.path), "code": "schema", "detail": e.message[:300]}
        for e in Draft202012Validator(selection_schema(packet["candidates"])).iter_errors(output)
    ]
    if not isinstance(output, dict) or not isinstance(output.get("comparisons"), list):
        return errors
    candidates = {c["instrument_id"]: c for c in packet["candidates"]}
    facts = unpack_facts(packet)
    shape = deepcopy(selection_schema(packet["candidates"])["properties"]["comparisons"]["items"])
    shape["additionalProperties"] = True
    rows = [r for r in output["comparisons"] if Draft202012Validator(shape).is_valid(r)]
    by_code = {r["instrument_id"]: r for r in rows}

    def error(code, field, detail):
        errors.append({"path": [code, field], "code": detail})

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
        if insufficient != (row["rank"] is None) or (
            insufficient and row["final_status"] != "unselected"
        ):
            error(code, "rank", "insufficient_cannot_rank_or_select")
        if row["primary_type"] not in row["type_labels"]:
            error(code, "type_labels", "primary_type_not_in_labels")
        comparators = {
            r["instrument_id"]
            for r in rows
            if r["instrument_id"] != code
            and r["primary_type"] == row["primary_type"]
            and r["final_status"] == "unselected"
        }
        if peer is not None and (peer not in by_code or peer == code):
            error(code, "comparator_id", "comparator_not_shown")
        if row["final_status"] != "unselected" and comparators and peer not in comparators:
            error(code, "comparator_id", "same_type_unselected_required")
        allowed = {code, peer}
        for c in candidates.values():
            if c["instrument_id"] in allowed:
                industry = c.get("industry")
                if industry:
                    allowed.add("industry:" + industry)
        for ref in row["fact_ids"]:
            if ref not in facts or facts[ref]["subject_id"] not in allowed:
                error(code, "fact_ids", "unknown_or_wrong_subject_fact:" + ref)
        for ref in row["analysis"]["scale_fact_ids"]:
            if ref not in facts or facts[ref]["subject_id"] != code or ref not in row["fact_ids"]:
                error(code, "scale_fact_ids", "scale_requires_declared_own_fact:" + ref)
        own_events = {e["record_id"]: e for e in candidates[code]["events"]}
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
            **{f: row["analysis"][f] for f in ANALYSIS_FIELDS},
            "trade_known": row["trade_conditions"]["known"],
            "trade_unknown": row["trade_conditions"]["unknown"],
        }
        for field, text in fields.items():
            refs = REF.findall(text)
            for ref in refs:
                if ref not in row["fact_ids"]:
                    error(code, field, "placeholder_not_declared:" + ref)
            prose = REF.sub("事实", text)
            prose = re.sub(r"\bH(?:1|3|5|10)\b", "固定观察期限", prose)
            if re.search(r"\d|净流[入出]|额比.{0,5}[高低]|收益.{0,5}[高低]", prose):
                error(code, field, "quantitative_prose_requires_fact_placeholder")
            if asserts_unsupported_microstructure(prose):
                error(code, field, "unsupported_microstructure_assertion")
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
        if re.search(r"\d|\[\[", output["market_view"]):
            error("all", "market_view", "market_view_qualitative_only")
    return errors


def format_fact(fact):
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
    return f"{fact['subject_id']} · {fact['period']} · {fact['metric']}：{number}"


def render_output(output, packet):
    """Never alter the archived model output; render an independently hashed view."""
    result = deepcopy(output)
    facts = unpack_facts(packet)
    for row in result["comparisons"]:

        def render(text):
            return REF.sub(lambda m: format_fact(facts[m[1]]), text)

        for field in FIELDS:
            row[field] = render(row[field])
        for field in ANALYSIS_FIELDS:
            row["analysis"][field] = render(row["analysis"][field])
        for field in ("known", "unknown"):
            row["trade_conditions"][field] = render(row["trade_conditions"][field])
        row["quant_claims"] = []  # Legacy field: daily validation uses explicit references.
        row["fact_cards"] = [facts[ref] for ref in row["fact_ids"]]
    result["render_version"] = VERSION
    result["raw_output_sha256"] = fingerprint(output)
    return result
