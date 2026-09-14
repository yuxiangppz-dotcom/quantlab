# S5 v1.2：历史行业成员证据审计与诊断协议

GitHub Issue：#137；前置：#131 / PR #136；上层研究计划：#127。

本任务不运行 S5 历史收益，只回答两个问题：

1. 指定的历史 `(股票, 决策日)` 人口是否有可辩护的 PIT 行业归属；
2. 在看任何 S5 结果之前，第一轮诊断到底怎样评估。

S5 v1 内核、v1.1 特征公式、Dynamic-N（最多 20 只、每只 4%）以及 V/M/D 风控均冻结，本任务不得修改。

## 行业成员审计状态

对每个 `S5MembershipRequirement(instrument_id, as_of, expected_sector_id)`，只读取当日有效区间覆盖该日期的成员证据：

- `covered_verified`：至少一条有效证据、全部有效证据均 `pit_verified=True`，且所有已验证证据一致指向同一行业；如果给出 expected sector，该行业必须与 expected 一致；
- `mismatch`：有效、已验证、无冲突，但唯一行业与 expected sector 不同；
- `missing`：当日没有有效成员证据；
- `unverified`：当日存在有效成员证据，但至少一条没有 PIT 验证；
- `conflicting`：当日存在至少两个已验证且行业身份互相冲突的有效事实。

优先级固定为：无 active -> missing；任何 active unverified -> unverified；多个 verified sector -> conflicting；唯一 verified sector 与 expected 不同 -> mismatch；否则 covered_verified。

多个来源对同一行业给出一致且 PIT-verified 的事实允许视为 covered，但来源全部保留到审计行和指纹中。

## 指纹

审计指纹绑定：

- schema/version；
- 完整 requirement 人口；
- 截至最后 required date 已开始生效的成员事实；
- source id、PIT 状态、有效起点；
- 已在 required horizon 内结束的有效终点。

开始于最后 required date 之后的事实不进入指纹；终点晚于最后 required date 与 open-ended 在本次审计中等价，避免后来得知的未来结束日期改写更早审计。历史成员或来源变化必须改变指纹。

## 完整性判定

- 空 requirement 人口直接拒绝；不能把“没有要求”解释成 100% coverage。
- 空 evidence 对非空人口是合法但 `missing` / blocked，不是假成功。
- 每个 decision date 只有当该日期所有 required rows 都为 `covered_verified` 才属于 fully covered date。
- 年度 coverage rate = 当年 `covered_verified rows / total required rows`。
- 第一轮 S5 诊断要求其冻结 population 的 membership coverage = 100%。任何 mismatch/missing/unverified/conflicting 都使该 population `blocked_membership_evidence`。

## 冻结的第一轮 S5 诊断协议

本 PR 只保存协议，不运行结果：

- strategy：准确使用已合并 S5 v1 + v1.1，无参数修改；
- universe：既有 A 股 PIT research universe 且 membership audit 为 covered_verified；
- benchmark：中证全指 000985 的现有 PIT broad-market source；如果不可用则 blocked；
- signal period：只能在真实审计后，从连续、100% verified membership coverage 的区间中选择，并在加载任何 S5 forward outcome 前冻结；
- forward horizons：5 / 10 / 20 个市场交易日；
- signal diagnostics：eligible sector count、eligible stock count、Dynamic-N exposure、可定义时的 stock RankIC、selected vs eligible-nonselected spread；
- controls：simple sector reversal、simple industry trend、现有 Alpha158/Ridge、broad-market control，在证据允许时比较；
- portfolio/economic stage：需要另行声明执行与成本证据；研究 proxy 不得命名为真实净账户收益；
- 必须报告负月份、负 regime、空仓期；
- 第一轮只允许一个冻结 run，不允许看结果后改 25%/40%/20D 等阈值重跑。

## 本任务测试

至少覆盖：区间边界、open-ended、同向多来源、unverified、verified conflict、日期缺口、expected mismatch、future-start evidence、输入顺序不变、历史 source/member drift 改指纹、未来 end 不改早期指纹、空 requirement 拒绝、空 evidence blocked、年度汇总与 fully-covered-date 判定。

验证：`uv run pytest -q`、`uv run ruff check .`、`git diff --check`、research-runtime CI 全绿。不得触碰 open S4 PR #126。
