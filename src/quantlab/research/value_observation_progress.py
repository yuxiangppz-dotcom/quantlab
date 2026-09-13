"""Read sealed observation evidence; no scoring, provider, or evaluation calls."""

from datetime import datetime
from zoneinfo import ZoneInfo

from quantlab.data.models import DataValidationError
from quantlab.research.round2_dataset import sealed_read, verify_entries
from quantlab.research.s2_prospective import validate_registration

BASE = "data/products/research_program/launch_20260912/"
BUNDLES = {
    "value": (
        BASE + "s2_prospective_20260913",
        {
            "observation.json": "2b9a706a45d08443d9063ae180c7f8c5edd385a90144457d48ee0b28e14f105f",
            "proof.json": "e8ed5c36613c3316d705986f45d47a1a4ebb4f34317c1c8abd7111b77cd88ecc",
        },
    ),
    "calendar": (
        BASE + "s2_observation_calendar",
        {
            "calendar_plan.json": (
                "5b4b47f521799eb033209c22293ff6ef2"
                "343baff2e39475f1bc8f0eaf2908200"
            ),
            "proof.json": "3dad0a3085d0b7dbe3d651d6b83b50192e076e83d984bcc64ce58132fecf0f8e",
        },
    ),
    "closing": (
        "data/products/rule_evidence/closing_quantity_20260913",
        {
            "report.json": "e439195d5d771854b5bda54f71b4bb57bb897a1174e2040cb265e9f74d308e6d",
            "proof.json": "013b96ecf44868b4133e8bb596c96bf1dc658450c6f9611f541a7290a1e99d72",
        },
    ),
}


def _bundle(root, key):
    folder, sources = BUNDLES[key]
    paths = [root / folder / name for name in sources]
    if not any(p.exists() for p in paths):
        return None
    if not all(p.exists() for p in paths):
        raise DataValidationError("观察与复核记录不齐全")
    records = []
    for path, expected in zip(paths, sources.values(), strict=True):
        row = sealed_read(path)
        if row["fingerprint"] != expected:
            raise DataValidationError("记录不是已复核版本")
        records.append(row)
    return records


def read_value_observation(root):
    pair = _bundle(root, "value")
    if pair is None:
        return None
    report, proof = pair
    if (
        proof["observation_fingerprint"] != report["fingerprint"]
        or proof["all_checks_passed"] is not True
        or proof["checked_codes"] != 256
        or report["cohort_size"] != 256
        or len(report["records"]) != 256
        or proof["scores_known"] != report["scores_known"]
    ):
        raise DataValidationError("估值观察与复核范围不一致")
    for flag in (
        "future_labels_evaluated",
        "historical_pit_certified",
        "performance_evidence",
        "historical_performance_eligible",
        "execution_authority",
        "future_calendar_complete",
    ):
        if report[flag] is not False:
            raise DataValidationError("观察不能升级为历史或未来收益资格")
    if any(proof[k] is not False for k in ("future_labels_evaluated", "execution_authority")):
        raise DataValidationError("复核不能赋予交易或收益权限")
    if (
        report["identity"] != "S2-A:prospective-value-20260913-v1"
        or report["correlation"] is not None
        or report["future_exit_date"] is not None
        or report["label_status"] != "pending_future_calendar_and21_session_prices"
        or report["price_as_of"] != "2026-09-11"
        or report["earliest_entry_session"] != "2026-09-14"
        or report["rule_candidates_cumulative"] != 4
        or report["economic_paths"] != 0
        or report["model_fits"] != 0
    ):
        raise DataValidationError("观察时间、用量或待评价状态发生变化")
    reasons = report["reasons"]
    if (
        set(reasons)
        != {
            "observed_value_score",
            "valuation_unknown_or_nonpositive",
            "fewer_than20_valid_values_in_size_group",
            "size_unknown_or_nonpositive",
        }
        or type(report["scores_known"]) is not int
        or any(type(v) is not int or v < 0 for v in reasons.values())
        or sum(reasons.values()) != 256
        or reasons.get("observed_value_score") != report["scores_known"]
        or any(type(r["score_known"]) is not bool for r in report["records"])
        or sum(r["score_known"] for r in report["records"]) != report["scores_known"]
    ):
        raise DataValidationError("已知与未知样本数量不一致")
    created, received = map(
        datetime.fromisoformat, (report["created_at"], report["source_received_at"])
    )
    validate_registration(created, received)
    plan = _bundle(root, "calendar")
    end = None
    if plan:
        calendar, calendar_proof = plan
        dates = calendar["planned_label_dates"]
        if (
            calendar["observation_fingerprint"] != report["fingerprint"]
            or calendar_proof["report_fingerprint"] != calendar["fingerprint"]
            or calendar_proof["all_checks_passed"] is not True
            or calendar_proof["source_rows_checked"] != 102
            or calendar_proof["planned_end"] != calendar["planned_end"]
            or len(dates) != 21
            or sorted(set(dates)) != dates
            or dates[0] != "2026-09-14"
            or calendar["planned_entry"] != dates[0]
            or dates[-1] != calendar["planned_end"]
            or calendar["subsequent_sessions"] != 20
            or calendar["requires_calendar_revalidation_at_evaluation"] is not True
        ):
            raise DataValidationError("计划日历与原20交易日规则不一致")
        if any(
            calendar[k] is not False
            for k in (
                "future_actual_openings_certified",
                "future_labels_evaluated",
                "execution_authority",
                "performance_evidence",
            )
        ) or any(
            calendar_proof[k] is not False
            for k in ("future_labels_evaluated", "execution_authority")
        ):
            raise DataValidationError("日历计划不是未来开市或收益证明")
        verify_entries(root / BUNDLES["calendar"][0], calendar["raw_files"])
        end = calendar["planned_end"]
    return {
        "created_at_china": created.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "price_as_of": report["price_as_of"],
        "cohort": 256,
        "known": report["scores_known"],
        "reasons": reasons,
        "planned_entry": "2026-09-14",
        "planned_end": end,
        "subsequent_sessions": 20,
        "rule_candidates_cumulative": 4,
        "state": "已登记，未来结果尚未评价",
        "performance_evidence": False,
        "execution_authority": False,
        "observation_fingerprint": report["fingerprint"],
    }


def read_closing_progress(root):
    pair = _bundle(root, "closing")
    if pair is None:
        return None
    report, proof = pair
    if (
        proof["report_fingerprint"] != report["fingerprint"]
        or proof["all_checks_passed"] is not True
        or proof["checked_rows"] != 2904
        or report["covered"] != 2904
        or len(report["rows"]) != 2904
        or report["sessions"] != 726
        or report["unchanged_continuous_payloads"] != 2904
        or any(
            r[k] is not False for r in pair for k in ("execution_authority", "performance_evidence")
        )
    ):
        raise DataValidationError("收盘数量子集复核不一致或越过权限边界")
    return {
        "sessions": 726,
        "board_dates": 2904,
        "execution_authority": False,
        "performance_evidence": False,
    }
