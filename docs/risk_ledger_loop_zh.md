# 风险层 × 逐日账本闭环：接口与中文说明

本模块把已合并的市场风险层（PR #122）接到逐日股数、现金和成交约束账本，
形成可验证闭环：**执行昨日意图 → 原始价估值 → 今日风险决定 → 缩放基础
目标 → 生成次日订单意图**。它不证明任何策略盈利，不认证历史数据，不下单；
人工情景与未来可能的历史回放都不是实盘收益。

- 任务卡：[risk_ledger_integration_v1](development_tasks/risk_ledger_integration_v1.md)
- 代码：`src/quantlab/research/risk_ledger_loop.py`（新）；`quantity_scheduler.py`
  仅抽取了单日推进函数 `advance_research_day`，旧入口 `simulate_research_schedule`
  行为与测试保持不变。
- 测试：`tests/research/test_risk_ledger_loop.py`（12 个情景，全部断言具体
  股数/现金/费用/状态）。

## 每日顺序（与任务卡一致）

1. 执行 t 日订单——只可能来自前一共同交易日的决定（`signal_date = t-1`）。
2. 既有内核处理成交约束、费用与持仓，再用 t 日原始收盘价估值。
3. 用截至 t 日已完成的含费净值（现金 + 股数×原始收盘，整数分）与持久化
   `RiskState` 计算风险决定（七组规则原样）。
4. 风险上限作用于调用者提供的、仍然有效的基础目标（adapter 投影旧日期
   目标并保留 `signal_as_of`）。基础目标每日由策略侧给出，**绝不**把昨日
   已缩放目标再缩放——风险解除后恢复到基础策略当前有效目标。
5. 目标与实际持仓的差分生成 t+1 意图：每证券每方向每天至多一条净意图，
   策略到期退出与风险退出合并为同一卖单；订单号
   `<signal_date>|<side>|<instrument>` 确定性可复现；下一日被挡（跌停/停牌/
   T+1/容量）则保留持仓，再下一日的差分重新发出**新的**明确指令。
6. 次日能否成交、成交多少由既有 quantity_kernel 裁决；先卖后买、只有模拟
   卖出实际成功的钱才能买入，全部在内核既有语义里。

## 关键冻结决策

- **风险未知 ≠ 空仓 ≠ 停止**：V/M 输入缺失时该日记为 `risk_unknown`，
  不发新意图、保留持仓、路径继续；只有执行证据缺失才停止并保留最后
  完整日。
- **不在目标里的持仓整体退出**：基础策略剔除的股票生成全额卖意图
  （受单笔上限约束，余量次日补足）。
- **V 的未缩放收益必须由调用者提供**并声明来源；本循环绝不从自身已被
  缩放的净值推导。D 的净值流用 `nav_series_id` 标识，状态跨日持久化、
  同日不可重放（R2 契约）。
- 买入按目标市值降序排队（最欠配的先竞争卖出释放的现金），卖出按代码
  序；数量网格与单笔上限按 `generation_rules` 声明场景在生成侧取整，
  执行侧以当日上下文为准。
- 每日一个原子提交（账本 + 风险状态 + 意图）；异常重试从最后完整日恢复，
  不重复记账或重复恢复仓位。

## 可运行示例（人工价格，非历史表现）

```python
from datetime import date, timedelta
from decimal import Decimal

from quantlab.research.risk_ledger_loop import (
    LedgerSessionEvidence, RiskLedgerConfig, run_risk_ledger_loop,
)
from quantlab.research.market_risk import RiskState
from quantlab.research.quantity_kernel import ResearchBook, ResearchFeeScenario, ResearchQuantityRules

calendar = tuple(...)  # 共同交易日历，严格递增
rules = ResearchQuantityRules(...)   # 声明的数量网格场景
fees = ResearchFeeScenario(...)      # 声明的费用场景（不可擅自填零未确认项）
evidence = {t: LedgerSessionEvidence(t, contexts(t), marks(t), True) for t in ...}
base_targets = {t: strategy_valid_target(t) for t in ...}   # 策略侧逐日给出
result = run_risk_ledger_loop(
    initial_book=ResearchBook(asof_date=calendar[0], cash_fen=20_000_000),
    calendar=calendar,
    requested_end=calendar[-2],
    base_targets=base_targets,
    evidence=evidence,
    config=RiskLedgerConfig(
        rule_id="VMD",                      # 或 C80/C50/V/M/D/VM
        nav_series_id="s4_baseline_2022",
        nav_source="loop_marked_equity",
        generation_rules=rules,
        return_source="parallel_unscaled_ledger",   # V 系列来源声明
        index_source="csi_all_share_000985_pit",
    ),
    drawdown_state=RiskState.initial(Decimal(20_000_000), "s4_baseline_2022"),
    unscaled_risk_returns=...,   # 仅 V 系规则需要；未缩放口径
    index_closes=...,            # 仅 M 系规则需要；000985 PIT
)
for record in result.records:
    print(record.session, record.marked_equity_fen, record.decision.cap,
          record.risk_target_gross_exposure, record.actual_gross_exposure,
          record.modeled_fees_fen, record.status)
```

每个 `record` 同时保存：当日风险决定、基础目标、风险调整目标、模拟订单、
实际模拟持仓、现金与费用；`actual_gross_exposure`（实际）与
`risk_target_gross_exposure`（目标）分开报告——目标合规不等于实际仓位
合规（卖不出时）。完整可运行版本见测试文件中的合成世界构造器。

## 与 Codex 的接口分工

- Codex 提供：逐日基础目标（策略侧）、逐日执行证据（上下文+原始标记+
  公司行为声明）、V 未缩放收益系列、M 指数序列、D 状态持久化位置。
- 本模块提供：闭环驱动、差分意图、原子日记录、停止语义。
- 首条历史回放的验入状态与最小缺口清单：
  [s4_first_replay_admission_zh](s4_first_replay_admission_zh.md)——
  目前停在 G1（2 只股票 prior20 窗口 6 个空日期）等缺口上，未消耗任何
  经济路径。
