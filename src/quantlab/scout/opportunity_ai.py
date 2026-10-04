"""Complete comparison contracts for the existing investigation and selection calls."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import date

from jsonschema import validate

from quantlab.scout.ai import (
    DISCOVERY_SCHEMA,
    SELECTION_SCHEMA,
    STRING,
    STRINGS,
    ShownEvidence,
    asserts_unsupported_microstructure,
    obj,
    semantic_numeric_issue,
    unsupported_numeric_claims,
    validate_selection,
)
from quantlab.scout.models import fingerprint
from quantlab.scout.opportunities import TYPE_LABELS, VERSION


def enum(values: list[str]) -> dict:
    return {"type": "string", "enum": values}


ANALYSIS = obj(
    {
        "novelty": enum(
            [
                "new_event",
                "material_update",
                "repeated_content",
                "long_term_background",
                "routine_schedule",
                "unknown",
                "price_only",
            ]
        ),
        "event_ids": STRINGS,
        "incremental_change": STRING,
        "economic_link": STRING,
        "exposure": enum(["direct", "indirect", "price_only", "unknown"]),
        "importance": STRING,
        "scale_fact_ids": STRINGS,
        "h5_mechanism": STRING,
        "next_observation_date": {"type": ["string", "null"]},
        "next_node_basis": STRING,
        "next_node_is_hypothesis": {"type": "boolean"},
    }
)
INVESTIGATION_SCHEMA = obj(
    {
        "hypotheses": DISCOVERY_SCHEMA["properties"]["hypotheses"],
        "opportunities": {
            "type": "array",
            "items": obj({"instrument_id": STRING, "analysis": ANALYSIS}),
        },
    }
)
NARRATIVE_FIELDS = ("thesis", "risk", "invalidation", "difference", "independent_basis", "unknowns")
ANALYSIS_FIELDS = (
    "incremental_change",
    "economic_link",
    "importance",
    "h5_mechanism",
    "next_node_basis",
)
TRADE_FIELDS = ("trade_known", "trade_unknown")
CLAIM = SELECTION_SCHEMA["properties"]["selected"]["items"]["properties"]["quant_claims"]["items"]
CLAIM = obj(
    {**CLAIM["properties"], "field": enum([*NARRATIVE_FIELDS, *ANALYSIS_FIELDS, *TRADE_FIELDS])}
)
COMPARISON = obj(
    {
        "instrument_id": STRING,
        "primary_type": enum(list(TYPE_LABELS)),
        "type_labels": {"type": "array", "items": enum(list(TYPE_LABELS))},
        "rank": {"type": ["integer", "null"], "minimum": 1},
        "final_status": enum(["focus", "watch", "unselected"]),
        "comparator_id": {"type": ["string", "null"]},
        "comparison_strength": enum(["strong", "medium", "weak", "insufficient"]),
        "evidence_reliability": enum(
            ["program_facts", "source_requires_verification", "insufficient"]
        ),
        "trade_conditions": obj({"known": STRING, "unknown": STRING}),
        **{field: STRING for field in NARRATIVE_FIELDS},
        "analysis": ANALYSIS,
        "evidence_ids": STRINGS,
        "fact_ids": STRINGS,
        "quant_claims": {"type": "array", "items": CLAIM},
    }
)
OPPORTUNITY_SCHEMA = obj(
    {"market_view": STRING, "comparisons": {"type": "array", "items": COMPARISON}}
)
INSTRUCTION = """本次启用opportunity_v1。主期限H5；机会类型与发现路线不同。
每只实际深查股都必须解释新增变化、经济关系、规模口径、H5机制和反证。首次采集不等于市场新消息；
possible_update只说明内容变化，不证明实质变化。旧预告、例行说明会、框架协议不等于当前业绩催化，
预增不等于超预期，概念成员不能证明公司利益。量价机会可没有新公告，并明确量价假设。
最终必须比较所有实际深查股：指定主要类型和唯一名次；证据不足使用rank=null、unselected。
最多3重点5观察。每只入选股与实际输入中同类最接近的未选者比较，说明具体差异；
确无同类未选者时comparator_id=null并说明缺口。不得照抄发现score，不能把证据可信度、
机会强中弱和交易条件合成概率。写出涨幅/涨停/热榜以外的依据或承认纯量价假设。
核心量化声明在quant_claims逐项绑定原句字段和program_facts；字段可为analysis内部字段名。
next_observation_date无已知来源日程时为null，假设节点需明确标记；不要虚构时间。
重复传播不是独立证据，未展示或省略的材料不能引用。按原文保留风险，不为通过校验删除反证。
"""


def investigation_schema(candidates: list[dict]) -> dict:
    """Bind investigation coverage to this run, independently of final display caps."""
    schema = deepcopy(INVESTIGATION_SCHEMA)
    rows = schema["properties"]["opportunities"]
    rows["minItems"] = rows["maxItems"] = len(candidates)
    rows["items"]["properties"]["instrument_id"] = enum(
        [row["instrument_id"] for row in candidates]
    )
    return schema


def check_coverage(rows: list[dict], candidates: list[dict]) -> None:
    expected = {c["instrument_id"] for c in candidates}
    counts = Counter(r.get("instrument_id") for r in rows)
    if set(counts) != expected or any(n != 1 for n in counts.values()):
        raise ValueError("opportunity_coverage_incomplete_or_duplicate")


def validate_comparisons(
    result: dict, candidates: list[dict], evidence: list[dict], market: dict
) -> dict:
    validate(result, OPPORTUNITY_SCHEMA)
    rows = result.get("comparisons")
    if not isinstance(rows, list):
        raise ValueError("opportunity_comparisons_missing")
    check_coverage(rows, candidates)
    by_code = {c["instrument_id"]: c for c in candidates}
    by_event = {
        e["record_id"]: e
        for c in candidates
        for e in c.get("opportunity_record", {}).get("events", [])
    }
    by_row = {r["instrument_id"]: r for r in rows}
    shown = [ShownEvidence.from_packet(e) for e in evidence]
    ranks = [r["rank"] for r in rows if r.get("rank") is not None]
    if any(type(r) is not int for r in ranks) or sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise ValueError("opportunity_ranks_not_unique_contiguous")
    selected = []
    for row in rows:
        code = row["instrument_id"]
        own = by_code[code]
        if row["primary_type"] not in TYPE_LABELS or row["primary_type"] not in row["type_labels"]:
            raise ValueError("opportunity_type_invalid")
        insufficient = row["primary_type"] == "insufficient_evidence"
        if (
            insufficient != (row["rank"] is None)
            or insufficient
            and row["final_status"] != "unselected"
        ):
            raise ValueError("opportunity_insufficient_cannot_rank_or_select")
        if row["final_status"] not in {"focus", "watch", "unselected"}:
            raise ValueError("opportunity_status_invalid")
        comparator = row["comparator_id"]
        same_type_unselected = [
            r
            for r in rows
            if r["instrument_id"] != code
            and r["primary_type"] == row["primary_type"]
            and r["final_status"] == "unselected"
        ]
        if comparator is not None and (comparator not in by_row or comparator == code):
            raise ValueError("opportunity_comparator_not_shown")
        if row["final_status"] in {"focus", "watch"} and same_type_unselected:
            if comparator not in {r["instrument_id"] for r in same_type_unselected}:
                raise ValueError("opportunity_needs_same_type_unselected_comparator")
        if not all(isinstance(row.get(f), str) and row[f].strip() for f in NARRATIVE_FIELDS):
            raise ValueError("opportunity_missing_reason_or_counterevidence")
        analysis = row["analysis"]
        event_ids = analysis["event_ids"]
        if any(e not in by_event or by_event[e]["instrument_id"] != code for e in event_ids):
            raise ValueError("opportunity_event_wrong_subject_or_not_shown")
        if row["primary_type"] == "event_update" and not event_ids:
            raise ValueError("opportunity_event_missing_identity")
        if row["primary_type"] == "event_update" and analysis["novelty"] in {
            "routine_schedule",
            "long_term_background",
            "repeated_content",
            "price_only",
        }:
            raise ValueError("opportunity_no_increment_for_event_type")
        if (
            row["primary_type"] == "event_update"
            and row["final_status"] == "focus"
            and (
                analysis["novelty"] not in {"new_event", "material_update"}
                or all(by_event[e]["title_only"] for e in event_ids)
                or analysis["exposure"] == "unknown"
            )
        ):
            raise ValueError("opportunity_event_focus_needs_increment_and_body")
        if analysis["novelty"] in {"new_event", "material_update"}:
            if not event_ids or all(
                by_event[e]["novelty"]
                in {"routine_schedule", "long_term_background", "repeated_content"}
                for e in event_ids
            ):
                raise ValueError("opportunity_old_or_routine_as_new")
            if analysis["novelty"] == "material_update" and not any(
                by_event[e]["previous_record_id"] for e in event_ids
            ):
                raise ValueError("opportunity_update_without_prior")
        if analysis["exposure"] == "indirect" and not analysis["economic_link"].strip():
            raise ValueError("opportunity_indirect_link_missing")
        node = analysis["next_observation_date"]
        if node:
            parsed = date.fromisoformat(node)
            if not analysis["next_node_is_hypothesis"]:
                own_source = " ".join(
                    e["title"] + " " + e["body"]
                    for e in evidence
                    if code in e.get("instrument_ids", [])
                )
                alternatives = [
                    node,
                    node.replace("-", ""),
                    f"{parsed.year}年{parsed.month}月{parsed.day}日",
                ]
                if not any(d in own_source for d in alternatives):
                    raise ValueError("opportunity_observation_date_not_in_source")
        if not all(
            isinstance(analysis.get(f), str) and analysis[f].strip() for f in ANALYSIS_FIELDS
        ):
            raise ValueError("opportunity_analysis_missing")
        known = {f["fact_id"] for c in candidates for f in c.get("program_facts", [])}
        if not set(analysis["scale_fact_ids"]) <= known or any(
            not f.startswith(f"fact:{code}:") for f in analysis["scale_fact_ids"]
        ):
            raise ValueError("opportunity_scale_unknown_fact")
        text_fields = {
            **{f: row[f] for f in NARRATIVE_FIELDS},
            **{f: analysis[f] for f in ANALYSIS_FIELDS},
            "trade_known": row["trade_conditions"]["known"],
            "trade_unknown": row["trade_conditions"]["unknown"],
        }
        cited = [e for e in shown if e.evidence_id in row["evidence_ids"]]
        for field, text in text_fields.items():
            issue = semantic_numeric_issue(
                field, text, own, cited, row["fact_ids"], candidates, row["quant_claims"]
            )
            if issue:
                raise issue
            # Preserve the existing guardrails also in the newly added explanations.
            from quantlab.scout.facts import extract_core_claims

            residual = text
            for claim in extract_core_claims(field, text, own, candidates):
                residual = residual.replace(claim.text, "", 1)
            if unsupported_numeric_claims(
                residual, own, cited
            ) or asserts_unsupported_microstructure(text):
                raise ValueError("opportunity_unsupported_explanation")
        base = {
            k: row[k]
            for k in ("instrument_id", "thesis", "risk", "invalidation", "evidence_ids", "fact_ids")
        }
        base["quant_claims"] = [
            q for q in row["quant_claims"] if q["field"] in {"thesis", "risk", "invalidation"}
        ]
        # New prose receives the established exhaustive-source/cross-subject checks,
        # as well as typed-number validation. Concatenation is only an audit view;
        # the original comparison fields remain untouched in the archived output.
        audit_thesis = "。".join(
            value for field, value in text_fields.items() if field not in {"risk", "invalidation"}
        )
        audit_base = {k: v for k, v in base.items() if k != "quant_claims"}
        validate_selection(
            {
                "market_view": result["market_view"],
                "selected": [
                    {
                        **audit_base,
                        "status": "watch",
                        "thesis": audit_thesis,
                    }
                ],
            },
            candidates,
            shown,
            market,
        )
        own_and_named = {code} | {
            c["instrument_id"]
            for c in candidates
            if c["instrument_id"] in " ".join(row[f] for f in ("thesis", "risk", "invalidation"))
            or c.get("name")
            and c["name"] in " ".join(row[f] for f in ("thesis", "risk", "invalidation"))
        }
        base["fact_ids"] = [
            ref
            for ref in base["fact_ids"]
            if any(ref.startswith(f"fact:{c}:") for c in own_and_named)
        ]
        base["evidence_ids"] = [
            ref
            for ref in base["evidence_ids"]
            if ref in {f"market:{c}" for c in own_and_named}
            or any(
                e["evidence_id"] == ref
                and (not e.get("instrument_ids") or set(e["instrument_ids"]) <= own_and_named)
                for e in evidence
            )
        ]
        # Even an unselected candidate's support must obey the same evidence/number rules.
        validate_selection(
            {"market_view": result["market_view"], "selected": [{**base, "status": "watch"}]},
            candidates,
            shown,
            market,
        )
        if row["final_status"] != "unselected":
            selected.append(
                {
                    **base,
                    "status": row["final_status"],
                    "primary_type": row["primary_type"],
                    "rank": row["rank"],
                    "comparison": row,
                }
            )
    selection = {"market_view": result["market_view"], "selected": selected}
    validate_selection(selection, candidates, shown, market)
    return selection


def freeze_comparisons(
    rows: list[dict], packet: dict, report_meta: dict, universe: dict, memberships: dict
) -> dict:
    """Identity and memberships freeze at report time; no future classification refill."""
    ordered = sorted((r for r in rows if r["rank"] is not None), key=lambda r: r["rank"])
    sizes = [len(ordered) // 3 + int(i < len(ordered) % 3) for i in range(3)]
    bands, cursor = {}, 0
    for label, size in zip(("top", "middle", "bottom"), sizes, strict=True):
        for row in ordered[cursor : cursor + size]:
            bands[row["instrument_id"]] = label
        cursor += size
    frozen = []
    for row in rows:
        code = row["instrument_id"]
        industry = memberships.get(code)
        peers = sorted(
            c for c in universe if c != code and industry and memberships.get(c) == industry
        )
        candidate = next(c for c in packet["candidates"] if c["instrument_id"] == code)
        themes = (candidate.get("source_summary") or {}).get("themes") or []
        frozen.append(
            {
                "instrument_id": code,
                "primary_type": row["primary_type"],
                "rank": row["rank"],
                "rank_band": bands.get(code, "insufficient"),
                "final_status": row["final_status"],
                "source_routes": candidate.get("recall_routes", []),
                "industry": industry,
                "themes": themes,
                "reference_ids": peers,
                "reference_status": "frozen"
                if industry and len(peers) >= 3
                else "insufficient_members_or_classification",
                "trading_status_at_report": (candidate.get("source_summary") or {}).get(
                    "trading_status"
                ),
                "invalidation": row["invalidation"],
                "event_ids": row["analysis"]["event_ids"],
            }
        )
    return {
        "version": VERSION,
        "status": "complete",
        "primary_horizon_sessions": 5,
        "rows": frozen,
        "metadata": report_meta,
        "input_sha256": fingerprint(packet),
        "reference_policy": "frozen_other_eligible_industry_members_min3_full_coverage_v1",
        "rank_band_policy": "unique ranks ascending; divmod(n,3), remainder to top then middle",
        "market_asof_session": packet["timing"]["asof_session"],
        "eligible_ids": sorted(universe),
        "memberships": {c: memberships[c] for c in universe if c in memberships},
    }
