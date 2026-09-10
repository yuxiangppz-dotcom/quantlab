"""Read-only evidence diagnostic, without an account performance method."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from quantlab.daily.service import PROJECT_ROOT, SHANGHAI
from quantlab.data.storage import ParquetStorage
from quantlab.personal.account import DEFAULT_ACCOUNT_ROOT, load_account
from quantlab.personal.historical import replay_account_at
from quantlab.personal.tracking_core import (
    _decode_journal,
    _load_journal,
    load_effective_account,
)
from quantlab.personal.valuation_checkpoint import (
    _canonical_hash,
    _sha256_file,
    load_latest_valuation_checkpoint,
)


def inspect_performance_inputs(
    account_id: str,
    *,
    account_root: Path = DEFAULT_ACCOUNT_ROOT,
    storage: ParquetStorage | None = None,
) -> dict:
    """Explain missing evidence for a future daily-close account return method.

    15:00 Shanghai is an explicit valuation convention for this diagnostic, not
    an assertion that after-hours facts cannot exist. No checkpoint is rewritten.
    A historical economic restatement is not a historical knowledge assertion.
    """
    storage = storage or ParquetStorage(PROJECT_ROOT / "data/canonical")
    checked_at = datetime.now(SHANGHAI)
    opening = load_account(account_id, account_root=account_root)
    journal = _load_journal(opening, account_root)
    fills, flows = _decode_journal(journal)
    if any(event.account_id != account_id for event in (*fills, *flows)):
        raise ValueError("performance input journal event account binding mismatch")
    journal_fp = journal["journal_fingerprint"] if journal else None
    checks = []

    def check(key, status, detail):
        checks.append({"check_id": key, "status": status, "detail": detail})

    check(
        "fill_execution_timing",
        "pass" if all(fill.is_performance_timing_eligible for fill in fills) else "unknown",
        "已录入成交都有真实发生时间" if fills else "当前起点下没有已录入成交",
    )
    if checks[-1]["status"] == "unknown":
        checks[-1]["detail"] = "旧成交缺少真实发生时刻；录入时间不能替代成交时间"
    check(
        "external_cash_flow_timing",
        "pass" if all(flow.is_performance_timing_eligible for flow in flows) else "unknown",
        "已录入出入金都有真实生效时间" if flows else "当前起点下没有已录入出入金",
    )
    if checks[-1]["status"] == "unknown":
        checks[-1]["detail"] = "旧出入金缺少真实生效时刻，不能精确分离资金进出和投资收益"
    history = replay_account_at(
        account_id,
        checked_at,
        opening_fingerprint=opening["account_fingerprint"],
        account_root=account_root,
        storage=storage,
    )
    check(
        "economic_replay",
        "pass" if history["status"] == "complete_economic_replay" else "blocked",
        "当前已录入事实可按经济时间回放；不证明历史上当时已经获知这些事实"
        if history["status"] == "complete_economic_replay"
        else "历史回放被时间或交易日历缺口阻止，请先查看历史回放提示",
    )
    saved = load_latest_valuation_checkpoint(account_id, account_root=account_root)
    value = saved[1] if saved else None
    check(
        "valuation_checkpoint",
        "pass" if saved else "unknown",
        "已保存估值的指纹和金额恒等式校验通过" if saved else "尚未保存账户估值",
    )
    effective = None
    if history["status"] == "complete_economic_replay":
        effective = load_effective_account(account_id, account_root=account_root, storage=storage)
    if value is not None and effective is not None:

        def quantities(rows):
            return sorted((row["instrument_id"], row["quantity"]) for row in rows)

        matches = (
            value["account_fingerprint"] == effective["account_fingerprint"]
            and value["cash_fen"] == history["cash_fen"]
            and quantities(value["positions"]) == quantities(history["positions"])
        )
        check(
            "valuation_account_binding",
            "pass" if matches else "blocked",
            "估值与当前已录入的现金和持仓一致"
            if matches
            else "估值对应此前或不一致的账户状态，请先保存新的估值",
        )
        close_at = datetime.combine(date.fromisoformat(value["price_date"]), time(15), SHANGHAI)
        latest_at = datetime.fromisoformat(history["latest_included_economic_at"])
        compatible = latest_at <= close_at <= checked_at
        check(
            "daily_close_cutoff",
            "pass" if compatible else "blocked",
            "账户经济事实不晚于本检查采用的北京时间 15:00 收盘估值时点"
            if compatible
            else "账户包含收盘后发生的事实，或收盘时点尚未到来，不能当成该日收盘账户",
        )
    else:
        check("valuation_account_binding", "unknown", "需要可回放账户和有效估值才能核对状态")
        check("daily_close_cutoff", "unknown", "需要账户真实发生时间与有效估值日期才能核对收盘时点")
    price_sha = value["evidence"]["daily_bar_partition_sha256"] if value else None
    if value is None:
        check("raw_price_source", "unknown", "尚无估值价格来源")
    elif not value["positions"]:
        check("raw_price_source", "pass", "该估值仅含现金，未使用证券价格")
    else:
        partition = storage.daily_bars_path(date.fromisoformat(value["price_date"]))
        if not partition.exists():
            check("raw_price_source", "unknown", "估值引用的原始日线分区目前不可读取")
        else:
            price_matches = _sha256_file(partition) == price_sha
            check(
                "raw_price_source",
                "pass" if price_matches else "blocked",
                "原始日线分区与估值保存时的字节指纹一致"
                if price_matches
                else "估值保存后原始价格分区发生变化，不能沿用此前的价格验证",
            )
    check(
        "cash_flow_boundary_valuations",
        "unknown" if flows else "pass",
        "已录入出入金前后的独立估值尚未建立，不能据此计算精确时间加权收益"
        if flows
        else "当前起点下没有已录入出入金，不需要这些记录的边界估值",
    )
    held_securities = bool(opening["positions"] or fills)
    check(
        "corporate_action_completeness",
        "unknown" if held_securities else "pass",
        "持有期间的分红、送转、配股和相关税款尚未完成账户入账与完整性核对"
        if held_securities
        else "当前起点及已录入流水均未涉及证券；这不证明券商记录已经完整导入",
    )
    check(
        "broker_reconciliation",
        "unknown",
        "尚无与券商对账单核对后的完整记录证明，含费用、税款和利息",
    )
    check("performance_method", "blocked", "尚未启用经过验证的账户收益计算方法及明确的起止估值区间")

    after = load_account(account_id, account_root=account_root)
    after_journal = _load_journal(after, account_root)
    after_fp = after_journal["journal_fingerprint"] if after_journal else None
    after_saved = load_latest_valuation_checkpoint(account_id, account_root=account_root)
    saved_fp = value["checkpoint_fingerprint"] if value else None
    after_saved_fp = after_saved[1]["checkpoint_fingerprint"] if after_saved else None
    if (
        after["account_fingerprint"] != opening["account_fingerprint"]
        or after_fp != journal_fp
        or history["evidence"]["journal_fingerprint"] != journal_fp
        or after_saved_fp != saved_fp
    ):
        raise ValueError("performance input evidence changed during inspection; retry")
    result = {
        "schema": "quantlab_performance_input_diagnostic_v1",
        "account_id": account_id,
        "checked_at": checked_at.isoformat(),
        "scope": "current_opening_and_recorded_journal_for_future_daily_close_method",
        "valuation_convention": "15:00 Asia/Shanghai; economic restatement, not known-at-time",
        "status": "blocked",
        "performance_eligible": False,
        "performance_claim": False,
        "broker_order_authority": False,
        "checks": checks,
        "unresolved_check_ids": [row["check_id"] for row in checks if row["status"] != "pass"],
        "evidence": {
            "opening_account_fingerprint": opening["account_fingerprint"],
            "journal_fingerprint": journal_fp,
            "historical_state_fingerprint": history["state_fingerprint"],
            "checkpoint_fingerprint": saved_fp,
        },
        "limitations": [
            "passing an individual input check does not establish investment returns",
            "absence of a recorded event does not prove the broker history is complete",
        ],
    }
    result["diagnostic_fingerprint"] = _canonical_hash(result)
    return result
