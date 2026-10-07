"""Small model judgments assembled into the unchanged, fully checked D1 report.

Order, grades and explanations belong to the model. Subject/metric bindings,
observation tuples, ranks and display facts belong to the program. No invalid
judgment is promoted, repaired by guesswork, or replaced with a rule ranking.
"""

import re
from collections import Counter
from copy import deepcopy
from decimal import Decimal

from jsonschema import Draft202012Validator

from quantlab.scout.daily_contract import (
    OPPORTUNITY_ENCODING,
    compact,
    unpack_facts,
    validate_output,
)
from quantlab.scout.models import fingerprint
from quantlab.scout.nextday_contract import CONDITION_OBSERVATIONS, SHARED
from quantlab.scout.opportunities import TYPE_LABELS

VERSION = "program_assembled_selection_v1"
SCHEMA_ID = "urn:quantlab:scout_next_session_schema_v2:judgments"
INSTRUCTION = (
    SHARED
    + OPPORTUNITY_ENCODING
    + """[PROGRAM_ASSEMBLED_SELECTION_V1]
facts按fact_columns读取，subject_id/metric/period/unit整数按fact_dictionaries还原，value保留原值。
technical_fact_indices按当前facts行从零索引。绝对均线/原MACD与其归一指标同源；
未用于调查的冗余绝对指标保留于审计，当前输入不提供，不能猜测或引用省略ID。
候选events数组按event_columns还原，{$not_present:true}为未提供，不等于有利假设。
condition_options每行按condition_columns读取，这是程序已经核对的完整规则绑定。
你只负责选择、顺序和定性判断，不重复输出调查analysis、排名数字、技术读数或规则对象。
comparisons按你的研究优先顺序排列，覆盖每股一次；最多三focus五watch，允许零。
primary_type及evidence_reliability不足时只能unselected；程序按数组顺序计算连续研究排名。
reason为选择依据，counterargument为最强反证，difference为与comparator_id的具体差异，
independent_basis承认纯量价/来源重叠，unknowns保留关键缺口，mechanism写目标日尚未发生的变化。
这些短句只写定性文字，不写日期、读数、股票代码或[[占位符]]。数字由独立事实引用显示。
不要将资金方向、相对收益或量比另写成已发生的断言；用support_fact_ids/counter_fact_ids。
support_fact_ids/counter_fact_ids只填本股已展示且非空事实，最多各两项，未知可以[]。
comparator_id从本股comparable_ids选，确实不可比时null；comparison_fact_pairs由程序
绑定本股/同行同metric/period/unit的事实，不需要你重新输出事实对。
focus若有同行必须有可用事实对并解释实际差异；中性事实卡不证明经济因果。
condition_id仅从本股condition_options选，与primary_type相容；程序固定观察指标及失效规则。
focus必须有条件和具体机制；“待验证/走势强/继续观察”不够。watch允许条件未知为null。
事件选项不代表事件已核实增量，仍以原调查novelty/exposure及官方正文状态约束focus。
不重写novelty，不把首次采集当新消息。调查原文、反证和缺口都在输入，必须考虑。
保留价格已扩张、减持、亏损等反证，不为通过校验隐去。无订单或可成交保证。
只输出符合schema的短JSON数据，不输出schema定义。程序附上技术位置、价格反应、
规则、原调查身份和来源，不需要你复制这些字段；这不代表已证明能赚钱。
"""
)


def _own(facts, code, metric):
    return next(
        (
            ref
            for ref, f in facts.items()
            if f["subject_id"] == code and f["metric"] == metric and f.get("value") is not None
        ),
        None,
    )


