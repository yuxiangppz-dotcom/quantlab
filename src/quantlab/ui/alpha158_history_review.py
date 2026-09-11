"""Chinese history staging coverage, with no provider, fitting or data writes."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.alpha158_staging import OUTPUT_DIRECTORY, staging_progress
from quantlab.ui.workbench import public_error


def render_alpha158_history():
    st.subheader("Alpha158 历史数据：滚动训练准备")
    out = PROJECT_ROOT / OUTPUT_DIRECTORY
    try:
        report = staging_progress(out)
        if report is None:
            st.info("历史因子批次尚未开始生成。五代码验证结果见下方。")
            return
        complete = report["status"] == "complete"
        if complete:
            st.success("历史因子数据已生成并校验。本生成批次不含训练；滚动模型结果见上方。")
        else:
            st.info(
                f"已保存 {report['completed_batches']} / {report['expected_batches']} 个因子批次。"
                "完整结果尚未就绪；已完成部分保留，后续从检查点继续。"
            )
            if report["terminal_failure_recorded"]:
                st.warning("已记录生成失败，需先核对失败原因。当前不能视为完整训练数据。")
        a, b, c = st.columns(3)
        a.metric("历史观察到的股票代码", f"{report['instrument_count']:,}")
        b.metric("158 项全部可用的记录", f"{report['target_all158_usable_rows']:,}")
        c.metric("本批新增模型训练", report["new_fit_attempts"])
        st.caption("2020-01-01 至 2026-09-10 · 向前预留 120 个交易日 · 每批最多 32 个代码")
        st.write(
            "股票范围来自历史日线中实际出现的代码，未按当前上市状态或未来收益是否存在筛选。"
            "缺失日期与代码无效期仍保留。历史原始记录可能经过修订，这批覆盖不能证明"
            "历史全市场数据完整，也不等于当年的及时预测。"
        )
        if report["years"]:
            st.dataframe(
                pd.DataFrame(report["years"])[
                    ["year", "grid_rows", "active_rows", "observed_rows", "all158_usable_rows"]
                ].rename(
                    columns={
                        "year": "年份",
                        "grid_rows": "代码日期位置",
                        "active_rows": "代码有效期记录",
                        "observed_rows": "原始日线记录",
                        "all158_usable_rows": "158 项全部可用记录",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
        with st.expander("各因子可用数量与排除原因"):
            st.dataframe(
                pd.DataFrame(report["features"]).rename(
                    columns={
                        "name": "因子",
                        "target_native_finite": "原生有限值记录",
                        "target_usable_rows": "完整历史窗口下可用记录",
                    }
                ),
                hide_index=True,
                width="stretch",
                height=280,
            )
            reasons = {
                "mapped_inputs_available": "六项映射输入可用（仍需检查历史窗口）",
                "inactive_code_or_lifecycle": "代码无效期",
                "missing_daily": "缺失日线",
                "invalid_or_missing_factor": "复权因子缺失或异常",
                "invalid_ohlc": "价格异常",
                "nonpositive_or_missing_volume": "成交量为零、负值或缺失",
                "nonpositive_or_missing_amount": "成交金额为零、负值或缺失",
                "derived_vwap_outside_bar": "推导均价超出当日高低价",
                "unknown_lifecycle": "代码有效期未知",
            }
            st.dataframe(
                pd.DataFrame(
                    [
                        {"输入检查": reasons.get(key, key), "记录数": value}
                        for key, value in report["target_exclusions"].items()
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        if complete:
            st.caption("本页核对摘要；整批文件与可用掩码的独立校验结果见中文报告。")
            st.caption(
                f"文件占用约 {report['generated_bytes_before_receipt'] / 1024**3:.2f} GiB；"
                f"进程内存峰值 {report['peak_rss_bytes'] / 1024**3:.2f} GiB；"
                f"累计运行约 {report['cumulative_wake_seconds'] / 60:.1f} 分钟。"
                "上限为 16 GiB 新文件、6 GiB 进程内存和两条计算线程。"
            )
            st.download_button(
                "下载历史 Alpha158 完整报告",
                (out / "report.json").read_bytes(),
                "QuantLab_Alpha158历史数据.json",
                "application/json",
            )
            findings = PROJECT_ROOT / "docs/alpha158_history_findings_zh.md"
            if findings.exists():
                st.download_button(
                    "下载历史 Alpha158 中文解读",
                    findings.read_bytes(),
                    "QuantLab_Alpha158历史解读.md",
                    "text/markdown",
                )
        st.caption(
            "这批封存因子供上方固定的线性与 LightGBM 滚动比较使用，全部结果保留。"
            "因子数据可用不代表可以成交；完整费用、历史规则和公司行为记账仍需补齐。"
            "本批尚未评价净收益或验证 20% 最大回撤目标。"
        )
    except Exception as exc:
        st.error(f"历史因子批次无法校验：{public_error(exc)}")
