"""Read sealed preparation summaries for display, without running experiments."""

from quantlab.data.models import DataValidationError
from quantlab.research.round2_dataset import sealed_read

BASE = "data/products/research_program/launch_20260912/"
RULES = "data/products/rule_evidence/rule_catalogue_v3_20260913/"
SOURCES = {
    "terms": (
        BASE + "corporate_minimum_terms/report.json",
        "dc6af719c9cb7ad7b61a20a929a795c505d30de2c48e501295d81373efeef4fc",
    ),
    "terms_proof": (
        BASE + "corporate_minimum_terms/independent_proof.json",
        "600e679907ffbd524f560fb23784bc6c8015cb5e33ea37a011ae774b6f02c64e",
    ),
    "recipients": (
        BASE + "corporate_20_primary_review/guard_report.json",
        "c2d115279ac56bafcdebfcebf86478f16d5a4be69f036b87b3b088c6ad21db16",
    ),
    "annotations": (
        BASE + "corporate_20_primary_review/reviewed_annotations.json",
        "6d66e42a08df7e5e80b58e174ed69538389a5c5ac8fc7a1c782ef6e6f7267322",
    ),
    "rules": (
        RULES + "report.json",
        "0d60b248e67c9d207f4de2aad56356b80d526b495019d1459f6277e95f3a9c25",
    ),
    "rules_proof": (
        RULES + "proof.json",
        "ebe88ce00b9125cd1a80bc21700cfc1ca2decdacf7b8e3ea71820974d8d60237",
    ),
}


def read_preparation(root):
    existing = [(root / name).exists() for name, _ in SOURCES.values()]
    if not any(existing):
        return None
    if not all(existing):
        raise DataValidationError("回放准备度资料尚不完整，不能把缺失部分显示为已通过")
    records = {}
    for key, (name, fingerprint) in SOURCES.items():
        row = sealed_read(root / name)
        if row["fingerprint"] != fingerprint:
            raise DataValidationError("回放准备度记录与已复核版本不同")
        records[key] = row
    for key in ("terms", "rules"):
        proof = records[key + "_proof"]
        if (
            proof["report_fingerprint"] != records[key]["fingerprint"]
            or proof["all_checks_passed"] is not True
        ):
            raise DataValidationError("回放准备度报告缺少对应的通过复核")
    if records["recipients"]["annotations_fingerprint"] != records["annotations"]["fingerprint"]:
        raise DataValidationError("股本分配注释与原件核对版本不一致")
    for key in ("terms", "recipients", "annotations", "rules"):
        if records[key]["execution_authority"] is not False:
            raise DataValidationError("准备度资料不能授予执行权限")
    for key in ("terms", "recipients", "annotations"):
        if (
            records[key]["cashflow_eligible"] is not False
            or records[key]["historical_pit_certified"] is not False
        ):
            raise DataValidationError("准备度资料不能升级现金流或历史时点资格")
    if records["rules"]["performance_evidence"] is not False:
        raise DataValidationError("规则范围不等于收益证据")
    terms, recipients, rules = (records[k] for k in ("terms", "recipients", "rules"))
    return {
        "terms": terms["implementation"],
        "recipient_occurrences": len(recipients["results"]),
        "cash_notice_annotations": recipients["cash_annotations"],
        "share_notice_annotations": recipients["share_annotations"],
        "ordinary_recipient_evidence_complete": recipients["ordinary_recipient_evidence_complete"],
        "ordinary_recipient_evidence_blocked": recipients["ordinary_recipient_evidence_blocked"],
        "rule_comparison": rules["comparison"],
        "rule_scopes": rules["summary"],
        "source_evidence": [
            {
                "path": SOURCES[k][0],
                "fingerprint": r["fingerprint"],
                "recorded_at": r.get("at"),
                "source_head": r.get("source_head"),
            }
            for k, r in records.items()
        ],
        "execution_authority": False,
        "performance_evidence": False,
        "cashflow_eligible": False,
        "historical_pit_certified": False,
    }
