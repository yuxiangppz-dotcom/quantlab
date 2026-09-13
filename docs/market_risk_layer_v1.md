# 市场仓位风险层 v1：接口与边界

独立可审查的风险上限模块。本层只做一件事：把调用者提供的时点观测
（未缩放组合收益、中证全指收盘、含费策略净值）翻译成七组已批准规则的
目标仓位上限，并把上限翻译成"只降不加"的目标组合。本层不取数、不重建
历史组合、不下单、不保证任何回撤或收益结果；未知输入保持未知，绝不把
缺数据当成空仓或风险解除。

- 任务卡：[market_risk_layer_v1](development_tasks/market_risk_layer_v1.md)
- 代码：`src/quantlab/research/market_risk.py`、`src/quantlab/research/risk_target_adapter.py`
- 测试：`tests/research/test_market_risk.py`、`tests/research/test_risk_target_adapter.py`

## 七组规则（参数冻结，不做扫描）

| 规则 | 上限语义 |
| --- | --- |
| C80 / C50 | 恒定 0.8 / 0.5，参考对照 |
| V | 60 个连续共同交易日的未缩放组合收益，样本标准差（ddof=1）年化 252 日，上限 min(0.8, 15%/年化波动)；全零波动给 0.8；不足 60 日、缺口、非有限值 → 未知 |
| M | 中证全指 000985 收盘严格高于含当日的 200 日均值 → 0.8，否则 0；相等归 0；缺值或预热不足 → 未知；其他指数一律拒绝 |
| D | 含费净值回撤分档：≥10% 系数 0.5、≥15% 系数 0.25；严重档回撤 ≤12% 恢复 0.5 档，0.5 档回撤 ≤8% 恢复 1 档；每次决策最多恢复一档；上限 = 0.8×系数；高水位不因空仓、重启、跨年重置 |
| VM / VMD | 各组成上限的最小值；任一组成未知 → 组合未知并附各原因；D 组成已评估时其新状态照常输出，保证持久化不丢失 |

边界口径："达到" = `>=`，"以内" = `<=`，全部用 Decimal 精确比较。V 的浮点
波动仅用于上限计算，阈值判断不涉及浮点相等。

## 契约类型（`market_risk.py`）

- `RiskInputs`：一次决策可见的全部输入。`sessions` 为共同交易日历（必须
  严格递增且包含决策日，可向未来延伸仅用于推导下一执行日）；任何晚于
  决策日的观测在构造时拒绝；重复/逆序日期、来源标识缺失、含未处理外部
 资金流的净值一律拒绝（ValueError）。
- `RiskState`：D 规则的持久状态（高水位 + 系数），由调用者跨决策保存。
- `RiskDecision`：决策日、下一允许执行日、规则版本指纹、状态（ok/unknown）、
  上限或未知、原因、波动估计、均线状态与均值、高水位、回撤、系数、
  新持久状态、输入来源标识。
- `evaluate_market_risk_rule(rule_id, inputs)`：七组规则的唯一入口。

适配器（`risk_target_adapter.py`）：

- `apply_risk_cap(target, decision) -> RiskTargetResult`。决策未知时返回
  `not_ready`（可检测、无目标、不冒充空仓也不静默沿用旧目标）；已知上限
  只降不加：按 `cap/gross` 等比例缩减既有正权重，排名与成员不变、不补票、
  不加杠杆，余量为现金；上限 0 给出 `positions=()`、`cash_weight=1.0` 的
  显式目标；负权重拒绝。

## 合成调用示例（非历史表现）

```python
from datetime import date
from decimal import Decimal

from quantlab.research.market_risk import (
    RiskInputs, RiskState, StrategyNav, evaluate_market_risk_rule,
)
from quantlab.research.risk_target_adapter import apply_risk_cap

sessions = (...)  # 共同交易日历，严格递增
inputs = RiskInputs(
    decision_date=sessions[-1],
    sessions=sessions,
    strategy_nav=StrategyNav(as_of=sessions[-1], nav=Decimal("85")),
    drawdown_state=RiskState(Decimal("100"), Decimal("1")),  # 上一决策持久化的状态
    nav_source="audited_net_ledger_v1",
)
decision = evaluate_market_risk_rule("D", inputs)
# decision.cap == Decimal("0.2")（回撤 15% 进入 0.25 档）
# 决策意图：gross ≤ 0.2；最早在 decision.next_execution_date 尝试执行
persisted = decision.drawdown_state  # 传给下一决策，绝不重置

result = apply_risk_cap(current_target, decision)
if result.status != "ready":
    ...  # 未就绪：保持决策挂起，不得假装空仓
# result.target: 权重等比例缩减后的新目标，余量现金
# 注意：actual 持仓仍可能高于 0.2 —— 例如某持仓一字跌停卖不出时，
# 执行账本保留该仓位；本层没有权力删除持仓或改写现金。
```

三层事实的区分：**风险意图**（decision.cap）→ **目标翻译**
（result.target）→ **实际执行**（执行账本里的成交、涨跌停、T+1 约束）。
目标合规不等于实际仓位合规；本层必须按既有持仓逐日评估，而不是只在
选股调仓日生效。

## 待 Codex 接入事项

1. V 规则需要"未被本风险层缩放的组合收益"序列——回放时请提供风险层
   生效前的组合口径收益，并在来源标识中声明；本层无法自行验证。
2. D 规则的净值必须是含费、可审计、已处理外部资金流的净值；持久化
   `RiskState` 的存储位置（回放账本或独立状态文件）由 Codex 决定。
3. M 规则需要中证全指 000985 的 PIT 收盘序列与共同交易日历的对齐。
4. 黄金/债券等子组合不得复用 M 开关；适配器对子组合逐个适用。
5. 已知限制：首期拒绝含未处理外部申赎的净值；浮点权重缩放存在
   1e-9 量级求和漂移（远小于组合模型 1e-6 容差）；`next_execution_date`
   为 None 时表示日历视野内无下一交易日，决策仍有效。
