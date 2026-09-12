"""Read-only explanation of dividend candidate links to saved strategy windows."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.dividend_event_run import OUTPUT, read_event_verification, read_report
from quantlab.ui.workbench import public_error


def render_dividend_events():
    st.subheader("分红事件与策略持有窗口")
    try:
        report = read_report(PROJECT_ROOT)
        if report is None:
            st.info("事件输入适配尚未完成；原始补数结果保留在下方。")
            return
        verification = read_event_verification(PROJECT_ROOT, report)
        linked_windows = sum(row["windows_with_candidates"] for row in report["window_summary"])
        linked_conflicts = sum(row["conflicting_candidates"] for row in report["window_summary"])
        a, b, c = st.columns(3)
        a.metric("保留原始观察", f"{report['totals']['observations_written']:,} 条")
        b.metric("固定研究窗口", f"{report['totals']['windows_written']:,} 个")
        c.metric("含分红候选的窗口", f"{linked_windows:,} 个")
        st.write("按原策略的股票、日期和持有期限关联分红候选，不改变原有选股与模型分数。")
        st.caption(
            f"共 {report['totals']['links_written']:,} 次候选关联，"
            f"其中冲突候选关联 {linked_conflicts:,} 次。"
            "不同模型和期限的窗口存在重叠，关联次数不能当作独立分红次数。"
        )
        table = []
        for row in report["window_summary"]:
            table.append(
                {
                    "模型": {"ridge": "岭回归", "lightgbm": "LightGBM"}.get(
                        row["model"], row["model"]
                    ),
                    "预期持有交易日": row["horizon"],
                    "研究窗口": row["windows"],
                    "含候选记录的窗口": row["windows_with_candidates"],
                    "候选记录关联次数": row["candidate_observations"],
                    "当时本地已观察的候选": row["observed_candidates_at_signal_close"],
                    "已知退出后发放的候选": row["payments_after_intended_exit"],
                    "起点尚未知的窗口": row["windows_without_known_entry"],
                    "退出尚未知的窗口": row["windows_without_known_exit"],
                }
            )
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")
        st.warning(
            "候选关联不是持仓分红：预案、重复和冲突版本全部保留，不能逐条累加成收益。"
            "未找到候选也不能证明没有公司行为。当前取得的历史记录不能倒填为当时已知信号。"
        )
        st.caption(
            "这些窗口是研究意向；账户持股批次、实际到账、股息税、可成交性和完整成本仍待核对。"
            "现金为正但缺发放日等缺口按字段与日期保留，不替换为零。"
            "退出日期未知时，退出后到账比较保持未知；表中对应次数只统计可比较的窗口。"
        )
        with st.expander("查看版本与缺口统计"):
            undated = report["quality_counts"].get("event_date_relevance_unknown", 0)
            normalized = report["quality_counts"].get("status_normalized", 0)
            st.write(
                f"保留完全重复记录 {report['exact_duplicate_rows']:,} 条；"
                f"全部历史候选冲突 {report['candidate_conflict_groups']:,} 组。"
                f"其中 {report['candidate_conflict_groups_date_window']:,} 组涉及研究日期范围，"
                f"实际固定窗口内冲突关联 {linked_conflicts:,} 次。"
            )
            st.write(f"没有有效权益日期的原始观察 {undated:,} 条，相关性仍未知。")
            st.write(f"状态空格等显式规范化 {normalized:,} 条；原始状态与规范化状态同时保存。")
        st.download_button(
            "下载事件窗口关联报告",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_事件窗口关联.json",
            "application/json",
            on_click="ignore",
        )
        st.download_button(
            "下载事件窗口汇总",
            pd.DataFrame(table).to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_事件窗口汇总.csv",
            "text/csv",
            on_click="ignore",
        )
        st.download_button(
            "下载分红版本待核对清单",
            (PROJECT_ROOT / OUTPUT / "review_cases.csv").read_bytes(),
            "QuantLab_分红版本待核对.csv",
            "text/csv",
            on_click="ignore",
        )
        if verification is not None:
            st.success("全部原始记录与固定窗口已通过独立复核；这不代表收益或持仓权益已验证。")
            st.download_button(
                "下载事件窗口独立复核",
                json.dumps(verification, ensure_ascii=False, indent=2),
                "QuantLab_事件窗口独立复核.json",
                "application/json",
                on_click="ignore",
            )
        findings = PROJECT_ROOT / "docs/dividend_event_inputs_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载事件窗口中文解读",
                findings.read_text(encoding="utf-8"),
                "QuantLab_事件窗口中文解读.txt",
                "text/plain",
                on_click="ignore",
            )
    except Exception as exc:
        st.error(f"事件输入证据无法校验：{public_error(exc)}")
