"""Read-only, sealed annual replay checkpoints and complete descriptive outcomes."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_frequency import load_status
from quantlab.research.extended_frequency_protocol import OUTPUT
from quantlab.research.round2_dataset import sealed_read
from quantlab.ui.extended_annual import render_annual
from quantlab.ui.extended_recovery import render_extended_recovery
from quantlab.ui.workbench import public_error


def render_extended_frequency():
    if render_annual():
        return
    st.subheader("全年训练频率对照：执行进度")
    try:
        report = load_status(PROJECT_ROOT)
        if report is None:
            st.info("尚无可核验的扩展执行快照。下方保留事先固定的全年规划。")
            return
        a, b, c, d = st.columns(4)
        a.metric("累计新训练名额", f"{report['cumulative_fit_attempts']} / 98")
        b.metric("完成的新模型", str(report["completed_new_fits"]))
        c.metric("完成旧模型复用", f"{report['completed_reuse_jobs']} / 6")
        d.metric("失败或中断", str(report["failed_or_interrupted_jobs"]))
        stamp = pd.Timestamp(report["at"]).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
        st.caption(
            f"已核验快照：{stamp}（北京时间）。当前正在运行的工作可能尚未记入；"
            "刷新页面查看下一份完成快照。关机后沿用同一累计预算，失败名额不会重置。"
        )
        st.write(
            "信号区间：2025年9月1日至2026年8月31日，52个周次、242个交易日。"
            "比较每周重新训练与每月首次周更后复用模型，固定Alpha158、Ridge和LightGBM。"
        )
        st.write(
            f"实际调用训练 {report['actual_fit_invocations']} 次；"
            f"已完成预测记录 {report['prediction_rows_completed']:,} 条，"
            f"其中沿用旧分数 {report['reused_prediction_rows']:,} 条，"
            f"新增计算 {report['newly_scored_rows']:,} 条。"
        )
        if report["status"] == "failed":
            st.error("原全年批次存在失败或中断，已停止该批后续训练，保留原始名额与证据。")
            repair = PROJECT_ROOT / "docs/extended_frequency_reuse_repair_zh.md"
            if repair.exists():
                explanation = repair.read_text(encoding="utf-8")
                if report["fingerprint"] in explanation:
                    st.caption(
                        "旧模型扩展复用出现批量计算差异；原批次仍保留失败。单独的恢复验证结果见下方。"
                    )
                    st.download_button(
                        "下载本轮失败与修复说明",
                        explanation,
                        "QuantLab_全年对照失败与修复.txt",
                        "text/plain",
                        on_click="ignore",
                    )
        elif report["status"] == "checkpoint":
            st.info("全年对照尚未完成，暂不据部分周次判断每周训练是否更好。")
        else:
            st.success("98个新模型与6个旧模型复用均已完成，全年描述性对照已生成。")
            st.caption("这里是执行与预测复算检查。全量独立复核及中文研究结论另行交付。")
        render_extended_recovery()
        st.warning(
            "这是已观察历史的回放，实际训练时间记录在本次运行。预测排序指标不等于收益；"
            "本页没有组合净收益、20%回撤达标结论或自动交易权限。"
        )
        table = pd.DataFrame(
            [
                {
                    "模型周次": value["slot"],
                    "方式": "旧模型复用" if value["mode"] == "reuse" else "新训练",
                    "状态": {"completed": "完成", "failed": "失败", "interrupted": "中断"}[
                        value["status"]
                    ],
                    "训练样本": value.get("summary", {}).get("train_rows"),
                    "预测记录": value.get("summary", {}).get("prediction_rows"),
                    "实际训练时间UTC": value.get("summary", {}).get("trained_at_utc"),
                    "回放开始日": value.get("summary", {}).get("replay_prediction_start"),
                    "说明": value.get("error", ""),
                }
                for value in report["attempts"]
            ]
        )
        with st.expander("查看已完成与中断的模型记录"):
            st.dataframe(table, hide_index=True, width="stretch")
        st.download_button(
            "下载全年执行进度",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_全年执行进度.json",
            "application/json",
            on_click="ignore",
        )
        st.download_button(
            "下载全年已执行模型记录",
            table.to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_全年已执行模型.csv",
            "text/csv",
            on_click="ignore",
        )
        if report["status"] == "complete":
            out = PROJECT_ROOT / OUTPUT
            diagnostics = sealed_read(out / "diagnostics.json")
            for key, label in (
                ("weekly", "各周诊断"),
                ("monthly", "各自然月诊断"),
                ("paired", "非共享周频率对照"),
            ):
                frame = pd.DataFrame(diagnostics[key])
                with st.expander(label):
                    st.dataframe(frame, hide_index=True, width="stretch")
                st.download_button(
                    f"下载全年{label}",
                    frame.to_csv(index=False).encode("utf-8-sig"),
                    f"QuantLab_全年{label}.csv",
                    "text/csv",
                    on_click="ignore",
                )
            daily = pd.read_parquet(out / "daily_policies.parquet", use_threads=False)
            st.download_button(
                "下载全年逐日诊断",
                daily.to_csv(index=False).encode("utf-8-sig"),
                "QuantLab_全年逐日诊断.csv",
                "text/csv",
                on_click="ignore",
            )
    except Exception as exc:
        st.error(f"全年执行证据无法校验：{public_error(exc)}")