def prepare_packet(packet):
    result = deepcopy(packet)
    facts = unpack_facts(result)
    redundant = {
        "ma5",
        "ma10",
        "ma20",
        "ma60",
        "prior_high_20d",
        "volume_shares",
        "macd_dif",
        "macd_dea",
        "macd_hist2",
        "macd_hist2_change_1d",
        "atr14",
    }
    protected = set(
        re.findall(
            r"fact:[a-f0-9]{12}",
            compact(
                [
                    result.get("investigation_opportunities", []),
                    result.get("research_notes", []),
                    result.get("price_reactions", []),
                ]
            ),
        )
    )
    removed = {
        ref
        for ref, f in facts.items()
        if f.get("calculation_version") and f["metric"] in redundant and ref not in protected
    }
    result["facts"] = {ref: values for ref, values in result["facts"].items() if ref not in removed}
    result["technical_fact_indices"] = [
        i for i, ref in enumerate(result["facts"]) if facts[ref].get("calculation_version")
    ]
    result["fact_metadata"] = {
        ref: values for ref, values in result.get("fact_metadata", {}).items() if ref not in removed
    }
    result["omitted_redundant_technical_fact_counts"] = dict(
        Counter(facts[ref]["metric"] for ref in removed)
    )
    facts = {ref: f for ref, f in facts.items() if ref not in removed}
    study = {
        r["instrument_id"]: r["analysis"] for r in result.get("investigation_opportunities", [])
    }
    options = {}
    for candidate in result["candidates"]:
        code = candidate["instrument_id"]
        own_options = []
        for metric, kind, rule in (
            ("relative_return_1d", "relative_strength_extension", "relative_d1_nonpositive_v1"),
            ("close_to_ma20", "price_structure_repair", "ma20_break_v1"),
        ):
            ref = _own(facts, code, metric)
            if ref is None or Decimal(facts[ref]["value"]) <= 0:
                continue
            if metric == "close_to_ma20" and not facts[ref].get("calculation_version"):
                continue
            key = code + ":" + kind
            own_options.append(key)
            options[key] = {
                "instrument_id": code,
                "kind": kind,
                "fact_ids": [ref],
                "event_id": None,
                "rule_id": rule,
                "reference_id": ref,
            }
        refs = [
            ref
            for ref, f in facts.items()
            if f["subject_id"] == code
            and f.get("value") is not None
            and f["unit"] != "provider_unit_unknown"
        ]
        for event in candidate.get("events", []):
            if not (
                refs
                and event.get("official_source")
                and not event.get("title_only", True)
                and event.get("relation") == "direct_subject"
                and event["record_id"] in study.get(code, {}).get("event_ids", [])
            ):
                continue
            key = code + ":" + event["record_id"]
            own_options.append(key)
            options[key] = {
                "instrument_id": code,
                "kind": "official_event_progress",
                "fact_ids": refs[:1],
                "event_id": event["record_id"],
                "rule_id": "event_cancelled_d1_v1",
                "reference_id": event["record_id"],
            }
        candidate["condition_options"] = own_options
        pairs = {}
        for peer in candidate.get("comparable_ids", []):
            for metric in ("close_to_ma20", "relative_return_1d", "return_1d", "return_5d"):
                own_ref, peer_ref = _own(facts, code, metric), _own(facts, peer, metric)
                if not own_ref or not peer_ref:
                    continue
                own_fact, peer_fact = facts[own_ref], facts[peer_ref]
                if (own_fact["period"], own_fact["unit"]) != (
                    peer_fact["period"],
                    peer_fact["unit"],
                ):
                    continue
                pairs[peer] = [own_ref, peer_ref]
                break
        candidate["comparison_fact_pairs"] = pairs
    result["condition_options"] = options
    result["selection_assembly_version"] = VERSION
    return result


