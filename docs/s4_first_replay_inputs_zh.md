# S4-A 首条回放：本地证据验入与输入包（2026-09-13）

本次把封存数据转成了可供 `risk_ledger_loop` 消费的输入包，并实际关闭了
能在本地解决的缺口。零下载、零 canonical 写入、零经济路径。

## 运行方式

```bash
uv run python -m quantlab.research.s4_replay_admission \
  --source-dir <Codex目录>/data/products/research_program/launch_20260912 \
  --canonical-dir <Codex目录>/data/canonical \
  --output-dir data/products/s4_first_replay_admission_v1
# 独立核对（不调用生产验入函数，Fraction 重推导）：
uv run python scripts/s4_admission_independent_verify.py data/products/s4_first_replay_admission_v1
```

输出三个文件：`input_package.json`（20 提案 + 名义股数 + 执行上下文 +
逐字段验入状态）、`preflight_report.json`（总体结论 + 精确停止日 + 逐券
全部缺口 + 来源清单与指纹）、`independent_verification.json`（独立核对
8/8 通过）。

## 缺口关闭情况

| 缺口 | 本次结果 | 说明 |
| --- | --- | --- |
| G2 七只精确成交量 | **已关闭** | reconciliation 整数经"手×100 / 千元×1000×100→分"精确 Fraction 验证后写入包；13 只封存上下文本已带精确整数（独立核对确认替换的 7 只封存值均为 None），同样标 admitted（来源=sealed plan） |
| G1 六个 prior20 空日期 | **定性完成，规则决定待批** | 6 个日期全部命中本地停牌记录（suspend_type=S，000301.SZ 12-22 停/23 复；000777.SZ 12-07~13 连停/14 复），与"供应商空返回+本地无日线"三方一致 → 已证明全天停牌。**但**停牌日如何计入 20 日窗口（剔除补足 vs 保持缺口）是冻结规则的变更，只提出方案未实施：prior20 仍未知，2 只提案继续 BLOCKED |
| G3 身份/市场状态/规则 | 部分关闭 | 逐券数量规则与板块口径随封存上下文带入并保留来源；market_open 与历史身份/信号资格**保持未知**（缺少记录不是可交易证明），已列每券缺口 |
| G4 公司行为 | **首日范围已关闭** | 初始空仓+纯买入 → 首日无持有批次跨除权日，`corporate_actions_processed=True` 带逐券书面理由（仅覆盖入场日，不覆盖未来持有期） |
| G5 费用边界 | 已明确 | 已知（用户申报口径）：股票佣金万分之 0.86、最低 5 元；卖方印花税另计、按日期分档（2023-08-28 前千分之一、之后万分之五）。未知：申报佣金之外的历史费用构成（过户费/征管费等是否已含、适用历史口径）→ `additional_fee_rate`、`additional_fee_fixed_fen` 保持显式未知，不填零、不重复计收。**最少待确认项（一句话）**：研究情景 2022-01-04 的模拟卖出/买入，除申报佣金与当期印花税外是否应建模其他费用及其数值——这是为 2022 年模拟情景补齐费用假设，不是询问一笔真实发生的历史交易；即使采纳某个研究假设，也不代表相关历史金融事实已被证明 |

## 预检结论

`preflight_stopped_before_execution_day`，精确停止日 **2022-01-04**。
当前残余缺口（逐券明细在报告中）：additional_fees ×20（等你一个回答）、
prior20 ×2（等停牌窗口规则决定）、market_open ×20 与历史身份 ×20
（需要可交易正证据或明确的"按封存口径接受"决定）。

## 消费接口

- `RiskLedgerCheckpoint.start(signal_date=date(2021,12,31), initial_cash_fen=20_000_000, config=<完整 RiskLedgerConfig>)`——首笔待执行意图保留 2021-12-31 信号日期。
- `LedgerSessionEvidence(session=date(2022,1,4), contexts=<包内 20 个 context 转 ResearchSession>, marks=<2022-01-04 原始收盘>, corporate_processing_complete=True)`——全部 open_gaps 关闭后即可组装。
- 全部关闭后仅标记"具备申请启动首条回放的输入条件"；运行本身仍需独立有限卡，经济路径保持 0。

## 边界

原始字段存在 / 数值已核对 / 金融资格已证明三档分开标注；封存文件
 SHA-256 在处理前后逐字节不变（独立核对含重算）；本包不输出模拟成交、
收益、CAGR 或回撤；单一 `generation_rules` 仍是声明场景，不冒充逐券
历史规则证明。
