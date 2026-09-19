# 历史中证800项目：操作、语义与验收

当前目标是完整的个人日频研究与按需回测；持续模拟是可选阶段。电脑无需每天开机，
历史交易日可以批量回放。代码和合成测试可在无行情的云端验收；真实来源覆盖、策略表现和容量需本地数据验证。
只有启用持续模拟后才需要连续运行验收。不能把“有测试、有发布按钮”称为已经证明优秀或盈利。

## 1. 一份策略，三种不同的集合

`config/project.example.json` 选择 v2、基准 `000906.SH`；`config/strategy.csi800.json`
冻结股票池政策、研究区间、资金/滑点情景和模拟发布门槛。

1. **原始数据集合**：保留历史证券，包括 ST、调出、退市证券及代码变更。不会删除非当前800成分的历史数据。
2. **当天研究集合**：当天历史中证800成分，剔除当天已知 ST/*ST，上市年龄至少120个已验证交易日。
3. **当天新买集合**：研究资格成立，最近20个交易日成交额资料完整，中位数至少2,000万元。
   科创板、创业板仍属于中证800研究范围；其实际撮合须有对应日期的数量、费率和交易规则。
   当前只有模拟权限，不能据此推断实际券商账户已有这些板块的交易权限。

`eligible` 仅决定研究样本；`can_open` 限制新买；`must_exit` 表示已知ST硬风险；
`soft_exit` 表示调出中证800。流动性短暂不足不强制卖出；调出按已有最短持有期、替换数和换手预算退出；
ST风险退出优先于普通换手预算。上述都是目标变化，实际能否卖出仍由停牌、跌停、T+1、数量和成交容量决定。
退出研究池的股票继续留在上下文中，已有股数、股息应收和估值不会消失。

这意味着过滤后可研究的数量可能少于800，但基础池始终是**当时的中证800**，不是另选一批表现好的股票。
初始建仓和硬风险退出单独计量，不把它们隐藏进“低换手”平均数。

## 2. 数据与来源接入

`sync --execute` 使用同一 TuShare 传输层：有限重试、节流、实际响应归档、单位转换，随后事务式封存一个完整交易日。
ST 1000行、停牌5000行触顶不得封存；旧回执也补查事件行数。stock_basic 6000行触顶拒绝。
证券主表包含上市/退市/暂停上市状态，行业不取今日 stock_basic 行业回填历史。
已封存数据有哈希，不原地修订。更换基准时，旧回执没有000906数据会明确报错。

### 月度指数资料与精确成分不是一件事

```bash
uv run quantlab pipeline --project config/project.local.json sync-index-observations \
  --start 2017-01-01 --end 2026-08-31 --execute
```

这个可恢复的命令按月保存 `index_weight` 的观察日、权重、实际下载时间和原始响应。
每个观察截面检查800个唯一成分和权重合计，空/不完整资料不标成成功。
它**不会**把月末快照倒填整月，也不会捏造公告日或调样生效日。
TuShare明确该接口提供月度数据：[官方文档](https://tushare.pro/document/2?doc_id=96)。
基础指数资料：[中证800官方资料](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000906factsheet.pdf)。

### 本地需绑定的来源格式

这些不是让用户手工填写数百万行：本地 Codex 应从原始供应商资料、官方调整公告和已有来源适配器生成，
保留原始文件、来源/版本和时间；无法证实的字段保持缺失并报告阻断。

**membership JSON**：

```json
{
  "schema": "quantlab_index_membership_v1",
  "index": "000906.SH",
  "semantics": "published_effective_intervals",
  "snapshots": []
}
```

每个 snapshot 必须含 `start/end`（含端点的实际生效区间）、带时区 `known_at`（公告/资料可得时刻）、
`source_id/revision_id`、`complete: true` 和800个唯一 `members`。示例空列表**不能通过验收**。
全成分列表可由上一个完整快照加有出处的调入调出事件重建。普通情况下两个区间不得重叠或有空档。
官方若确有临时非800个成分，应提交原始证据、明确例外与测试后扩展协议，不能随意改数量以放行。

**industry JSON**：`{"intervals": [...]}`，每行含 `instrument_id/start/end/known_at/source_id/revision_id/industry`。
研究集合要求明确行业。长期调出且未持有的证券可以保留未知行业，不伪造当前分类；
若实际仍持有它，组合决策会明确阻断，而非记成“其他”掩盖风险。

**event_coverage JSON**：`{"schema":"quantlab_event_coverage_v1","stock_st":[...]}`。
每段含 `start/end/known_at/source_id/revision_id/complete`；它证明相应区间的 ST 数据覆盖。
空 ST 响应本身不能证明当天“全市场无 ST”。
依据接口可用历史、原始请求范围和独立抽样核验生成覆盖说明，不能仅因请求返回200就写 complete。

**source_availability.parquet**：`trade_date/known_at/source_id/revision_id`。
历史可得时间需证据；今天下载旧数据的时间不能充当历史发布时间。
前向特征还自动取真实入库观察时刻作为下界，迟到就不能成为当天合格决策。

**execution_policy / corporate_actions**：仍沿用原手册的数据协议，必须绑定分时期费用、最小佣金、
数量规则、成交参与率、涨跌停和公司行动覆盖。费用不自动设零；分红采用明确的净额和到账日。
公司行动支持现金分红、整数送股/拆股；配股、合并换股、退市结算、持有期差异税等未支持事件仍明确停止。
本地遇到这些事件时应补有证据的专项适配和会计测试，不能删掉事件或跳过亏损日期。

`universe` 自动把上述来源和 Canonical 拼成逐日资格、原因码和风格暴露，按天写 Parquet，
归档原始输入副本及哈希，避免将多年800股票资格一次全部装入 Python 对象。
风格报告含流通市值对数、成交额对数、60日年化波动与对中证800的60日 beta；窗口缺项保留未知。
Alpha158 中名字为 BETA 的特征是其原始表达式含义，不能冒充这里的市场 beta。

## 3. 正式研究与模拟准入

```bash
uv run quantlab pipeline --project config/project.local.json plan
uv run quantlab pipeline --project config/project.local.json sync --execute
uv run quantlab pipeline --project config/project.local.json universe
uv run quantlab pipeline --project config/project.local.json init-study
uv run quantlab pipeline --project config/project.local.json build
uv run quantlab pipeline --project config/project.local.json train --resume
uv run quantlab pipeline --project config/project.local.json replay --resume
uv run quantlab pipeline --project config/project.local.json report
uv run quantlab pipeline --project config/project.local.json stress --resume
```

以上是按需研究主线；完成报告和压力测试即可结束本次研究。以下仅在准备启用前向模拟时执行：

```bash
uv run quantlab pipeline --project config/project.local.json register --fold YYYY-MM --model lightgbm
uv run quantlab pipeline --project config/project.local.json release --model-id 返回的ID
uv run quantlab pipeline --project config/project.local.json activate --model-id 返回的ID --effective-from 将来或当天日期
```

默认 `development_end=2025-12-31`、留出候选区间2026年1月至8月。项目过去看过2023–2026结果，
因此这个文件**不能把已看过的历史重新变成未见测试集**。本地须如实记录历史暴露。可预先冻结方法，未来开机时批量评估新增未见区间；
这种历史回放仍不等于当时按时封存信号的前向模拟，不能证明每日运行可靠性。
`phase=final_holdout` 时，test区间必须落在声明留出期；主入口传入 study，首次留出规格被锁定。
开发试验不得越过开发截止日。策略、成本矩阵、发布门槛改变应新建研究协议；不能看完测试结果后改门槛来通过。

标签保持 `复权收盘(t+11)/复权收盘(t+1)-1`，按日 rank；它是研究目标，不是承诺成交收益。
固定 Alpha158；Ridge 与浅 LightGBM；882日训练、126日验证、按月前推；训练/验证仅使用已经成熟的标签。
Ridge的缺失填充、截尾和缩放只在训练段拟合，LightGBM保留缺失处理；不强制对所有模型做一套机械中性化。
每次拟合记录验证rank IC、可计算天数、模型参数和特征参考统计，支持模型重新加载一致性核验。

默认20只、80%股票预算、单股5%、行业20%，每5个交易日普通换仓，entry30/exit60、最短10日、至多替换5只、
普通目标单边换手上限25%。组合依据实际持仓/现金，不能把卖不出的股票当已卖、不能花尚未收到的卖款。

回放默认5万/20万/100万独立账户；stress在原费用基础上分别将**每边总不利滑点**设为5/10/20bp，
重新运行成交、资金和仓位路径，不是在同一收益曲线上简单扣一笔钱，也不是将这三档再叠加原滑点。
报告包含同期净收益、基准、相对净值、IC分块置信区间、逐年结果、实际暴露、风险偏离、覆盖与终止原因。
附加的股票预算×指数收益对照是每日再平衡、现金零利率、无费用的诊断参考，不是另一条可实施策略。
当前000906为价格指数；组合含明确股息，两者分红口径不同，不能把全部相对收益宣称为alpha。
同池等权已接入独立回放；仍需核验小资金下的实际仓位。总收益基准与因子组消融仍需本地接入及实验。

`register` 只保存候选；`release` 逐项核对匹配的已登记试验、完整情景区间、正向信号证据、
投资过的账户、风格覆盖与风险偏离。默认门槛252个交易日、回撤不超过25%、相对价格指数收益不低于0；
这是明确声明的**保守项目政策**，不是金融行业的统一合格线，也不保证未来表现。
所有资金/成本情景要通过；未通过便保留候选与阻断理由，不自动调参补成绩。
发布产物绑定训练、报告、策略和真实创建时刻；持续账户也核对它，不能借高级 `ml activate` 绕过。

## 4. 可选每日模拟，以及备份与恢复

仅开展研究回测时，跳过账户初始化、daily、health、monitor和定时器部署；仍需保存并验证研究备份。
不要用daily补跑历史研究，也不要把事后回放伪装成当天已发布的订单。

启用持续模拟时，首次账户与策略/特征契约冻结。换股票池后应新建隔离账户，不能给旧账户直接换标签。

```bash
uv run quantlab pipeline --project config/project.local.json init-account --as-of 日期
uv run quantlab pipeline --project config/project.local.json daily --as-of 日期 --sync --execute
uv run quantlab pipeline --project config/project.local.json health
uv run quantlab pipeline --project config/project.local.json monitor --as-of 已结算日期
```

每日顺序是：实际到达的数据 → 同一资格/特征代码 → 结算昨天封存订单 → 核对现金、股数、公司行动与估值 →
使用已准入、未过期的模型评分 → 风险与换手控制 → 封存下一日订单。
18:00上海时刻后只能补记账，不能补造旧信号。暂停、过期、缺模型、迟到等情况，账本可能已结算，
但正常前向决策没有完成，命令返回非零，operations记录受阻。
V2模型失效时，如持仓的风险事实仍完整及时，可另外封存仅卖出的硬风险减仓；依然向调度器报受阻，
不能冒充正常模型决策。这种减仓一旦封存也不可在当日改成另一套订单。

如果执行政策明确声明 `stale_valuation`，已证实停牌且没有原始价格时可最多沿用20个交易日以内的最近原始收盘估值：
`mode=last_raw_close_known_halt`、`max_sessions`（1–20）、`source_id`、带时区 `known_at`。
缺口中每个无价日必须有停牌证据；跨除权/分红事件不得直接沿用，须有明确估值覆盖。
沿用值只进入估值，交易上下文的原始成交价仍为空，并记录估值来源和陈旧天数。

月度更新：

```bash
uv run quantlab pipeline --project config/project.local.json refresh \
  --as-of 新观察日 --parent-model-id 已准入模型ID --resume
```

它使用当时已成熟标签重新拟合同一种模型、同一窗口和参数，验证段rank IC须为正且有足够天数。
模型/特征契约、策略、代码或运行时变化要求重新研究准入；不借更新名义重新选参数。
候选通过只获得“同方法更新的模拟观察资格”，不自动activate；验证失败保持旧启用记录。
原模型到45日仍未有有效替换会阻断新风险，不能无限期静默使用旧模型。
月度候选更新继承的是方法层面的历史检验，**不是新模型获得了完整的新样本外收益证明**。

`monitor` 只取账户实际采用、按时发布的信号（包括成功重试版本），回收已成熟的10日标签。
未成熟不评分；端点缺失计入覆盖缺失。展示最近成熟信号IC、分块区间和特征漂移。
IC区间整体负、成熟标签覆盖低于95%、超过20%特征均值漂移大于训练3个标准差会告警并返回非零。
告警不自动改模型，也不倒改过去持仓。

```bash
uv run quantlab pipeline --project config/project.local.json backup --destination 隔离备份路径
uv run quantlab pipeline --project config/project.local.json verify-backup --path 隔离备份路径
uv run quantlab pipeline --project config/project.local.json revision-plan --start 日期 --end 日期
```

备份会复制配置指定数据/产物，可能很大；目的地必须与所有来源隔离，拒绝符号链接，逐文件核验后原子发布。
verify-backup只证明副本内容完整，不等于已经演练过恢复。绝对路径绑定要求在原路径恢复，或用经过验证的显式迁移。
revision-plan找出已封存分区的差异和受影响环节；重新取数、重算必须进入新数据版本/新workspace，旧实验和账本保留。
它不是一个会擅自覆盖旧数据或修复真实账户现金的命令。

UI可通过 `QUANTLAB_PROJECT=/绝对路径/project.local.json` 读取同一个项目账户路径。
可选定时器模板见 `scripts/quantlab_daily_job.py`；仅在明确启用自动模拟后确认时区、交易日历、
WSL睡眠/启动条件和日志，再部署。当前按需研究不安装。

## 5. 调研差距逐项落地状态

| 环节 | 本次代码落地/保留 | 尚需本地事实或实验 |
|---|---|---|
| 投资任务 | v2策略规格、中证800、时钟/组合配置绑定 | 确认实际账户板块权限和规模 |
| TuShare | 请求归档/重试沿用，ST/停牌/主表触顶修复，指数观察入口 | 权限、空响应原因、真实覆盖核验 |
| 数据版本 | 事务回执、原始证据副本、修订影响计划 | 新命名空间取数、恢复演练；未做自动覆盖修订 |
| 身份/日历 | 日期化代码映射沿用、股票池上市年龄 | 历史代码、退市与特殊证券事件证据 |
| 股票池 | 有效期成分、ST、上市年龄、流动性、原因码 | 官方调样事件与可得时间 |
| 研究/新买/退出 | 已拆分，历史退出持仓保留 | 数据真实状态抽样 |
| PIT/行业 | 来源区间、时区、截断、特征契约一致 | 公告/修订历史认证不能由程序推断 |
| 因子/预处理 | 固定Alpha158及现有表达式/依赖契约、训练内拟合 | 分组消融、冗余与增量收益实验 |
| 基本面/另类 | 旧PIT组件保留，不无证据并入主模型 | 财报披露与修订证据齐后再接入 |
| 标签/验证 | 成熟标签、月度purge/早停保留，新增验证IC与成熟回收 | H=5/20等少量预登记对照 |
| 研究治理 | study接主入口，策略冻结与匹配试验检查 | 已看过历史如实标记、积累真正前向证据 |
| 统计评价 | IC分块区间沿用，逐年结果和覆盖 | 完整试验台账下评估多重检验；没有宣称通用IC门槛 |
| 组合/风险 | 实际持仓缓冲、买卖预算、ST硬退出、模型失效仅减仓 | 是否需要更严格风格约束须实证 |
| 风格报告 | 规模/成交额/60日波动/市场beta、未知覆盖 | 真实暴露及行业分类验收；未伪装成Barra归因 |
| 执行/容量 | 分资金、分每边滑点重新回放，拒单/部分成交沿用 | 收盘撮合、参与率与滑点校准 |
| 停牌估值 | 明确声明的陈旧价格政策、除权边界与来源 | 长停/重大事件估值与公司行动专项适配 |
| 收益基准 | 000906价格指数、预算匹配诊断、逐年报告 | 总收益指数、同池可实施等权基准尚待接入 |
| 模型发布 | 候选/准入/启用分开，账户核验、真实时刻、压力门槛 | 无真实结果时不发布“有效”结论 |
| 持续更新 | 同冻结方法候选refresh，验证失败保留旧模型 | 本地耗时/内存、更新与激活操作演练 |
| 监控调度 | 非零失败状态、健康、成熟标签、漂移告警、任务脚本 | 至少20交易日运行验收，随后长期观察 |
| 恢复/UI | 数据备份/自检、项目路径UI、旧账户保护 | 断网、磁盘满、断电、迁机演练 |
| 历史与真实账户 | 不改旧实验收益口径、personal账本独立 | 券商交易/订单对账未接入，属于未来单独任务 |

参考思想来自 [Qlib Workflow](https://qlib.readthedocs.io/en/latest/component/workflow.html)、
[LEAN 动态股票池](https://www.quantconnect.com/docs/v2/writing-algorithms/universes/key-concepts)、
[LEAN Reality Modeling](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/key-concepts)、
[Alphalens](https://quantopian.github.io/alphalens/)、
[Cvxportfolio](https://www.cvxportfolio.com/en/stable/)。采用其时间、证据、风险和生命周期做法，不安装另一整套框架。

## 6. 哪些旧东西不要继续作为主入口

新研究/日常使用 `pipeline` v2；旧顶层 `daily`、`shadow`、阶段S*实验只用于历史复现。
`ml` 是高级底层接口，不是绕过v2研究或准入的捷径。共享股数/现金内核、公司行动、PIT、数据适配和personal真实流水必须保留。
先归档旧实验产物并建立依赖清单，再考虑本地磁盘清理；本次没有删除旧数据或改写历史回测。
