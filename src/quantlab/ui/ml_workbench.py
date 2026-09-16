"""Default ML workbench: read verified artifacts; never launch training or orders."""

from pathlib import Path

import pandas as pd
import streamlit as st

from quantlab.research.ml.service import inspect_service
from quantlab.research.ml.service_view import account_frames
from quantlab.ui.workbench import public_error


def render_ml_workbench(root: Path):
    st.subheader("日频 ML 主工作流")
    st.write(
        "历史研究：数据核验 → 滚动训练 → 账户回放 → 基准报告。"
        "每日模拟：结算已封存订单 → 当日预测 → 封存下一日决策。"
    )
    st.caption("原 Daily、旧前瞻观察和阶段实验已归入历史研究；这里展示新 ML 服务。")
    services = sorted(
        {
            p.parent
            for pattern in ("*/service.json", "*/*/service.json")
            for p in (root / "data/experiments").glob(pattern)
        }
    )
    if not services:
        st.info("尚未初始化每日模拟账户。先按 docs/ml_service_zh.md 接入本地数据并完成验收。")
        st.code(
            "uv run quantlab ml --help\nuv run quantlab ml init-service --help\n"
            "uv run quantlab ml run-day --help",
            language="bash",
        )
        st.write("本地交接任务：docs/ml_v2_local_completion_prompt_zh.md")
        return
    selected = st.selectbox("模拟账户", services, format_func=lambda p: str(p.relative_to(root)))
    try:
        status = inspect_service(selected)
        frames = account_frames(selected)
    except Exception as exc:
        st.error(f"账户证据无法核验：{public_error(exc)}")
        return
    cols = st.columns(3)
    cols[0].metric("账户截止日", status["asof"])
    cols[1].metric("现金（元）", f"{status['cash_fen'] / 100:,.2f}")
    cols[2].metric("持仓批次", status["lots"])
    st.write(f"状态：{status['status']}")
    if status.get("stale"):
        st.warning(f"账户尚未更新到应完成交易日 {status['expected_session']}，不能视为今日正常。")
    if not status["forward_decision"]:
        st.warning("最近一日没有合格前向决策。请检查截止时间、模型、输入或暂停状态。")
    if status.get("latest_failure"):
        st.warning(f"最近运行失败：{status['latest_failure']['reason']}")
    ledger = frames["ledger"]
    if not ledger.empty:
        st.line_chart(
            ledger.set_index("session")[["equity_fen"]].rename(
                columns={"equity_fen": "模拟权益（元）"}
            )
            / 100
        )
        st.dataframe(ledger, hide_index=True, width="stretch")
    for title, name, date_col in (
        ("实际模拟持仓", "holdings", "session"),
        ("最近封存目标", "targets", "signal_date"),
        ("模拟成交与拒单", "attempts", "session"),
    ):
        st.subheader(title)
        frame = frames[name]
        latest = (
            frame.loc[frame[date_col].eq(status["asof"])] if not frame.empty else pd.DataFrame()
        )
        st.dataframe(latest, hide_index=True, width="stretch")
    st.caption("目标与实际持仓分开显示。日频下一收盘撮合是研究假设，没有券商交易权限。")
