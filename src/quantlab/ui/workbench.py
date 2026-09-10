"""Read-only workbench composition and explicit user-requested actions."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime

from quantlab.daily.integrity import load_validated_latest_snapshot
from quantlab.daily.service import SHANGHAI, inspect_data_status
from quantlab.personal import list_accounts
from quantlab.research.forward_shadow import DEFAULT_SHADOW_ROOT, generate_forward_shadow
from quantlab.research.forward_shadow_analytics import summarize_forward_shadow
from quantlab.research.shadow_timing import temporal_admission

DATA_STATUS_LABELS = {
    "complete": "最新交易日数据可用",
    "complete_previous_session_for_closed_date": "休市日，使用上一交易日数据",
    "stale_missing_required_data": "行情尚未补齐",
    "stale_calendar_unknown": "交易日历不完整",
    "blocked_no_calendar": "缺少交易日历",
    "blocked_no_common_complete_date": "缺少完整行情",
}


def public_error(error: Exception) -> str:
    message = str(error)
    token = os.environ.get("TUSHARE_TOKEN")
    return message.replace(token, "[凭据已隐藏]") if token else message


def shadow_admission(report: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if report is None:
        return {"eligible": False, "reason": "先生成当天的完整日报。"}
    try:
        timing = temporal_admission(
            report["effective_as_of"],
            now.isoformat(),
            report["generated_at"],
        )
    except (KeyError, ValueError) as exc:
        return {"eligible": False, "reason": f"日报时间信息无法验证：{public_error(exc)}"}
    if report.get("data_status", {}).get("status") != "complete":
        return {"eligible": False, "reason": "日报未确认当天数据完整，请先检查并更新数据。"}
    reasons = {
        "blocked_before_observation_window": "请在信号日 16:00 后生成日报并登记。",
        "blocked_after_signal_day": "日报日期已过登记期限；旧预测不能补记为前瞻证据。",
        "eligible_same_signal_day": "可以登记；同一模型、版本和日期保留首次记录。",
    }
    return {"eligible": timing["forward_eligible"], "reason": reasons[timing["status"]]}


def read_workbench_status() -> dict:
    """Compose each independent state without generating artifacts or provider calls."""
    result = {"data": None, "report": None, "shadow": None, "accounts": None, "errors": {}}
    readers = {
        "data": inspect_data_status,
        "report": lambda: (
            snapshot.report if (snapshot := load_validated_latest_snapshot()) else None
        ),
        "shadow": lambda: [asdict(row) for row in summarize_forward_shadow(DEFAULT_SHADOW_ROOT)],
        "accounts": list_accounts,
    }
    for name, reader in readers.items():
        try:
            result[name] = reader()
        except Exception as exc:
            result["errors"][name] = public_error(exc)
    result["token_available"] = bool(os.environ.get("TUSHARE_TOKEN"))
    result["shadow_admission"] = shadow_admission(result["report"])
    return result


def register_current_shadow():
    now = datetime.now(SHANGHAI)
    snapshot = load_validated_latest_snapshot()
    admission = shadow_admission(snapshot.report if snapshot else None, now)
    if not admission["eligible"]:
        raise ValueError(admission["reason"])
    return generate_forward_shadow(now=now)


def csv_template(columns) -> bytes:
    """Templates contain headers only; never supply pretend real account/fill facts."""
    return (",".join(columns) + "\n").encode("utf-8-sig")


def preview_matches(preview, source_sha, account, saved_sha, saved_account_fingerprint) -> bool:
    return bool(
        preview
        and preview.get("account_id") == account["account_id"]
        and source_sha == saved_sha
        and account["account_fingerprint"] == saved_account_fingerprint
    )
