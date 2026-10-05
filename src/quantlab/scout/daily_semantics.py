"""Closed quantitative meanings; program templates, not Chinese subject/number inference."""

import re
from decimal import Decimal, InvalidOperation

VERSION = "scout_semantics_v1"
BENCHMARK = "current_eligible_industry_members_mean"
BENCHMARK_LABEL = "本次合格行业成员均值（非指数、非历史完整行业）"
CORE = re.compile(r"^(?:relative_)?return_(?:1|5|20)d$|^net_(?:1|3|5)d_wan_cny$|^amount_ratio_5d$")
REF = re.compile(r"\[\[([^\[\]]+)\]\]")
KINDS = ("absolute_return", "relative_return", "fund_windows_same_sign", "amount_multiple")
CLAIM_SCHEMA = {
    "type": "array",
    "maxItems": 8,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "subject_id", "fact_ids", "periods", "direction", "benchmark"],
        "properties": {
            "kind": {"enum": list(KINDS)},
            "subject_id": {"type": "string", "maxLength": 32},
            "fact_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^fact:"},
            },
            "periods": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"enum": ["1d", "3d", "5d", "20d"]},
            },
            "direction": {"enum": ["positive", "negative", "zero"]},
            "benchmark": {"enum": [None, BENCHMARK]},
        },
    },
}
INSTRUCTION = """量化含义由semantic_claims声明：kind为absolute_return/relative_return/
fund_windows_same_sign/amount_multiple，绑定subject_id、fact_ids、periods、direction、benchmark。
相对收益基准只能current_eligible_industry_members_mean，其余benchmark=null。
多窗口同号必须至少两个完整资金窗口且方向一致；窗口重叠，不代表逐日改善或独立证据。
正文里的核心事实引用须为单独分句，只可加本股/同行/行业/事实/资金反证/当时日线等中性标签。
禁止在事实引用分句附加“走强/一致/量比/改善”等自由解释，含义由程序事实和声明渲染。
未来条件假设另写句子，明确“若/需/待观察”，不冒充已发生事实。成交额倍数不是成交量量比。
"""


def claim_errors(claims, facts, allowed):
    errors = []
    for index, claim in enumerate(claims):
        refs = claim.get("fact_ids", [])
        bound = [facts[r] for r in refs if r in facts]
        kind, subject = claim.get("kind"), claim.get("subject_id")
        valid = bool(bound) and len(bound) == len(refs) and subject in allowed
        valid = valid and all(f["subject_id"] == subject for f in bound)
        valid = valid and sorted(claim.get("periods", [])) == sorted(f["period"] for f in bound)
        expected = {
            "absolute_return": (r"return_(1|5|20)d", "ratio"),
            "relative_return": (r"relative_return_(1|5|20)d", "ratio"),
            "fund_windows_same_sign": (r"net_(1|3|5)d_wan_cny", "wan_CNY"),
            "amount_multiple": (r"amount_ratio_5d", "times"),
        }.get(kind)
        valid = (
            valid
            and expected is not None
            and all(
                re.fullmatch(expected[0], f["metric"]) and f["unit"] == expected[1] for f in bound
            )
        )
        valid = valid and claim.get("benchmark") == (
            BENCHMARK if kind == "relative_return" else None
        )
        valid = valid and (len(bound) >= 2 if kind == "fund_windows_same_sign" else len(bound) == 1)
        for fact in bound:
            try:
                value = Decimal(fact["value"])
                direction = "positive" if value > 0 else "negative" if value < 0 else "zero"
                valid = valid and value.is_finite() and direction == claim.get("direction")
            except (InvalidOperation, TypeError, ValueError):
                valid = False
        if not valid:
            errors.append({"index": index, "code": "semantic_binding_mismatch"})
    return errors


def prose_errors(text, facts, identities=()):
    """Finite grammar around core references prevents synonym-based relabelling.

    This is a surface-contract check, not a proof that qualitative reasoning is true.
    Non-quantitative conditional reasoning remains model judgment for human review.
    """
    errors = []
    for clause in re.split(r"[。；;，,\n]", text):
        core = [r for r in REF.findall(clause) if r in facts and CORE.fullmatch(facts[r]["metric"])]
        if not core:
            continue
        residual = REF.sub("", clause)
        for identity in identities:
            if identity:
                residual = residual.replace(identity, "")
        for label in (
            "当时日线",
            "资金反证",
            "程序事实",
            "市场事实",
            "本股",
            "同行",
            "行业",
            "事实",
            "反证",
        ):
            residual = residual.replace(label, "")
        if re.sub(r"[\s：:、/（）()·与及和]", "", residual):
            errors.append("core_fact_clause_requires_neutral_label")
    # These claims cannot be established by same-asof overlapping snapshots.
    if re.search(r"(?:资金|净额).{0,8}(?:持续改善|逐日改善|连续改善|不断改善)", text):
        errors.append("fund_improvement_requires_cross_time_facts")
    if re.search(r"(?:资金|窗口).{0,8}(?:多窗口一致|窗口一致|多窗口同向|多窗同向)", text):
        errors.append("fund_consistency_requires_structured_claim")
    return list(dict.fromkeys(errors))


def render_claim(claim, facts, formatter):
    label = {
        "absolute_return": "本股绝对涨跌幅",
        "relative_return": "相对收益；基准=" + BENCHMARK_LABEL,
        "fund_windows_same_sign": "资金窗口同号（重叠累计窗口，非独立证据，不能证明逐日改善）",
        "amount_multiple": "当日成交额/前五日平均成交额；不是成交量量比",
    }[claim["kind"]]
    return label + "：" + "；".join(formatter(facts[r]) for r in claim["fact_ids"])
