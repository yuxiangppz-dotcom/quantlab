# S4-A 首条回放：本地证据验入与输入包（2026-09-13 第三次修订）

本次把封存数据转成可供 `risk_ledger_loop` 消费的输入包，并实际关闭了本地
可解决的缺口。零下载、零 canonical 写入、零经济路径。当前有效交付为
**v3 输出目录**；v1/v2 及其证明按原样保留（含已知缺陷），不得用旧证明为
新包背书。

## 运行方式

```bash
# 生成（输出目录必须不存在；绝不指向封存 source 或 canonical 树内）
uv run python -m quantlab.research.s4_replay_admission \
  --source-dir <Codex目录>/data/products/research_program/launch_20260912 \
  --canonical-dir <Codex目录>/data/canonical \
  --output-dir data/products/s4_first_replay_admission_v3

# 独立核对（独立算术与直读证据，不调用生产验入函数）
uv run python scripts/s4_admission_independent_verify.py \
  --package-dir data/products/s4_first_replay_admission_v3 \
  --source-dir <Codex目录>/data/products/research_program/launch_20260912 \
  --canonical-dir <Codex目录>/data/canonical
```

已有封存目录与 canonical 数据**只读**：例子里的输出目录是本工作区新目录，
不能按例子覆盖或重跑任何封存采集。

输出文件：`started.json`、`input_package.json`（20 提案+名义股数+执行
上下文+逐字段三档状态）、`preflight_report.json`（结论+精确停止日+逐券
全部缺口+**完整来源绑定清单**：intent/result/response.body/日线与停牌
分区及来源记录 id，按 sealed/canonical 根标注）、`completed.json`（绑定
包与报告哈希，可溯源到清单）、`independent_verification.json`（独立
核对，含被验证文件与验证器代码的精确哈希）。

## 缺口关闭情况

| 缺口 | 状态 | 说明 |
| --- | --- | --- |
| G2 七只精确成交量 | **已关闭** | reconciliation 整数经"手×100 / 千元×1000×100→分"精确 Fraction 验证；其余 13 只封存上下文本已带精确整数（admitted as-sealed）。响应正文逐份解析：`code=0` 且 `data.items=[]`、intent/result 封存互证、artifacts/wire 哈希与实际字节一致 |
| G1 六个 prior20 空日期 | **定性完成，规则决定待批** | 每个日期独立验证：已验证空响应（正文 code=0/items=[]，封存互证）+ 确认日线覆盖下无本地行 + 当日 S 记录且 suspend_timing 为空 → 供应商口径的全天停牌（Tushare 派生记录是佐证，不是独立来源）。覆盖缺失/缺列/日内时段/冲突一律不判全天。**窗口处理（剔除补足 vs 保持缺口）是规则决定，未实施**：prior20 仍未知，2 只提案 BLOCKED |
| G3 身份/市场状态/规则 | 部分 | market_open 与历史身份/信号资格 ×20 保持显式未知；`calendar_verified` 非 True、缺价格边界、规则区间不覆盖执行日等结构缺口全部逐券列出 |
| G4 公司行为 | **首日范围已关闭** | 派生上下文实际携带 `corporate_actions_processed=true`（逐券书面理由；仅入场日） |
| G5 费用边界 | 已明确 | 已知（用户申报口径）：佣金万 0.86 最低 5 元、卖方印花税分时点。未知：`additional_fee_rate`/`additional_fee_fixed_fen`。**最少待确认项**：为 2022-01-04 **模拟情景**补齐费用假设——申报佣金之外是否还有其他费用及数值；这不是询问一笔真实历史交易，采纳研究假设也不证明历史金融事实 |

## 预检结论

`preflight_stopped_before_execution_day`，精确停止日 **2022-01-04**。
残余缺口逐券明细在报告：additional_fees ×20、prior20 ×2、market_open
×20、历史身份 ×20 及各券结构缺口。

## 消费接口

```python
from datetime import date
from decimal import Decimal
from quantlab.research.risk_ledger_loop import RiskLedgerCheckpoint, RiskLedgerConfig
from quantlab.research.s4_replay_admission import build_first_pending_orders

package = json.load(open(".../input_package.json"))
pending = build_first_pending_orders(package)   # 20 笔买单，signal_date=2021-12-31
checkpoint = RiskLedgerCheckpoint.start(
    signal_date=date(2021, 12, 31),
    initial_cash_fen=package["initial_cash_fen"],   # 20,000,000
    config=RiskLedgerConfig(rule_id="C80", run_id="s4_first_replay_v3",
                            nav_series_id="s4_baseline_2022",
                            nav_source="loop_marked_equity",
                            generation_rules=<声明的数量规则场景>),
    pending_orders=pending,                        # 首批意图显式传入
    drawdown_state=RiskState.initial(Decimal(20_000_000), "s4_baseline_2022"),
)
# LedgerSessionEvidence 的 contexts 用 research_session_from_context 逐券组装；
# 全部 open_gaps 关闭后才能通过 preflight。
```

全部关闭后仅标记"具备申请启动首条回放的输入条件"；运行本身需独立有限卡，
经济路径保持 0。原始字段存在 / 数值已核对 / 金融资格已证明三档分开；
本包不含模拟成交、收益、CAGR 或回撤；单一 `generation_rules` 是声明场景，
不冒充逐券历史规则证明。
