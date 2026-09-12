"""Explain the saved diagnostics and the historical pilot preparation snapshot."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.weekly_plan_run import read_report, read_verification
from quantlab.ui.workbench import public_error


def render_weekly_plan():
    st.subheader("策略月度表现与每周训练准备")
    try:
        loaded = read_report(PROJECT_ROOT)
        if loaded is None:
            st.info("周更训练日历与标签成熟条件的准备报告尚未交付。")
            return
        report, monthly, weekly = loaded
        verification = read_verification(PROJECT_ROOT, report, weekly)
        a, b, c = st.columns(3)
        a.metric("已有月度诊断", f"{report['monthly_rows']} 组")
        b.metric("计划候选训练", "6 次")
        c.metric("准备时训练快照", f"{report['new_fit_attempts']} 次")
        st.write(
            "使用全部已保存月份，比较岭回归和LightGBM的排序表现。"
            "RankIC是排序相关性，不是收益率；前20名变化也不是实际交易换手。"
        )
        month_table = pd.DataFrame(monthly["rows"])[
            [
                "model",
                "month",
                "market_days",
                "score_rows",
                "evaluation_rows",
                "rank_ic_days",
                "mean_rank_ic",
                "positive_ic_day_fraction",
                "mean_rank_stability",
                "mean_top20_membership_change",
            ]
        ].rename(
            columns={
                "model": "模型",
                "month": "信号月份",
                "market_days": "交易日",
                "score_rows": "预测记录",
                "evaluation_rows": "可评价标签",
                "rank_ic_days": "有效排序评价日",
                "mean_rank_ic": "日均RankIC",
                "positive_ic_day_fraction": "正RankIC日占比",
                "mean_rank_stability": "相邻日排名稳定性",
                "mean_top20_membership_change": "前20名成员变化",
            }
        )
        month_table["模型"] = month_table["模型"].replace(
            {"ridge": "岭回归", "lightgbm": "LightGBM"}
        )
        month_table = month_table.sort_values(["信号月份", "模型"], ascending=[False, True])
        st.dataframe(month_table, hide_index=True, width="stretch")
        st.caption(
            "保留全部月份与空值。2026年9月仅截至10日；按信号月份分组，标签可能跨月，"
            "仍遵守原年度评价边界。数据已经被观察，不能重新称为未见样本或前瞻。"
        )
        st.write("周更试验：每周训练，与首周训练后连续三周保持不变的版本比较。")
        week_table = pd.DataFrame(weekly["rows"])[
            [
                "week",
                "train_start",
                "train_end",
                "prediction_start",
                "prediction_end",
                "train_rows",
                "train_unmatured_or_unknown_end",
                "prediction_rows",
                "evaluation_rows",
            ]
        ].rename(
            columns={
                "week": "周次",
                "train_start": "训练起点",
                "train_end": "训练截止",
                "prediction_start": "预测起点",
                "prediction_end": "预测终点",
                "train_rows": "可训练样本",
                "train_unmatured_or_unknown_end": "标签未成熟或终点未知",
                "prediction_rows": "保留预测成员",
                "evaluation_rows": "可评价成员",
            }
        )
        st.dataframe(week_table, hide_index=True, width="stretch")
        st.warning(
            "这是预先固定的六次训练方案；实际训练进度见上方周更对照。"
            "首周两种更新频率共享模型，第二、三周才能比较更新效果。"
            "六次上限包含失败尝试，不追加搜索参数，也不自动替换策略。"
        )
        st.caption(
            "训练只使用截止日已成熟的五交易日标签；每周重新拟合训练期预处理。"
            "分红、成交、完整税费与回撤目标仍须另行评价；2026年弱化的原因尚未证实。"
        )
        for label, value, filename, mime in (
            (
                "下载周更准备报告",
                json.dumps(report, ensure_ascii=False, indent=2),
                "QuantLab_周更准备报告.json",
                "application/json",
            ),
            (
                "下载全部月份表现",
                month_table.to_csv(index=False).encode("utf-8-sig"),
                "QuantLab_全部月份表现.csv",
                "text/csv",
            ),
            (
                "下载周次与样本数量",
                week_table.to_csv(index=False).encode("utf-8-sig"),
                "QuantLab_周次与样本.csv",
                "text/csv",
            ),
            (
                "下载固定候选训练方案",
                json.dumps(weekly, ensure_ascii=False, indent=2),
                "QuantLab_候选训练方案.json",
                "application/json",
            ),
        ):
            st.download_button(label, value, filename, mime, on_click="ignore")
        findings = PROJECT_ROOT / "docs/weekly_candidate_plan_findings_zh.md"
        if verification is not None:
            st.success("全部月份与三个周次样本已独立核对；此处保留准备阶段的零次拟合快照。")
            st.download_button(
                "下载周更准备独立复核",
                json.dumps(verification, ensure_ascii=False, indent=2),
                "QuantLab_周更准备独立复核.json",
                "application/json",
                on_click="ignore",
            )
        if findings.exists():
            st.download_button(
                "下载周更准备中文解读",
                findings.read_text(encoding="utf-8"),
                "QuantLab_周更准备解读.txt",
                "text/plain",
                on_click="ignore",
            )
    except Exception as exc:
        st.error(f"周更准备证据无法校验：{public_error(exc)}")
