"""Streamlit application for QuantLab Daily."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from quantlab.daily.experiments import load_baseline_view, load_latest_factor_view
from quantlab.daily.service import (
    generate_daily_snapshot,
    inspect_data_status,
    load_latest_snapshot,
)
from quantlab.personal import (
    build_reference_plan,
    create_demo_account,
    import_account_csv,
    list_accounts,
    load_account,
    load_latest_plan,
)

st.set_page_config(page_title="QuantLab Daily", page_icon="📈", layout="wide")


@st.cache_data(show_spinner=False)
def _baseline_view() -> dict | None:
    return load_baseline_view()


@st.cache_data(show_spinner=False)
def _factor_view() -> dict | None:
    return load_latest_factor_view()


def _latest() -> tuple[object | None, pd.DataFrame | None, pd.DataFrame | None]:
    snapshot = load_latest_snapshot()
    if snapshot is None:
        return None, None, None
    return snapshot, pd.read_csv(snapshot.ranking_path), pd.read_csv(snapshot.target_path)


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


st.title("QuantLab Daily v1")
st.caption("本地日频研究与辅助决策工具；研究目标不是券商订单，未知证据不会显示为安全。")

page = st.sidebar.radio(
    "页面",
    ("数据状态与日报", "股票排名与因子", "回测与基准", "账户与参考计划"),
)

if page == "数据状态与日报":
    status = inspect_data_status()
    cols = st.columns(4)
    cols[0].metric("共同数据截止日", status["effective_as_of"] or "不可用")
    cols[1].metric("日历截止日", status["latest_calendar_date"] or "不可用")
    cols[2].metric("状态", status["status"])
    cols[3].metric(
        "缺口交易日",
        status["stale_open_sessions"] if status["stale_open_sessions"] is not None else "未知",
    )
    if status["issues"]:
        st.warning("；".join(status["issues"]))
    st.dataframe(
        pd.DataFrame.from_dict(status["datasets"], orient="index").reset_index(names="dataset"),
        width="stretch",
        hide_index=True,
    )
    if st.button("生成/刷新日频报告", type="primary"):
        with st.spinner("正在读取必要 lookback 并生成报告…"):
            created = generate_daily_snapshot(date.fromisoformat(status["requested_as_of"]))
        st.success("已复用相同输入的缓存" if created.reused else "日报已生成")
        st.rerun()
    snapshot, _, target = _latest()
    if snapshot is None:
        st.info("尚无日报缓存。点击上方按钮后生成；页面刷新本身不会同步或重算。")
    else:
        report = snapshot.report
        st.subheader(f"缓存日报：{report['effective_as_of']}")
        if report["data_status"]["status"] != "complete":
            st.warning(f"这是 {report['effective_as_of']} 的离线结果，不是今天的收盘结果。")
        st.write(report["ranking"]["score_interpretation"])
        st.dataframe(target, width="stretch", hide_index=True)
        st.download_button(
            "下载目标 CSV",
            snapshot.target_path.read_bytes(),
            file_name=snapshot.target_path.name,
            mime="text/csv",
        )
        st.download_button(
            "下载 HTML 日报",
            snapshot.html_path.read_bytes(),
            file_name=snapshot.html_path.name,
            mime="text/html",
        )

elif page == "股票排名与因子":
    snapshot, ranking, _ = _latest()
    if snapshot is None or ranking is None:
        st.info("请先在“数据状态与日报”页面显式生成报告。")
    else:
        st.caption(
            f"模型 {snapshot.report['model']['strategy_id']} · "
            f"状态 {snapshot.report['model']['model_status']} · "
            f"数据 {snapshot.report['effective_as_of']}"
        )
        selected_only = st.toggle("仅看目标股票")
        query = st.text_input("代码或名称筛选")
        shown = ranking
        if selected_only:
            shown = shown[shown["selected"]]
        if query:
            mask = shown["instrument_id"].str.contains(query, case=False, na=False)
            mask |= shown["name"].astype(str).str.contains(query, case=False, na=False)
            shown = shown[mask]
        st.dataframe(shown, width="stretch", hide_index=True, height=650)
        research = _factor_view()
        if research is not None:
            st.subheader("有限因子研究批次")
            st.caption(
                f"run {research['run_id']} · signal end {research['signal_end']} · "
                "仅 RankIC 诊断，尚未完成成本与 Control 晋级"
            )
            rows = []
            for item in research["registry"]:
                rows.append(
                    {
                        "factor": item["factor_id"],
                        "status": item["status"],
                        "discovery_ic": item["discovery"]["mean_rank_ic"],
                        "validation_ic": item["validation"]["mean_rank_ic"],
                        "observed_ic": item["test_observed"]["mean_rank_ic"],
                    }
                )
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            st.info(
                f"Qlib: {research['qlib']['status']}；LightGBM: {research['lightgbm']['status']}"
            )
        st.download_button(
            "下载完整排名 CSV",
            snapshot.ranking_path.read_bytes(),
            file_name=snapshot.ranking_path.name,
            mime="text/csv",
        )

elif page == "回测与基准":
    view = _baseline_view()
    if view is None:
        st.error("未找到通过 COMPLETED 标记的正式 baseline artifact。")
    else:
        st.caption(
            f"正式历史 artifact {view['schema']} / {view['run_id']} · "
            "以下指标从 manifest 约束的 daily_records 重新计算"
        )
        s = view["strategy_recovery_1"]
        c = view["control_recovery_1"]
        cols = st.columns(5)
        cols[0].metric("策略总收益", _format_pct(s["total_return_net"]))
        cols[1].metric("策略 CAGR", _format_pct(s["cagr_net"]))
        cols[2].metric("最大回撤", _format_pct(s["max_drawdown_net"]))
        cols[3].metric("Control 总收益", _format_pct(c["total_return_net"]))
        cols[4].metric("相对 Control", _format_pct(view["active"]["cumulative_active_return"]))
        st.line_chart(view["curve"].set_index("trade_date")[["strategy", "control"]])
        st.error("诚实结论：该研究示例策略相对同宇宙等权 Control 没有正 active return。")
        st.warning(
            "Settlement recovery=1 与 recovery=0 都是假设情景，不是退市实际变现；"
            "终止上市公告覆盖有限，unknown 不是 safe。"
        )
        with st.expander("指标与披露"):
            serializable = {key: value for key, value in view.items() if key != "curve"}
            st.code(json.dumps(serializable, ensure_ascii=False, indent=2), language="json")

else:
    snapshot, _, target = _latest()
    st.subheader("账户持仓与参考调仓计划")
    st.warning("本页只生成参考计划，不会向券商下单。T+1 状态、次日价格和完整费用仍需人工复核。")
    with st.expander("账户 CSV 格式与导入", expanded=not list_accounts()):
        template = (
            "account_id,account_mode,as_of,cash_cny,instrument_id,quantity,"
            "sellable_quantity,reference_cost_cny,open_orders_declaration\n"
            "my_account,manual_tracking,2026-09-10T09:00:00+08:00,200000.00,"
            "000001.SZ,1000,1000,10.50,none_declared\n"
        )
        st.download_button(
            "下载账户模板 CSV", template.encode(), "quantlab_account_template.csv", "text/csv"
        )
        uploaded = st.file_uploader("选择完整账户快照 CSV", type="csv")
        if uploaded is not None:
            raw = uploaded.getvalue()
            try:
                preview = pd.read_csv(pd.io.common.BytesIO(raw))
                st.dataframe(preview, width="stretch", hide_index=True)
                if st.button("确认导入账户快照"):
                    path = import_account_csv(raw)
                    st.success(f"已原子写入 {path}")
                    st.rerun()
            except Exception as exc:
                st.error(f"CSV 无法导入：{exc}")
        if st.button("创建/重置 20 万元演示账户"):
            path = create_demo_account()
            st.success(f"演示账户已写入 {path}")
            st.rerun()

    accounts = list_accounts()
    if not accounts:
        st.info("尚无账户。请显式创建演示账户或导入完整账户快照。")
    else:
        account_id = st.selectbox("账户", accounts)
        account = load_account(account_id)
        positions = pd.DataFrame(account["positions"])
        cols = st.columns(4)
        cols[0].metric("账户模式", account["account_mode"])
        cols[1].metric("现金", f"¥{Decimal(account['cash_fen']) / 100:,.2f}")
        cols[2].metric("持仓数", len(account["positions"]))
        cols[3].metric("账户时间", account["as_of"])
        if account["account_mode"] == "demo_simulation":
            st.info("这是演示账户，不是用户实际资产。")
        if not positions.empty:
            positions["reference_cost_cny"] = positions["reference_cost_fen"].map(
                lambda value: None if pd.isna(value) else float(value) / 100
            )
            st.dataframe(positions, width="stretch", hide_index=True)
        if st.button("生成下一交易日参考计划", type="primary"):
            try:
                json_path, csv_path, payload = build_reference_plan(account_id)
                st.success(f"参考计划已生成：{payload['plan_id'][:12]}")
            except Exception as exc:
                st.error(f"计划未生成：{exc}")
        saved_plan = load_latest_plan(
            account_id, account_fingerprint=account["account_fingerprint"]
        )
        if saved_plan is not None:
            path, payload = saved_plan
        else:
            path = None
            payload = None
        if path is not None and payload is not None:
            st.error(
                f"状态：{payload['status']}；execution_confirmed=false；"
                f"信号日 {payload['signal_date']}，拟用于 "
                f"{payload['intended_next_session']} 复核。"
            )
            plan = pd.DataFrame(payload["rows"])
            st.dataframe(plan, width="stretch", hide_index=True)
            csv_path = path.with_suffix(".csv")
            st.download_button("下载参考计划 CSV", csv_path.read_bytes(), csv_path.name, "text/csv")
            with st.expander("已知限制"):
                for limitation in payload["known_limitations"]:
                    st.write(f"- {limitation}")
    if snapshot is None:
        st.info("尚无日频报告；参考计划需要先生成日报。")
