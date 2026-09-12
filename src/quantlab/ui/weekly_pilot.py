"""Present saved weekly/anchor results without training or strategy promotion."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.weekly_pilot import load_report
from quantlab.research.weekly_pilot_protocol import OUTPUT
from quantlab.research.weekly_pilot_review import read_verification
from quantlab.ui.workbench import public_error


def render_weekly_pilot():
    st.subheader("每周训练与固定模型对照")
    out = PROJECT_ROOT / OUTPUT
    if not (out / "report.json").exists():
        st.info("六次候选训练结果尚未完整交付；预先固定的训练方案见下方。")
        return
    try:
        report = load_report(PROJECT_ROOT)
        verification = read_verification(PROJECT_ROOT, report)
        a, b, c = st.columns(3)
        a.metric("已消耗训练名额", f"{report['cumulative_fit_attempts']} / 6")
        b.metric("完成模型训练", f"{report['completed_fits']} 次")
        c.metric("本段运行时间", f"{report['segment_seconds'] / 60:.1f} 分钟")
        st.write(
            "在2026年8月3日至21日的三个固定周次中，对比每周重新训练与首周训练后保持不变。"
            "首周共享模型，第二、三周才提供更新频率的比较。实际训练发生于2026年9月12日。"
        )
        attempts = []
        for item in report["attempts"]:
            value = item.get("summary", {})
            trained = value.get("trained_at_utc")
            completed_local = (
                pd.Timestamp(trained).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
                if trained
                else None
            )
            attempts.append(
                {
                    "训练名额": item["slot"]
                    .replace("week", "第")
                    .replace("_ridge", "周·岭回归")
                    .replace("_lightgbm", "周·LightGBM"),
                    "状态": {"completed": "完成", "failed": "失败"}.get(
                        item["status"], item["status"]
                    ),
                    "训练截止": value.get("train_end"),
                    "训练样本": value.get("train_rows"),
                    "实际训练完成（北京时间）": completed_local,
                    "拟合用时秒": value.get("fit_seconds"),
                    "保存预测记录": value.get("prediction_rows"),
                }
            )
        st.dataframe(pd.DataFrame(attempts), hide_index=True, width="stretch")
        if report["status"] != "complete":
            st.error("本轮训练未全部完成；失败名额已消耗，不自动重试或产生优胜结论。")
        else:
            table = pd.DataFrame(report["summaries"]).rename(
                columns={
                    "model": "模型",
                    "policy": "更新方式",
                    "week": "周次",
                    "prediction_start": "预测起点",
                    "prediction_end": "预测终点",
                    "score_rows": "预测记录",
                    "evaluation_rows": "可评价标签",
                    "rank_ic_days": "有效评价日",
                    "mean_rank_ic": "日均RankIC",
                    "mean_score_std_population": "分数标准差",
                    "mean_rank_stability": "相邻日排名稳定性",
                    "mean_mean_absolute_percentile_rank_change": "平均排名分位变化",
                    "mean_top20_membership_change": "前20名成员变化",
                }
            )
            table["模型"] = table["模型"].replace({"ridge": "岭回归", "lightgbm": "LightGBM"})
            table["更新方式"] = table["更新方式"].replace(
                {"anchor": "首周固定", "weekly": "每周训练"}
            )
            table = table[
                [
                    "模型",
                    "周次",
                    "更新方式",
                    "预测起点",
                    "预测终点",
                    "预测记录",
                    "可评价标签",
                    "有效评价日",
                    "日均RankIC",
                    "相邻日排名稳定性",
                    "前20名成员变化",
                    "分数标准差",
                    "平均排名分位变化",
                ]
            ]
            paired = pd.DataFrame(report["paired"]).rename(
                columns={
                    "model": "模型",
                    "week": "周次",
                    "paired_days": "成对交易日",
                    "mean_weekly_minus_anchor_rank_ic": "每周减固定的RankIC差",
                    "identical_members_and_labels": "成员与标签一致",
                }
            )
            paired["模型"] = paired["模型"].replace({"ridge": "岭回归", "lightgbm": "LightGBM"})
            paired = paired[
                ["模型", "周次", "成对交易日", "每周减固定的RankIC差", "成员与标签一致"]
            ]
            st.write("第二、三周的成对比较：正值表示当周更新版排序相关性较高。")
            st.dataframe(paired, hide_index=True, width="stretch")
            st.write("全部12组结果，包括首周共享结果与负值：")
            st.dataframe(table, hide_index=True, width="stretch", height=490)
            st.warning(
                "每个模型仅有10个可比较交易日，周更是否更好尚未证实。"
                "RankIC是排序相关性，不是收益率或准确率；前20名成员变化不是实际交易换手。"
                "历史已被观察，不能当作新前瞻证据。"
            )
            st.caption(
                "相邻日指标连接三个周次，保留换模型当天的变化。未定义的相关性保留空值。"
                "这不是完整策略回测，净收益、分红税费与最大回撤20%目标仍待验证。"
            )
            daily = pd.read_parquet(out / "daily_policies.parquet")
            for label, frame, name in (
                ("下载全部周更对照结果", table, "全部对照"),
                ("下载逐日周更对照", daily, "逐日对照"),
                ("下载更新频率成对比较", paired, "成对比较"),
            ):
                st.download_button(
                    label,
                    frame.to_csv(index=False).encode("utf-8-sig"),
                    f"QuantLab_周更{name}.csv",
                    "text/csv",
                    on_click="ignore",
                )
        st.download_button(
            "下载六次训练完整报告",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_周更训练报告.json",
            "application/json",
            on_click="ignore",
        )
        if verification is not None:
            st.success(
                "六次保存模型的253,806条预测已全部复现，训练样本、预处理与逐日对照已独立复核。"
            )
            st.download_button(
                "下载六次训练独立复核",
                json.dumps(verification, ensure_ascii=False, indent=2),
                "QuantLab_周更训练独立复核.json",
                "application/json",
                on_click="ignore",
            )
        findings = PROJECT_ROOT / "docs/alpha158_weekly_pilot_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载周更对照中文解读",
                findings.read_text(encoding="utf-8"),
                "QuantLab_周更对照解读.txt",
                "text/plain",
                on_click="ignore",
            )
    except Exception as exc:
        st.error(f"周更对照证据无法校验：{public_error(exc)}")
