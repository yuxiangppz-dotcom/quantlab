# 每日 ML 模拟账户

这是 `quantlab ml` 主线的逐日运行入口。历史 `shadow` 命令仍是“封存分数的组合情景回放”；
要记录当时实际形成的组合决策，使用本文的 `run-day`。两者均不连接券商。

## 一次性准备

先按 `ml_daily_v2_zh.md` 完成数据核验、训练和模型注册/启用。每日服务要求训练 bundle
包含 `feature_contract.json` 和 PIT lineage，注册模型会携带特征契约 SHA256。契约应定义
特征公式、版本、单位和依赖来源；只有列名相同不够。旧的无契约模型仍可用于历史研究，
不能直接进入服务。模型配置必须与服务配置相同，模型更新需显式 activate。

```bash
uv run quantlab ml init-service --output data/experiments/ml_service \
  --config config/ml_daily_v2.json --calendar /path/calendar.json \
  --initial-marks /path/initial_marks.json --feature-contract /path/feature_contract.json \
  --as-of YYYY-MM-DD --capital-cny 200000 --max-model-age-days 45
```

`as-of` 是空仓账户的首个决策日，初始标价必须来自当日收盘。初始资金为模拟资金，不导入真实账户。
标价 JSON 格式沿用回放的 initial-marks。初始化后不修改本金、策略参数及历史状态；不同政策开独立服务。

## 每日输入

本地适配器为每个交易日准备一个独立目录：

- `features.parquet`：恰好当日一份特征，含 trade_date、instrument_id、eligible、industry、
  feature_available_at 和模型特征列。包括所持股票的上下文，不因没有预测而删除持仓证券。
- `calendar.json`：完整已知交易日序列，至少含下一交易日；允许在末尾追加，禁止改变已有日期。
- `market.json`：单个 ResearchDay JSON，对应当日，格式见原 market-days JSONL 中的一行。
  必须提供账本涉及股票及下一次候选股票的原始收盘标价；不允许夹带 orders。
- `corporate_actions.json`：coverage 覆盖账户起点至当日；events 包含截至当时已知的完整事件，
  包括未来除权但登记日已知的事件。已录入事件不得消失或静默修改。迟到的历史权益需要单独对账。
- `manifest.json`：下述回执。所有时间/来源必须取自真实采集记录，不允许为过门槛而回填。

```json
{
  "asof": "YYYY-MM-DD",
  "source_id": "本地适配器及源快照版本",
  "available_at": "YYYY-MM-DDT15:40:00+08:00",
  "feature_contract_sha256": "训练特征契约文件的64位SHA256",
  "files": {
    "features.parquet": "SHA256",
    "calendar.json": "SHA256",
    "market.json": "SHA256",
    "corporate_actions.json": "SHA256"
  }
}
```

## 每日命令与顺序

```bash
uv run quantlab ml run-day --service data/experiments/ml_service \
  --inputs /path/daily/YYYY-MM-DD --registry data/experiments/ml_registry --as-of YYYY-MM-DD
uv run quantlab ml service-state --service data/experiments/ml_service
```

1. 校验输入、历史账户父记录、连续交易日和事件一致性。
2. 首日确认初始标价；后续每天结算前一日封存的股数订单。共用历史回测的费用、交易约束和公司行动内核。
3. 用结算后的持仓生成下一交易日决策。保存配置引用、输入快照、模型、股数订单、目标、账户及代码版本。
4. 完整写入并发布后才记录可用时间。默认截止为上海时间 16:00；超时产物不能成为前向订单。
5. 下一日仅消费已封存且按时发布的订单，不重新打分或按新参数重算历史决策。

相同日期相同输入重跑不会重复记账；不同输入不能覆盖完成日。中断前尚未发布的工作目录不参与账本。
若发布后、回执写入前中断，重试以**重试时刻**保守记录可用时间，绝不补写过去的时间。

缺日必须按顺序补跑。晚到补跑继续结算已有订单、记账与估值，但不会补造当时未生成的决策；
没有有效决策时下一天持有原仓和现金，仍处理公司行动，不假定可以清仓。正常风险减仓同样需要及时决策。

模型距训练截止超过 45 个自然日（可在初始化时指定）就停止生成新决策。缺模型、特征不合格等
可在当日记录为 decision_blocked，但已完成的账户结算保留。**完成日只提交一次**：当天已提交阻断/暂停状态，
不会事后追加一套决策；修复后从下一交易日继续。操作上应先完成数据/模型预检，再触发正式日任务。

`run-day` 无合格前向决策返回退出码 2，运行失败返回非零；定时任务必须保存 stdout/stderr 并对非零报警。
禁止只按进程退出或上次成功时间显示“今日正常”。发生无法核验的费用/公司行动时不提交当天账户。

```bash
uv run quantlab ml service-state --service data/experiments/ml_service --set paused
uv run quantlab ml service-state --service data/experiments/ml_service --set active
```

暂停只停止生成新决策；此前已封存订单仍按原计划做模拟结算，不回滚成交。该控制不影响真实账户。

## 本地运行验收

数据适配及 WSL 定时任务见 `ml_v2_local_completion_prompt_zh.md`。云端没有真实市场输入；合成测试验证的是
时序、会计与恢复机制。日频下一收盘撮合仍是研究假设，信号分数不是预期收益，模拟成交不是成交回报。

## 每日账户报告

```bash
uv run quantlab ml service-report --service data/experiments/ml_service \
  --benchmark /path/benchmark.parquet --output data/experiments/ml_service_report_YYYYMMDD
```

benchmark 两列为 session、benchmark_return；必须覆盖全部已结算日。报告保留无有效决策的交易日，
输出账户净值、基准比较、换手、费用、持仓、成交/拒单和风险偏离。停止日不被伪装成完整区间。
UI 默认 ML 工作台展示相同账户；service-state 包含新鲜度和最近未解决失败，日志也须保留。
