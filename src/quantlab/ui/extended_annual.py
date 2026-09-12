"""One current annual result, with the original failure and two later stages preserved."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.extended_completion_assembly import read_assembly
from quantlab.research.extended_completion_protocol import OUTPUT, PARENT, RECOVERY
from quantlab.research.round2_dataset import sealed_read
from quantlab.ui.extended_annual_results import render_verified_results
from quantlab.ui.workbench import public_error


def render_annual():
    if not (PROJECT_ROOT / OUTPUT / "combined/report.json").exists():
        return False
    try:
        report = read_assembly(PROJECT_ROOT)
        st.subheader("全年训练频率对照：完整结果")
        a, b, c = st.columns(3)
        a.metric("完成模型引用", "104 / 104")
        b.metric("原预算内新训练", "98 / 98")
        c.metric("保存的预测记录", "4,320,562")
        st.caption(
            "2025年9月1日至2026年8月31日，242个交易日。固定Alpha158与两类模型，对照每周重训和月内复用。"
        )
        render_verified_results(report)
        out = PROJECT_ROOT / OUTPUT / "combined"
        diagnostics = sealed_read(out / "diagnostics.json")
        for key, label in (
            ("weekly", "各周诊断"),
            ("monthly", "各自然月诊断"),
            ("paired", "非共享周频率对照"),
        ):
            data = pd.DataFrame(diagnostics[key])
            st.download_button(
                f"下载完整年度{label}",
                data.to_csv(index=False).encode("utf-8-sig"),
                f"QuantLab_完整年度{label}.csv",
                "text/csv",
                on_click="ignore",
            )
        daily = pd.read_parquet(out / "daily_policies.parquet", use_threads=False)
        st.download_button(
            "下载完整年度逐日诊断",
            daily.to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_完整年度逐日诊断.csv",
            "text/csv",
            on_click="ignore",
        )
        st.download_button(
            "下载完整年度证据清单",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_完整年度证据清单.json",
            "application/json",
            on_click="ignore",
        )
        st.warning(
            "完整回放不等于可交易策略：本页不包含组合净收益、真实费用与成交验证、20%回撤达标结论或自动交易权限。"
        )
        with st.expander("查看保留的原失败记录与两次后续处理"):
            st.write(
                "原全年批次在94次新训练后停止，失败记录没有改写。随后单独恢复六个旧引用，再完成原计划剩余四个新模型；总预算仍为98次。"
            )
            rows = []
            for label, base, filename in [
                ("原全年批次（保留失败）", PARENT, "QuantLab_原全年失败记录.json"),
                ("旧模型恢复（新增训练0）", RECOVERY, "QuantLab_旧模型恢复记录.json"),
                ("剩余四个原定名额", OUTPUT, "QuantLab_四个原定名额完成记录.json"),
            ]:
                record = sealed_read(PROJECT_ROOT / base / "report.json")
                rows.append(
                    {
                        "阶段": label,
                        "状态": record["status"],
                        "记录时间UTC": record["at"],
                        "证据标识": record["fingerprint"],
                    }
                )
                st.download_button(
                    f"下载{label}",
                    json.dumps(record, ensure_ascii=False, indent=2),
                    filename,
                    "application/json",
                    on_click="ignore",
                )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        return True
    except Exception as exc:
        st.error(f"完整年度证据无法校验：{public_error(exc)}")
        return True