def schema(packet):
    def text():
        # Final narratives are qualitative; the separate ID fields carry values.
        return {"type": "string", "minLength": 1, "maxLength": 70}

    def refs():
        return {
            "type": "array",
            "maxItems": 2,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": "^fact:", "maxLength": 32},
        }

    props = {
        "instrument_id": {"enum": [c["instrument_id"] for c in packet["candidates"]]},
        "primary_type": {"enum": list(TYPE_LABELS)},
        "final_status": {"enum": ["focus", "watch", "unselected"]},
        "evidence_reliability": {
            "enum": ["program_facts", "source_requires_verification", "insufficient"]
        },
        "comparison_strength": {"enum": ["strong", "medium", "weak", "insufficient"]},
        "comparator_id": {"type": ["string", "null"], "maxLength": 16},
        "condition_id": {"type": ["string", "null"], "maxLength": 160},
        "support_fact_ids": refs(),
        "counter_fact_ids": refs(),
        **{
            k: text()
            for k in (
                "reason",
                "counterargument",
                "difference",
                "independent_basis",
                "unknowns",
                "mechanism",
            )
        },
    }
    return {
        "$id": SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["market_view", "comparisons"],
        "properties": {
            "market_view": text(),
            "comparisons": {
                "type": "array",
                "minItems": len(packet["candidates"]),
                "maxItems": len(packet["candidates"]),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(props),
                    "properties": props,
                },
            },
        },
    }


def _clauses(refs):
    return "；".join("事实：[[" + ref + "]]" for ref in refs)


def assemble(value, packet):
    """Called only after wire shape/identity checks; never edits the wire response."""
    facts = unpack_facts(packet)
    candidates = {c["instrument_id"]: c for c in packet["candidates"]}
    studies = {
        r["instrument_id"]: r["analysis"] for r in packet.get("investigation_opportunities", [])
    }
    rows, rank = [], 0
    for judgment in value["comparisons"]:
        code = judgment["instrument_id"]
        candidate, study = candidates[code], studies.get(code, {})
        option = packet["condition_options"].get(judgment["condition_id"])
        condition, rule = None, None
        if option:
            driver, window, metric, state = CONDITION_OBSERVATIONS[option["kind"]]
            condition = {
                "kind": option["kind"],
                "fact_ids": option["fact_ids"],
                "event_id": option["event_id"],
                "driver": driver,
                "mechanism": judgment["mechanism"],
                "observation": {"window": window, "metric": metric, "expected_state": state},
            }
            rule = {"rule_id": option["rule_id"], "reference_id": option["reference_id"]}
        rankable = (
            judgment["primary_type"] != "insufficient_evidence"
            and judgment["evidence_reliability"] != "insufficient"
        )
        if rankable:
            rank += 1
        tech = [
            ref
            for ref, f in facts.items()
            if f["subject_id"] == code
            and f.get("calculation_version")
            and f.get("value") is not None
        ]
        # A fixed display order, never used as a model score or selection rule.
        metrics = ("close_to_ma20", "drawdown_from_peak_close_20d", "rsi14", "macd_hist2_to_close")
        tech = sorted(
            tech,
            key=lambda ref: (
                metrics.index(facts[ref]["metric"])
                if facts[ref]["metric"] in metrics
                else len(metrics),
                ref,
            ),
        )[:2]
        reaction = [
            ref for metric in ("return_1d", "return_5d") if (ref := _own(facts, code, metric))
        ]
        shown = {e["evidence_id"] for e in packet.get("evidence", [])}
        event_ids = [
            identity
            for identity in study.get("event_ids", [])
            if any(e["record_id"] == identity for e in candidate.get("events", []))
        ]
        source_ids = sorted(
            {
                ref
                for e in candidate.get("events", [])
                if e["record_id"] in event_ids
                for ref in e.get("source_ids", [])
                if ref in shown
            }
        )[:4]
        status = candidate.get("trading_status")
        status = status.get("status") if isinstance(status, dict) else status
        analysis = {
            "novelty": study.get("novelty", "unknown"),
            "event_ids": event_ids,
            "exposure": study.get("exposure", "unknown"),
            "incremental_change": "变化及新旧状态沿用本次调查记录，首次采集不等于新消息",
            "economic_link": judgment["reason"],
            "importance": judgment["counterargument"],
            "scale_fact_ids": [],
            "next_session_thesis": judgment["mechanism"],
            "next_observation_date": None,
            "next_node_basis": "条件性目标日观察，不宣称已有后续日程",
            "next_node_is_hypothesis": True,
        }
        row = {
            "instrument_id": code,
            "primary_type": judgment["primary_type"],
            "type_labels": [judgment["primary_type"]],
            "rank": rank if rankable else None,
            "ranking_state": "rankable" if rankable else "not_rankable",
            "final_status": judgment["final_status"],
            "comparator_id": judgment["comparator_id"],
            "comparison_strength": judgment["comparison_strength"],
            "evidence_reliability": judgment["evidence_reliability"],
            "thesis": "；".join(
                filter(None, (_clauses(judgment["support_fact_ids"]), judgment["reason"]))
            ),
            "risk": "；".join(
                filter(None, (_clauses(judgment["counter_fact_ids"]), judgment["counterargument"]))
            ),
            "difference": "；".join(
                filter(
                    None,
                    (
                        _clauses(
                            candidate["comparison_fact_pairs"].get(judgment["comparator_id"], [])
                        ),
                        judgment["difference"],
                    ),
                )
            ),
            "independent_basis": judgment["independent_basis"],
            "unknowns": judgment["unknowns"],
            "invalidation": "按选定规则在目标日对应时点核查；缺数据保持未知",
            "invalidation_rule": rule,
            "next_session_condition": condition,
            "analysis": analysis,
            "evidence_ids": ["market:" + code, *source_ids],
            "semantic_claims": [],
            "technical_fact_ids": tech,
            "technical_interpretation": (
                "技术指标来自同一价格家族，未来需求和成交条件未知" if tech
                else "技术事实不足，位置未知"
            ),
            "price_reaction": _clauses(reaction) or "历史价格反应未知",
            "participation_status": "restricted"
            if status == "hold_for_official_notice_review"
            else "observe_only",
            "participation_cancel_rule": {
                "rule_id": "target_open_limit_or_halt_v1",
                "reference_id": None,
            },
            "trade_conditions": {
                "known": "交易状态与研究排序独立，日线不证明目标日可成交",
                "unknown": "目标日开盘价格、停牌最终状态与实际成交条件待确认",
            },
            "opportunity_ids": [
                h["hypothesis_id"]
                for h in packet.get("opportunity_hypotheses", [])
                if code in h.get("instrument_ids", [])
            ],
        }
        rows.append(row)
    return {"market_view": value["market_view"], "comparisons": rows}


