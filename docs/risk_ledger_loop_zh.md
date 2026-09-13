# 风险层 × 逐日账本闭环：接口与中文说明

本模块把已合并的市场风险层（PR #122）接到逐日股数、现金和成交约束账本，
形成可验证闭环：**执行昨日意图 → 原始价估值 → 今日风险决定 → 缩放基础
目标 → 生成次日订单意图**。它不证明任何策略盈利，不认证历史数据，不下单；
人工情景与未来可能的历史回放都不是实盘收益。

- 任务卡：[risk_ledger_integration_v1](development_tasks/risk_ledger_integration_v1.md)
  （其中"风险未知继续运行"的表述已被
  [risk_ledger_fix_v1](development_tasks/risk_ledger_fix_v1.md) 按原委托
  纠正为"必要未知停止"）；修复卡同时记录 R1–R5 的审查修复。
- 代码：`src/quantlab/research/risk_ledger_loop.py`（新）；
  `quantity_scheduler.py` 的单日推进函数 `advance_research_day` 要求账本
  恰好位于前一共同交易日（拒绝同日重入/逆序/跳日），且只在整日成功后
  才合并 attempted 身份集合；旧入口行为与测试保持不变。
- 测试：`tests/research/test_risk_ledger_loop.py`、调度器守卫测试。

## 每日顺序与原子提交

1. 执行 t 日订单——只可能来自前一共同交易日的决定（`signal_date = t-1`）。
2. 既有内核处理成交约束、费用与持仓，再用 t 日原始收盘价估值。
3. 用截至 t 日已完成的含费净值（整数分）与持久化 `RiskState` 计算风险决定。
4. 风险上限作用于调用者提供的、仍然有效的基础目标（adapter 投影并保留
   `signal_as_of`）；绝不连续缩放已降仓目标，恢复回到基础策略当前有效目标。
5. 目标与实际持仓差分生成 t+1 意图：每证券每方向每天至多一条净意图，
   策略到期退出与风险退出合并；订单号 `<signal_date>|<side>|<instrument>`
   确定性可复现；权重→预算用 `Decimal(str(weight)) × 整数分权益` 精确
   地板除，不用浮点乘积（0.57×2000 万在 11400 分/股下恰好 1000 股）。

**一个交易日 = 一个原子提交**（成交 + 风险状态 + 次日意图）。任何必要
未知——当日证据缺失、基础目标缺失、风险决定未知（V/M 预热或缺口、
D 净值不可用）、目标股票缺原始标记、权益非正、公司行为声明未知——
都在当日**停止**路径：返回上一个完整检查点、失败日期与具体原因；
当日已产生的中间账本变更全部丢弃，attempted 身份不被污染。V/M 的预热
历史必须先于评估窗口由调用者提供；预热缺口是停止，不是一段可比较的
空仓收益。未来某日证据缺失按日发现，保留已完成前缀，不预先抛异常。

## 检查点与重启（R1 契约）

`RiskLedgerCheckpoint` 绑定：完整账本、待执行意图（全部签署在账本当日，
支持 2021-12-31 信号 → 2022-01-04 执行的首次建仓）、D 状态、attempted
订单身份、**原始本金**与运行标识 `run_id`。`run_risk_ledger_loop` 从检查点
启动并在结果上暴露最后提交的检查点；续跑不重播种空待执行列表、不把
剩余现金当原始本金（`result.initial_cash_fen` 恒为原始值）、`run_id` 不匹配
即拒绝。测试在含部分成交与被挡订单的世界里**逐切分日**恢复并逐日比对
订单、费用、现金、股数、风险状态与本金。

## 可运行示例（人工价格，非历史表现）

```python
from datetime import date
from decimal import Decimal

from quantlab.research.risk_ledger_loop import (
    LedgerSessionEvidence, RiskLedgerCheckpoint, RiskLedgerConfig,
    run_risk_ledger_loop,
)
from quantlab.research.market_risk import RiskState
from quantlab.research.quantity_kernel import ResearchFeeScenario, ResearchQuantityRules

calendar = tuple(...)  # 共同交易日历，严格递增；V/M 预热历史先于窗口
rules = ResearchQuantityRules(...)   # 声明的数量网格场景（不等于真实逐券规则）
fees = ResearchFeeScenario(...)      # 显式费用；未确认项不得填零
evidence = {t: LedgerSessionEvidence(t, contexts(t), marks(t), True) for t in ...}
base_targets = {t: strategy_valid_target(t) for t in ...}   # 策略侧逐日给出

start = RiskLedgerCheckpoint.start(
    signal_date=calendar[0],          # 或 2021-12-31，携带首日建仓意图
    initial_cash_fen=20_000_000,
    run_id="s4_baseline_2022_c80",
    drawdown_state=RiskState.initial(Decimal(20_000_000), "s4_baseline_2022"),
)
result = run_risk_ledger_loop(
    checkpoint=start, calendar=calendar, requested_end=calendar[-2],
    base_targets=base_targets, evidence=evidence,
    config=RiskLedgerConfig(
        rule_id="VMD", run_id="s4_baseline_2022_c80",
        nav_series_id="s4_baseline_2022", nav_source="loop_marked_equity",
        generation_rules=rules,
        return_source="parallel_unscaled_ledger",
        index_source="csi_all_share_000985_pit",
    ),
    unscaled_risk_returns=...,   # 仅 V 系规则；未缩放口径，先于窗口备齐
    index_closes=...,            # 仅 M 系规则；000985 PIT
)
for record in result.records:
    print(record.session, record.marked_equity_fen, record.decision.cap,
          record.risk_target_gross_exposure, record.actual_gross_exposure,
          record.modeled_fees_fen)
# 中断续跑：next_run = run_risk_ledger_loop(checkpoint=result.checkpoint, ...)
# result.initial_cash_fen 始终是 20,000,000，不是剩余现金。
```

每个 `record` 保存当日风险决定、基础目标、风险目标、模拟订单、实际持仓、
现金与费用；`actual_gross_exposure`（实际）与 `risk_target_gross_exposure`
（目标）分开报告——目标合规不等于实际仓位合规（卖不出时）。完整可运行
版本见测试文件的合成世界构造器（含非零佣金/印花税情景）。

## 与 Codex 的接口分工

- Codex 提供：逐日基础目标、逐日执行证据（上下文+原始标记+公司行为声明）、
  V 未缩放收益系列、M 指数序列、D 状态持久化位置、`run_id` 命名约定。
- 本模块提供：闭环驱动、差分意图、原子检查点、停止语义。
- 首条历史回放验入状态（按封存事实三档区分）：
  [s4_first_replay_admission_zh](s4_first_replay_admission_zh.md)——
  停在 G1（2 只股票 prior20 窗口 6 个空日期）等缺口上，经济路径消耗 0。
- 已知限制：单一 `generation_rules` 只是声明场景，不覆盖逐证券/日期的
  真实规则差异；人工测试与 completed_scenario 均不构成历史收益资格。
