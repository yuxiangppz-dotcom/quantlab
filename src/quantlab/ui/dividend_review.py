"""Chinese read-only raw acquisition progress and receipt downloads."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.dividend_acquisition import CONFIG
from quantlab.data.dividend_review import read_acquisition, read_verification
from quantlab.ui.workbench import public_error

STATUS = {
    "failed": "请求失败（详见逐股票状态）",
    "nonempty": "已取回非空响应",
    "empty": "已取回空响应",
    "pending": "尚未请求",
    "saturated": "达到行数上限或疑似截断",
    "body_limit": "响应达到字节上限",
    "interrupted_unknown": "中断，结果未确证",
    "transport_error": "连接或读取失败",
    "http_transient": "服务器暂时失败",
    "rate_limited": "接口限频",
    "permission_error": "认证或权限失败",
    "schema_error": "响应结构不符合要求",
    "secret_echo": "响应涉及凭据，已脱敏并停止",
    "http_error": "HTTP响应异常",
    "provider_error": "数据源返回错误",
}


FIELD_NAMES = {
    "end_date": "方案所属期末",
    "ann_date": "公告日期",
    "record_date": "股权登记日",
    "ex_date": "除权除息日",
    "pay_date": "现金发放日",
    "div_listdate": "红股上市日",
    "imp_ann_date": "实施公告日",
    "base_date": "基准股本日",
    "base_share": "基准股本（万股）",
    "stk_div": "合计送转（每股）",
    "stk_bo_rate": "送股（每股）",
    "stk_co_rate": "转增（每股）",
    "cash_div": "数据源税后现金（元/股）",
    "cash_div_tax": "税前现金（元/股）",
    "div_proc": "方案阶段",
}


def render_dividend_acquisition():
    st.subheader("分红送转原始数据：补数进度")
    try:
        result = read_acquisition(PROJECT_ROOT)
        if result is None:
            st.info("补数任务尚无已保存进度。")
            return
        kind, report = result
        counts = report["status_counts"]
        visible_counts = {
            key: counts.get(key, 0)
            for key in (
                "nonempty",
                "empty",
                "pending",
                "saturated",
                "body_limit",
                "interrupted_unknown",
            )
        }
        visible_counts["failed"] = sum(
            value for key, value in counts.items() if key not in visible_counts
        )
        a, b, c = st.columns(3)
        a.metric("固定目标股票", f"{report['instrument_count']:,} 只")
        b.metric("已取回非空响应", f"{counts.get('nonempty', 0):,} 只")
        c.metric("返回历史记录", f"{report['returned_rows']:,} 条")
        st.caption(f"最近记录时间（含时区）：{report['at']}；这是保存的状态，不代表进程仍在运行。")
        st.dataframe(
            pd.DataFrame(
                [
                    {"状态": STATUS.get(key, key), "股票数": value}
                    for key, value in visible_counts.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.warning(
            "取回数据尚不代表事件历史完整：空响应不等于没有分红，非空响应也不证明全部版本齐全。"
            "这些资料是现在取得的历史记录，尚不能作为实际到账、股息税或当年已知信息的证明。"
        )
        if kind == "progress":
            st.info("目前为阶段进度，完整响应校验和本轮封存报告尚未完成。")
        else:
            st.write(f"累计请求 {report['attempts']:,} 次，其中重试 {report['retries']:,} 次。")
            verification = read_verification(PROJECT_ROOT, report)
            if verification is not None:
                charged = verification["conservative_body_budget_bytes"] / 1024**2
                st.write(f"独立复核已完成；响应体预算按保守口径计入 {charged:.2f} MiB。")
                if not verification["recorded_budget_is_conservative"]:
                    st.warning(
                        "原下载报告低估了失败请求可能消耗的字节；独立复核已补计完整预留量。"
                        "原始报告保留，校正说明可下载；失败时实际收到的总字节仍无法确证。"
                    )
                st.download_button(
                    "下载独立复核与预算校正",
                    json.dumps(verification, ensure_ascii=False, indent=2),
                    "QuantLab_分红补数独立复核.json",
                    "application/json",
                )
            stop = report["stop_reason"]
            if stop == "wakeup_checkpoint":
                st.info("本段已保存检查点；下一轮从剩余请求继续，成功响应不重复下载。")
            elif stop == "request_scope_exhausted":
                st.info(
                    "本批允许的请求已结束。请按状态区分取回、失败和截断；完整事件与账户权益仍待核对。"
                )
            else:
                st.error(f"本段已停止：{STATUS.get(stop, stop)}。已有结果与失败记录保留。")
            with st.expander("查看字段缺口与研究范围"):
                profile = report["observation_profile"]
                st.write(
                    "请求保留每只股票返回的全部历史方案；"
                    "现金、送股、转增和基准股本保持官方原始单位，未推算账户权益。"
                )
                st.write(
                    f"至少一个权益日期位于2023-01-04至2026-09-10的记录："
                    f"{profile.get('rows_with_date_in_required_window', 0):,} 条。"
                    "这只说明日期可能相关，不代表持仓符合分红资格。"
                )
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"返回方案阶段": key, "记录数": value}
                            for key, value in profile.get("status_counts", {}).items()
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
                st.caption("下表统计全部方案阶段；预案没有登记或发放日期，不能一概判为下载错误。")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "字段": FIELD_NAMES.get(key, key),
                                "缺失记录": value,
                                "格式或数值待核对": profile.get("malformed_counts", {}).get(key, 0),
                            }
                            for key, value in profile.get("null_counts", {}).items()
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
                st.write(
                    f"完全相同的重复记录：{profile.get('exact_duplicate_rows', 0):,} 条；"
                    f"候选方案标识冲突：{profile.get('candidate_identity_conflicts', 0):,} 组。"
                    "全部保留，不自动合并或累加成到账金额。"
                )
                st.write(
                    f"标为实施且税前现金为正、但缺少有效发放日期："
                    f"{profile.get('implemented_positive_cash_without_valid_pay_date', 0):,} 条。"
                    "数据源的税后字段不能代替你账户的实际税额。"
                )
                st.write(
                    "先前的费用输入审计继续保留原始时点结论；完整扣费收益与20%回撤目标仍待验证。"
                )
        st.caption("下方费用输入审计是补数前封存的快照；本页单独显示新取回的原始资料。")
        findings = PROJECT_ROOT / "docs/dividend_acquisition_findings_zh.md"
        if findings.exists():
            st.download_button(
                "下载补数中文解读",
                findings.read_bytes(),
                "QuantLab_分红补数解读.md",
                "text/markdown",
            )
        st.download_button(
            "下载公司行为补数报告",
            json.dumps(report, ensure_ascii=False, indent=2),
            "QuantLab_公司行为补数报告.json",
            "application/json",
        )
        frame = pd.DataFrame(report["per_code"])
        frame["status"] = frame["status"].map(lambda value: STATUS.get(value, value))
        st.download_button(
            "下载逐股票补数状态",
            frame.to_csv(index=False).encode("utf-8-sig"),
            "QuantLab_逐股票补数状态.csv",
            "text/csv",
        )
        st.download_button(
            "下载补数范围与来源",
            (PROJECT_ROOT / CONFIG).read_bytes(),
            "QuantLab_补数范围与来源.json",
            "application/json",
        )
    except Exception as exc:
        st.error(f"补数证据无法校验：{public_error(exc)}")