def errors(value, packet):
    issues = [
        {"path": list(e.path), "code": "judgment_schema", "detail": e.message[:240]}
        for e in Draft202012Validator(schema(packet)).iter_errors(value)
    ]
    if issues:
        return issues
    candidates = {c["instrument_id"]: c for c in packet["candidates"]}
    facts = unpack_facts(packet)
    ids = [r["instrument_id"] for r in value["comparisons"]]
    if len(set(ids)) != len(ids) or set(ids) != set(candidates):
        return [{"path": ["comparisons"], "code": "duplicate_or_missing_subject"}]
    for row in value["comparisons"]:
        code = row["instrument_id"]
        for field in ("support_fact_ids", "counter_fact_ids"):
            for ref in row[field]:
                if (
                    ref not in facts
                    or facts[ref]["subject_id"] != code
                    or facts[ref].get("value") is None
                ):
                    issues.append(
                        {
                            "path": [code, field],
                            "code": "requires_shown_nonnull_own_fact",
                            "actual": ref,
                        }
                    )
        option = packet["condition_options"].get(row["condition_id"])
        if row["condition_id"] is not None and (not option or option["instrument_id"] != code):
            issues.append(
                {"path": [code, "condition_id"], "code": "condition_option_not_shown_for_subject"}
            )
        if option and (
            (option["kind"] == "official_event_progress") != (row["primary_type"] == "event_update")
        ):
            issues.append({"path": [code, "condition_id"], "code": "condition_type_mismatch"})
    if issues:
        return issues
    return validate_output(assemble(value, packet), packet)


def provenance(value, packet, output):
    return {
        "version": VERSION,
        "model_judgments_sha256": fingerprint(value),
        "assembled_output_sha256": fingerprint(output),
        "condition_options_sha256": fingerprint(packet["condition_options"]),
        "investigation_records_sha256": fingerprint(packet.get("investigation_opportunities", [])),
        "model_order": [r["instrument_id"] for r in value["comparisons"]],
        "model_grades": [[r["instrument_id"], r["final_status"]] for r in value["comparisons"]],
        "program_selection_score": None,
        "invalid_rows_promoted": False,
    }


