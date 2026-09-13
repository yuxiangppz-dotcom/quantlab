"""Read-only display of one verified prospective observation, never its evaluator."""

from datetime import datetime
from zoneinfo import ZoneInfo

from quantlab.data.models import DataValidationError
from quantlab.research.round2_dataset import sealed_read, verify_entries

BASE = "data/products/research_program/launch_20260912/s4_prospective_20260913/"
SOURCES = {
    "observation": (
        "observation.json",
        "50c35dea116a0909416e4921ed3fff5a2d3c8a65c517382f3561868bf6c22ba6",
    ),
    "proof": (
        "independent_proof.json",
        "ffafeaefa403163285b88f10634ecb85585d02d5e6ea27eaceff9d8c4f036a84",
    ),
}


def read_observation(root):
    out = root / BASE
    exists = [(out / name).exists() for name, _ in SOURCES.values()]
    if not any(exists):
        return None
    if not all(exists):
        raise DataValidationError("前瞻观察与独立复核尚不齐全")
    records = {}
    for key, (name, expected) in SOURCES.items():
        row = sealed_read(out / name)
        if row["fingerprint"] != expected:
            raise DataValidationError("前瞻观察记录不是已复核版本")
        records[key] = row
    report, proof = records["observation"], records["proof"]
    if proof["report_fingerprint"] != report["fingerprint"] or any(
        proof[k] is not True
        for k in ("raw_unit_mapping_reconciled", "all_scores_and_masks_reconciled")
    ):
        raise DataValidationError("前瞻观察缺少对应的通过复核")
    if any(
        report[k] is not False
        for k in (
            "broker_order",
            "performance_evidence",
            "execution_authority",
            "candidate_promoted",
        )
    ) or any(proof[k] is not False for k in ("performance_evidence", "execution_authority")):
        raise DataValidationError("价格观察不能被显示为交易或收益资格")
    if (
        report["future_diagnostic_pending"] is not True
        or report["economic_paths"] != 0
        or report["model_fits"] != 0
    ):
        raise DataValidationError("本次观察仍待未来评价，不能直接展示收益")
    if (
        type(report["rows"]) is not int
        or report["rows"] != 256
        or proof["checked_codes"] != report["rows"]
        or proof["checked_grid_rows"] != 256 * 21
    ):
        raise DataValidationError("固定前瞻样本或复核范围发生变化")
    for key in ("known_S4_A", "known_reference20"):
        if type(report[key]) is not int or not 0 <= report[key] <= report["rows"]:
            raise DataValidationError("前瞻有效分数数量无效")
    t = report["timing"]
    created, available, cutoff = [
        datetime.fromisoformat(t[k])
        for k in (
            "created_at",
            "source_available_at",
            "publication_cutoff",
        )
    ]
    china = ZoneInfo("Asia/Shanghai")
    if (
        any(d.utcoffset() is None for d in (created, available, cutoff))
        or not available <= created < cutoff
        or created.astimezone(china).date().isoformat() != "2026-09-13"
        or cutoff.isoformat() != "2026-09-14T09:30:00+08:00"
        or t["price_as_of"] != "2026-09-11"
        or t["old_same_day_forward_shadow_eligible"] is not False
        or t["future_window_not_started"] is not True
    ):
        raise DataValidationError("前瞻观察时间或周末登记口径无效")
    if report["label_dates"] != [
        "2026-09-14",
        "2026-09-15",
        "2026-09-16",
        "2026-09-17",
        "2026-09-18",
        "2026-09-21",
    ]:
        raise DataValidationError("未来比较窗口发生变化")
    verify_entries(out, report["files"])
    if any((out / p).stat().st_size != e["bytes"] for p, e in report["files"].items()):
        raise DataValidationError("前瞻观察输出大小改变")
    return {
        "created_at_china": created.astimezone(china).strftime("%Y-%m-%d %H:%M:%S"),
        "price_as_of": t["price_as_of"],
        "cohort": report["rows"],
        "known_S4_A": report["known_S4_A"],
        "known_reference20": report["known_reference20"],
        "reference_dates": report["label_dates"],
        "state": "已登记，未来结果尚未评价",
        "observation": report,
        "proof": proof,
        "performance_evidence": False,
        "execution_authority": False,
    }
