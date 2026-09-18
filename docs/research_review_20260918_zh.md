# 2026-09-18 研究主线独立复核（进行中）

直接委托；打开的评审工作树为干净的 detached `9c18f7e`，实际研究工作树
`/home/administrator/projects/quantlab-pr189` 为干净、已推送的 `f0ada0a`。
PR #189/#190 均 OPEN，#190 包含 #189。开始前检查 WSL 进程及桌面任务，未见其他写入者。
旧数据、旧报告和旧回执保留；本次审查产物单独写入 `data/research_review_20260918`。

## 首批证据清单（修复前行号）

| 状态 | 事实与影响 | 代码与本地产物 |
|---|---|---|
| 已证实问题 | 月末名单根据通用调样日倒推半年调整身份，未逐次绑定官方公告；临时调整用观察日冒充生效日；最后区间无限延长。800只不能证明身份或可得时间。没有发现全部月份倒填月初，已排除这一特定实现，但不是排除其他前视。 | `pipeline/evidence.py:54-156`；`data/evidence/csi800_membership.json`、116个 `provider_observations/csi800_weights` 月度目录 |
| 已证实问题 | ST无抽验时只要有会话就 complete=true；有抽验时以公告日期±10日且不检查公告含义的70%匹配率认证全部会话。2354个已封存分区证明完整性哈希，不能证明事件全集。 | `scripts/build_csi800_evidence.py:465-564`；`data/evidence/receipts/event_coverage.json`（105样本但只保存40个） |
| 尚未证实风险 | 17:00是发布时间情景，不是历史修订快照证明。行情/每日指标有供应商更新时间说明，不能推广到全部数据。下游bundle仍标 historical_publication_certified=false，这一点已排除“元数据已获认证”。 | `pipeline/evidence.py:164`、`pipeline/features.py:257`；`data/evidence/source_availability.parquet` |
| 已证实问题 | cash_div_tax是税前，代码注释误认税后；送转与现金并存时只产生送转事件；红股上市日未使用。 | `pipeline/evidence.py:589-674`；`data/evidence/raw/dividends`、`data/evidence/corporate_actions.json` |
| 已证实问题 | 送转余数一律舍弃，不能从供应商比例推断本账户实际获配整数；现有声明不能算中国结算实际规则验收。 | `research/ml/corporate.py:125-149` |
| 已排除（限所查路径） | 缺涨跌停数值及转换为None的哨兵不会默认可交易：股数内核要求上下限，不满足则拒单。哨兵转换注释声称无门槛与实际行为不符，所引doc_id=181实际是行业分类。 | `pipeline/market.py:21-47`；`research/quantity_kernel.py:285-306` |
| 已证实展示问题 | 总报告截取所有模型共同完成的108日，Ridge完整726日收益未在主表出现，交接将108日+2.32%/2.91%误配给726日。 | `research/ml/reporting.py:164-185`；`data/experiments/csi800_v2/report/report.md` 与 `replay/summary.json` |

以上是证据盘点，不是策略收益认证。后续更新将记录修复、实际运行与阻断。