def repair_plan(previous, issues, contract):
    if not isinstance(previous, dict) or not isinstance(previous.get("comparisons"), list):
        return None
    rows = previous["comparisons"]
    if not all(isinstance(r, dict) and isinstance(r.get("instrument_id"), str) for r in rows):
        return None
    ids = [r["instrument_id"] for r in rows]
    expected = contract["properties"]["comparisons"]["items"]["properties"]["instrument_id"]["enum"]
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        return None
    targets, market = set(), False
    for issue in issues:
        if issue["code"] == "adapter_invalid_response":
            continue
        path = issue.get("path", [])
        if path and path[0] in ids:
            targets.add(path[0])
        elif path[:1] == ["comparisons"] and len(path) > 1 and isinstance(path[1], int):
            targets.add(ids[path[1]])
        elif path[:1] == ["market_view"] or path[:2] == ["all", "market_view"]:
            market = True
        elif path[:1] == ["all"]:
            targets.update(ids)
        else:
            return None
    item = deepcopy(contract["properties"]["comparisons"]["items"])
    item["properties"]["instrument_id"] = {"enum": sorted(targets)}
    props = {
        "repairs": {
            "type": "array",
            "minItems": len(targets),
            "maxItems": len(targets),
            "items": item,
        }
    }
    if market:
        props["market_view"] = deepcopy(contract["properties"]["market_view"])
    return {
        "targets": sorted(targets),
        "market": market,
        "previous_rows": [r for r in rows if r["instrument_id"] in targets],
        "schema": {
            "$id": SCHEMA_ID + "-repairs",
            "type": "object",
            "additionalProperties": False,
            "required": list(props),
            "properties": props,
        },
    }


def merge_repairs(previous, patch, plan):
    issues = [
        {"path": list(e.path), "code": "judgment_repair_schema", "detail": e.message[:240]}
        for e in Draft202012Validator(plan["schema"]).iter_errors(patch)
    ]
    if issues:
        return None, issues
    rows = patch["repairs"]
    replacements = {r["instrument_id"]: r for r in rows}
    if len(replacements) != len(rows) or set(replacements) != set(plan["targets"]):
        return None, [
            {"path": ["repairs"], "code": "repair_subject_missing_duplicate_or_unauthorized"}
        ]
    result = deepcopy(previous)
    result["comparisons"] = [
        deepcopy(replacements.get(r["instrument_id"], r)) for r in previous["comparisons"]
    ]
    if plan["market"]:
        result["market_view"] = patch["market_view"]
    return result, []


def model_packet(packet):
    """Lossless field-name compaction of administrative event/option metadata."""
    result = deepcopy(packet)
    event_columns = sorted({k for c in packet["candidates"] for e in c["events"] for k in e})
    result["event_columns"] = event_columns
    for candidate in result["candidates"]:
        candidate["events"] = [
            [e[k] if k in e else {"$not_present": True} for k in event_columns]
            for e in candidate["events"]
        ]
    columns = ["instrument_id", "kind", "fact_ids", "event_id", "rule_id", "reference_id"]
    result["condition_columns"] = columns
    result["condition_options"] = {
        key: [value[k] for k in columns] for key, value in packet["condition_options"].items()
    }
    return result


def decode_model_packet(packet):
    result = deepcopy(packet)
    if "event_columns" in result:
        columns = result.pop("event_columns")
        for candidate in result["candidates"]:
            candidate["events"] = [
                {k: v for k, v in zip(columns, row, strict=True) if v != {"$not_present": True}}
                for row in candidate["events"]
            ]
    if "condition_columns" in result:
        columns = result.pop("condition_columns")
        result["condition_options"] = {
            key: dict(zip(columns, row, strict=True))
            for key, row in result["condition_options"].items()
        }
    return result


def prompt(packet):
    return INSTRUCTION + compact(model_packet(packet))
