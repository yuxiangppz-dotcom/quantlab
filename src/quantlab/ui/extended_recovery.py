"""Read-only presentation keeps the failed parent and derived recovery distinct."""

import json

import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_recovery import load_report
from quantlab.research.extended_recovery_review import read_verification
from quantlab.ui.workbench import public_error


def render_extended_recovery():
    try:
        report = load_report(PROJECT_ROOT)
        if report is None:
            return
        st.subheader("旧模型恢复验证：独立记录")
        st.caption("原全年批次仍保留失败记录。这里单独验证修复后的旧模型复用，不增加训练名额。")
        a, b, c = st.columns(3)
        a.metric("恢复完成的旧模型", f"{report['completed_recovery_jobs']} / 6")
        b.metric("本次新增训练", str(report["additional_fit_attempts"]))
        c.metric("合并覆盖模型", f"{report['combined_model_references']} / 104")
        st.write(
            f"本次核验预测 {report['prediction_rows']:,} 条："
            f"保留原分数 {report['reused_rows']:,} 条，"
            f"沿用原模型新增计算 {report['newly_scored_rows']:,} 条。"
        )
        proof = read_verification(PROJECT_ROOT, report)
        if report["status"] == "failed":
            st.error("恢复验证也遇到失败，已封存本次记录，后续工作停止。")
        elif report["status"] == "checkpoint":
            st.info("恢复验证尚未完成，已完成记录与中断状态分别保留。")
        elif proof is None:
            st.info("六个旧模型的恢复计算已完成，等待全量独立复核。")
        else:
            st.success("314,804条预测已逐条独立复算一致；原模型、分数和失败记录均保持原样。")
        st.warning(
            f"当前合并覆盖 {report['combined_model_references']} / 104 个模型引用；"
            "剩余4个新模型名额尚未执行。完整年度频率对照仍未完成，暂不判断每周训练是否更好。"
            "历史全市场覆盖尚未证实，本页结果也不是组合收益或20%回撤达标证据。"
        )
        st.download_button(
            "下载旧模型恢复记录",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_旧模型恢复记录.json",
            "application/json",
            on_click="ignore",
        )
        if proof is not None:
            st.download_button(
                "下载旧模型恢复独立复核",
                json.dumps(proof, ensure_ascii=False, indent=2),
                "QuantLab_旧模型恢复独立复核.json",
                "application/json",
                on_click="ignore",
            )
            doc = PROJECT_ROOT / "docs/extended_recovery_findings_zh.md"
            if doc.exists() and proof["fingerprint"] in doc.read_text(encoding="utf-8"):
                st.download_button(
                    "下载旧模型恢复中文说明",
                    doc.read_text(encoding="utf-8"),
                    "QuantLab_旧模型恢复中文说明.txt",
                    "text/plain",
                    on_click="ignore",
                )
    except Exception as exc:
        st.error(f"旧模型恢复证据无法校验：{public_error(exc)}")
