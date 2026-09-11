"""Read-only native factor validation and usable-history coverage."""

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.alpha158_audit import OUTPUT_DIRECTORY, load_audit_report
from quantlab.ui.workbench import public_error


def render_alpha158_review():
    st.subheader("Qlib 因子验证：Alpha158")
    out = PROJECT_ROOT / OUTPUT_DIRECTORY
    if not (out / "report.json").exists():
        st.info("尚无完整的 Alpha158 验证报告。缺少报告不能视为验证通过。")
        return
    try:
        report = load_audit_report(out)
        features = report["features"]
        contract = report["contract"]
        first, second, third = st.columns(3)
        first.metric("已验证原生因子", len(features))
        second.metric("读取方式之间的数值差异", sum(x["mismatch_rows"] for x in features))
        third.metric("有效代码日期记录", f"{report['target_active_rows']:,}")
        st.caption(
            f"Qlib {contract['qlib_version']} · {contract['start']} 至 {contract['end']} · "
            f"固定 {len(contract['instruments'])} 个代码 · "
            f"向前预留 {contract['warmup_sessions']} 个交易日"
        )
        st.write(
            "158 项原生表达式通过文件和内存两种读取方式计算，结果及缺失位置一致。"
            "另有 13 项当日公式独立核对，以及复权、缺失数据和未来数据扰动测试。"
            "这是因子计算与数据边界的验证，还没有检验这些因子能否提高收益。"
        )
        st.info(
            "有数值不等于可用于训练。只有所需历史窗口完整、代码在有效期内、结果有限，"
            "该因子才记为可用。原生结果与可用结果分别保留；当前缺失规则比 Qlib 默认更严格。"
        )
        windows = {x["name"]: x["lookback_sessions"] for x in contract["features"]}
        with st.expander("查看 158 个因子的验证与可用记录", expanded=True):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "因子": item["name"],
                            "计算验证": "通过",
                            "所需前序交易日": windows[item["name"]],
                            "原生有限值记录": item["target_native_finite"],
                            "可用记录": item["target_usable_rows"],
                            "未通过完整性或数值检查": (
                                report["target_active_rows"] - item["target_usable_rows"]
                            ),
                        }
                        for item in features
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=320,
            )
            inactive = report["target_grid_rows"] - report["target_active_rows"]
            st.caption(
                f"目标区间共 {report['target_grid_rows']:,} 个代码日期位置，"
                f"其中 {inactive:,} 个处于代码无效期。"
                "原生有限值可能包含历史窗口不足或无效期中的结果，因此可能多于有效记录。"
            )
        with st.expander("代码变更与输入覆盖"):
            st.dataframe(
                pd.DataFrame(report["target_by_code"]).rename(
                    columns={
                        "instrument_id": "代码",
                        "rows": "目标日期位置",
                        "active_rows": "有效期内记录",
                        "observed_rows": "原始日线记录",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
            st.write(
                "300114.SZ 与 302132.SZ 按 2025-02-17 的代码变更边界分别处理，"
                "不拼接历史。新代码名下较早的原始记录保留供核查，但不用于有效期之前的因子。"
            )
        st.caption(
            "本批新增研究模型训练为 0；没有扩大到全市场。五个固定代码不能代表投资股票池。"
            "历史数据可能经过修订；组合的完整费用、交易规则与公司行为记账仍待补齐，"
            "尚不能评价可成交净收益或验证 20% 最大回撤目标。"
        )
        st.download_button(
            "下载 Alpha158 完整验证报告",
            (out / "report.json").read_bytes(),
            "QuantLab_Alpha158验证.json",
            "application/json",
        )
        findings = PROJECT_ROOT / "docs/alpha158_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载 Alpha158 中文解读",
                findings.read_bytes(),
                "QuantLab_Alpha158解读.md",
                "text/markdown",
            )
        st.caption(f"报告指纹：{report['fingerprint']}。本页核验报告，完整文件另经独立校验。")
    except Exception as exc:
        st.error(f"Alpha158 报告无法校验：{public_error(exc)}")
