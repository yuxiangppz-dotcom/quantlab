# S5 v1.2：为什么历史行业归属要先审计

S5 的核心逻辑是“先看行业，再在行业里挑股票”。这意味着历史研究有一个比普通价量因子更隐蔽的风险：**必须知道某只股票在当时属于哪个行业，而不是知道它今天属于哪个行业。**

如果把 2026 年的行业分类直接套到 2022 年，可能把公司主营业务变化、行业分类修订、指数体系调整等后来信息倒灌到过去。这样得到的 S5 回测即使很好看，也不能证明策略当时真的可以这样做。

## 五种审计状态

对每个未来想研究的“股票 + 决策日”，QuantLab 会单独检查当日有效的行业成员事实：

| 状态 | 含义 |
|---|---|
| `covered_verified` | 有 PIT 证据，所有有效事实一致指向同一行业 |
| `mismatch` | PIT 证据明确，但与调用者预期的行业不同 |
| `missing` | 当天没有有效行业成员证据 |
| `unverified` | 有记录，但至少一条不能证明是当时可知的 PIT 事实 |
| `conflicting` | 两条以上已验证事实在同一天指向不同的行业 |

系统不会把后三种状态自动修成“当前行业”，也不会因为公司名称看起来像某个行业就推断归属。

多个独立来源如果都在当日有效、都经过 PIT 验证并且一致指向同一行业，可以共同支持 `covered_verified`；它们的来源仍会完整写入审计结果和指纹。

## 为什么要求 100% membership coverage

第一轮 S5 诊断要求它实际使用的人口全部是 `covered_verified`。这是一个有意偏严格的门槛。

原因是 S5 的行业层不是辅助展示，而是策略进入条件本身。假如缺行业数据的股票被直接删掉，研究人口会发生选择性变化；如果用今天分类补齐，又会产生未来信息。两种做法都可能让历史结果虚假变好。

因此如果某个冻结研究区间存在 membership 缺口，正确输出是：

`blocked_membership_evidence`

而不是悄悄缩小分母继续跑。

## fully covered date 是什么

某个交易日只有在当天所有预先要求的股票都具有 `covered_verified` 行业身份时，才是 fully covered date。

审计会报告：

- 完整逐行状态；
- 每年 coverage rate；
- 哪些日期是 fully covered；
- 最早 / 最晚 fully covered date；
- mismatch / missing / unverified / conflicting 的数量；
- 完整证据指纹。

这些信息用于选择一个真正有资格研究的历史区间，但**选择区间必须发生在读取 S5 forward return 之前**。

## 第一轮诊断已经提前冻结什么

如果历史 membership evidence 足够，第一次 S5 retrospective diagnostic 将固定使用：

- 已合并的 S5 v1 策略内核；
- 已合并的 S5 v1.1 特征公式；
- 中证全指 `000985.SH` 作为相对强度 broad-market benchmark；
- 5 / 10 / 20 个市场交易日 forward horizons；
- Dynamic-N 原始策略仓位，不改“最多 20 只、每只 4%”；
- eligible sector count、eligible stock count、股票仓位、可定义时的 RankIC、selected 与 eligible-but-not-selected spread；
- 与简单行业反转、简单行业趋势、已有 Alpha158/Ridge 和 broad-market control 比较；
- 只允许一次冻结运行，不允许看到结果后扫描新的 20D/30D、20%/25%/30% 回撤阈值救结果。

历史收益还没有在本任务中计算。

## 这和实盘收益仍然有距离

即使以后 S5 的 signal diagnostic 通过，也还需要组合与执行层验证：真实股票数量、整手、佣金最低收费、印花税、涨跌停、停牌、T+1、公司行动以及卖不出的情况。

因此 S5 v1.2 的意义不是“证明策略赚钱”，而是确保下一次看到的 S5 历史表现至少没有因为行业身份倒灌而失真。
