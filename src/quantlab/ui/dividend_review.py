"""Chinese read-only raw acquisition progress and receipt downloads."""

import json

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.data.dividend_acquisition import CONFIG
from quantlab.data.dividend_review import read_acquisition
from quantlab.ui.workbench import public_error

STATUS = {
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


def render_dividend_acquisition():
    st.subheader("分红送转原始数据：补数进度")
    try:
        result = read_acquisition(PROJECT_ROOT)
        if result is None:
            st.info("补数任务尚无已保存进度。")
            return
        kind, report = result
        counts = report["status_counts"]
        a, b, c = st.columns(3)
        a.metric("固定目标股票", f"{report['instrument_count']:,} 只")
        b.metric("已取回非空响应", f"{counts.get('nonempty', 0):,} 只")
        c.metric("返回历史记录", f"{report['returned_rows']:,} 条")
        st.caption(f"最近记录时间（含时区）：{report['at']}；这是保存的状态，不代表进程仍在运行。")
        st.dataframe(
            pd.DataFrame(
                [{"状态": STATUS.get(key, key), "股票数": value} for key, value in counts.items()]
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
            stop = report["stop_reason"]
            if stop == "wakeup_checkpoint":
                st.info("本段已保存检查点；下一轮从剩余请求继续，成功响应不重复下载。")
            elif stop != "request_scope_exhausted":
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
                            {
                                "字段": key,
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
                    "先前的费用输入审计继续保留原始时点结论；完整扣费收益与20%回撤目标仍待验证。"
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
