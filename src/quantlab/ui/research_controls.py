"""Read-only user research constraints and hypothetical fee component calculator."""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from quantlab.daily.service import PROJECT_ROOT
from quantlab.research.costs import estimate_research_order_components, load_research_cost_profile
from quantlab.ui.workbench import public_error


def render_research_costs():
    with st.expander("我的研究约束与费用试算", expanded=True):
        st.write(
            "目标：最大回撤控制在 20% 以内追求收益；至少持有 1 个交易日，比较 5、10、20 日上限。"
        )
        st.caption("这是研究筛选目标，无法保证未来回撤上限；停牌和跌停可能延长实际持有时间。")
        try:
            profile = load_research_cost_profile()
            rate_per_ten_thousand = Decimal(profile["commission_rate"]) * 10000
            stock_min = Decimal(profile["stock_minimum_commission_fen"]) / 100
            etf_min = Decimal(profile["etf_minimum_commission_fen"]) / 100
            st.write(
                f"佣金暂按万分之 {rate_per_ten_thousand.normalize():f}；"
                f"股票最低 {stock_min.normalize():f} 元，ETF 最低 {etf_min.normalize():f} 元。"
                "股票卖出印花税另计。"
            )
            amount = st.number_input(
                "单笔假设成交金额（元）",
                min_value=0.0,
                max_value=100000000.0,
                value=5000.0,
                step=100.0,
                format="%.2f",
            )
            day = st.date_input(
                "费用规则适用日期",
                value=date.fromisoformat(profile["rules_reviewed_through"]),
                min_value=date.fromisoformat(profile["supported_start"]),
                max_value=date.fromisoformat(profile["rules_reviewed_through"]),
            )
            rows = []
            for asset, asset_name in (("stock", "股票"), ("etf", "ETF")):
                for side, side_name in (("buy", "买入"), ("sell", "卖出")):
                    result = estimate_research_order_components(
                        [int(Decimal(str(amount)) * 100)],
                        asset_type=asset,
                        side=side,
                        trade_date=day,
                    )
                    if result["profile_fingerprint"] != profile["profile_fingerprint"]:
                        raise ValueError("研究费用配置在试算期间发生变化，请重新试算")
                    rows.append(
                        {
                            "类别": asset_name,
                            "方向": side_name,
                            "佣金（元）": f"{Decimal(result['commission_fen']) / 100:.2f}",
                            "印花税（元）": f"{Decimal(result['stamp_duty_fen']) / 100:.2f}",
                            "两项小计（元）": (
                                f"{Decimal(result['known_components_subtotal_fen']) / 100:.2f}"
                            ),
                        }
                    )
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            st.warning(
                "这里只试算佣金与印花税。其他收费覆盖、滑点、价差、冲击成本及分红税尚未完整核验，不能把小计当作全部成本。"
            )
            st.caption(
                "按一个假设订单的成交金额汇总后计一次最低佣金，分项四舍五入至分；尚未核验券商实际计费口径。"
            )
            st.download_button(
                "下载本轮研究约定",
                (PROJECT_ROOT / "docs/research_round2_preregistration.md").read_bytes(),
                "QuantLab_研究约定.md",
                "text/markdown",
            )
        except Exception as exc:
            st.error(f"研究费用设置无法校验：{public_error(exc)}")
    st.caption("下方已有历史结果沿用原费用假设；本次费用设置尚未用于重算这些结果。")
