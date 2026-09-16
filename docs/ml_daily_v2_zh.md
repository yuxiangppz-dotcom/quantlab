# 日频机器学习研究主流程 v2

状态：云端已实现研究、账户回放、恢复运行、模型登记、每日预测、前向模拟和报告的统一命令流程。
真实数据来源完整性、市场规则和策略有效性仍待本地验收；不具备券商交易权限。

本次直接用户委托从 `ad5e16147929e8f6a35ede10936086c1adf691d6` 开始。
目标是把现有组件连成一致的研究链路，不以解释某条 -98% 曲线为前置条件。
旧 `alpha158_rolling_v1`、生命周期回测和已冻结实验保持原语义，避免改完后旧结果失去出处。
新股票 ML 实验统一使用 `quantlab ml`。UI 默认 ML 工作台；新 ML registry 与旧策略注册系统相互隔离，
不会自动切换旧策略或修改真实账户。

## 统一的数据流

1. 读取已有、封存的 Alpha158 特征/原始价格分区以及明确的历史 PIT 上下文。
2. 导出只读来源绑定的输入包；校验唯一身份、交易日历、特征可用时刻和缺失比例。
3. 构造与执行时点一致的标签，按月生成严格成熟的训练/验证集。
4. 每模型每月只训练一次，保存参数、预处理、模型、日期边界与 OOS 打分。
5. 对实际股数账本估值；根据实际持仓做缓冲选股、行业/单股/仓位约束。
6. t 日收盘后确定目标股数，交给 t+1 收盘研究交易内核；按实际模拟成交更新股数和现金。
7. 分别输出信号诊断和逐日数量账本；未知证据停止路径，保留截止日和未卖出持仓。

## 默认研究假设

以下是便于验证的一组起点，不是收益承诺，也不是所有机构的统一参数。
完整参数定义在 `src/quantlab/research/ml/config.py`，覆盖配置在 `config/ml_daily_v2.json`。

| 环节 | 默认值 | 原因与边界 |
|---|---|---|
| 决策时刻 | t 日北京时间 16:00 | 特征可用时刻必须带时区且不晚于此时 |
| 执行口径 | t+1 收盘 | 对齐已有 quantity scheduler；没有混入开盘成交假设 |
| 标签 | adjusted_close(t+11) / adjusted_close(t+1) - 1 | 持有跨度 10 个市场交易日；缺端点留空、不滑到下一有效价格 |
| 学习目标 | 每日横截面 rank，均值归零 | 降低极端收益和市场共同分量影响；原始评估收益不截尾 |
| 特征 | 复用 Alpha158，显式 allowlist | 拒绝 future/label/target 列；不用全期统计填充 |
| 缺失 | 至少 80% 特征有效 | LGB 原生 NaN；Ridge 训练期中位数填充、0.5%/99.5% 截尾、标准化 |
| 训练/验证 | 882 / 126 个交易日 | 约 3.5 年训练 + 半年验证，合计 4 年历史 |
| 更新 | 每月重训，预测下一月 | 训练标签结束必须早于验证首日；验证标签结束不晚于拟合截止日 |
| 样本权重 | 每个交易日相同总权重 | 不让股票较多的后期日期自动获得更大权重 |
| 基线 | Ridge 与 LightGBM | 必须在相同 OOS、组合与成本条件下比较 |
| LGB | MSE、lr .03、15 叶、深度 4、叶最少 1000、L2=10 | feature/bagging .8、bagging_freq=1；最多 1500 轮、验证早停 100 |
| 可选目标 | binary / lambdarank | 默认不搜索；分类 top20%，排序按日期分组、10 档线性 label_gain |
| 调仓 | 每 5 个交易日 | 打分每天产生，正常调仓与风险检查分开 |
| 持仓 | 最多 20 个、目标总仓位 80% | 每个新仓 4%，不足时留现金 |
| 缓冲 | 新仓 rank<=30，旧仓 rank>60 才考虑退出 | 最少持有 10 个交易日，每次正常退出/新增各最多 5 个 |
| 风险 | 单股 5%、行业 20%、总仓位 80% | 这是目标阈值；0.5 个百分点执行/估值容差避免微小价格漂移引起碎单 |
| 换手预算 | 常规每次单边 <=25% | 0.5×股票买卖绝对权重差；初次建仓和硬风险减仓单独标记 |
| 小单过滤 | 3000 元 | 风险退出和清仓可例外；还要过真实的整手和费用约束 |
| 资金规模 | 分别模拟 5万 / 20万 / 100万元 | 不线性放大单位 NAV；最低佣金、股数格和参与率会导致非线性差异 |

每次实际成交的单边换手定义为 `(买入金额+卖出金额)/(2×上一完成日权益)`。
目标换手预算与实际成交换手不是同一个指标；跳空、股数舍入、无法成交会产生差异。
Top20 名单变化单独命名 `top20_membership_change_not_turnover`，不能冒充交易换手。

默认要求完整 1008 个历史交易日，不会静默缩短窗口。如果现有历史只从 2020 年开始，
不能用此默认配置声称从 2023 年开始做四年窗口的前向训练。应推迟首个预测日；
如明确选择三年窗口，另存一份配置并记录这是一项新的研究假设。
已观察过的 2023–2026 年只称历史 walk-forward，不称全新未见测试集。

## 输入包

`manifest.json` 绑定以下四个必需文件的 SHA-256；封存后的输入不能原地更新。
还可绑定 `pit_lineage.parquet` 和 `feature_contract.json`，两者必须一起提供。

| 文件 | 字段/格式 |
|---|---|
| `features.parquet` | trade_date、instrument_id、feature_available_at、eligible、industry、显式特征列 |
| `prices.parquet` | trade_date、instrument_id、adj_close；用于标签，绝不作为股数成交价格 |
| `calendar.json` | 完整、递增、不重复的市场交易日期字符串数组 |
| `feature_names.json` | 特征名称 allowlist 数组，顺序固定 |

`eligible` 只能是 true / false / unknown。历史未上市、退市边界、ST、流动性、上市天数等
应由本地 PIT 适配器产出，不能用今天的股票池追溯历史。行业也必须是当时有效的分类。
未知的非持仓标的不新买；已持仓标的的资格或行业证据缺失，不能自动当作已卖出。

导出已存在的封存历史：

```bash
uv run quantlab ml export-history \
  --history data/experiments/EXISTING_SEALED_ALPHA158_HISTORY \
  --pit-context data/experiments/ml_v2_inputs/pit_context.parquet \
  --output data/experiments/ml_v2_inputs/bundle
```

`pit_context.parquet` 恰好包含上表前五个元数据字段；它的行键可以限制研究区间和股票池。
导出器流式写分区，保留标签所需的价格日期，并验证所有上下文键恰好被特征覆盖一次。
封存只证明字节绑定，不证明供应商数据无遗漏或历史可用性真实。
特征/价格输入包和训练矩阵有显式内存上限；超限时停止，不偷偷抽样。
全市场多年数据应先合理限定 PIT 股票池/区间或在有内存预算的本地机器上调整上限。

检查和训练（日期仅为命令示例，须按本地覆盖范围确定）：

```bash
uv sync --frozen --extra research --extra qlib
uv run quantlab ml check --bundle data/experiments/ml_v2_inputs/bundle \
  --start 2024-02-01 --end 2026-08-31
uv run quantlab ml train --bundle data/experiments/ml_v2_inputs/bundle \
  --start 2024-02-01 --end 2026-08-31 --output data/experiments/ml_v2_run_001
```

真实训练要求干净、已提交的代码。新实验创建新目录；中断后使用原命令加 `--resume`，
只复用输入、配置和代码完全一致的已封存月份，失败记录保存在 `failures/`。
特征按月份训练窗口、历史 eligible=true 读取，价格按必要证券和标签端点过滤，
不再全量读取整个历史面板。单个训练窗口超内存仍会明确停止，不能静默抽样。
记录包含 Git HEAD、运行时版本、输入指纹、配置指纹、每次拟合日期与最优迭代数。
模型用 LightGBM 文本和 Ridge NPZ 保存，不加载外来 pickle。

## 交易证据与数量回放

回放复用 `quantity_kernel.py` 和 `quantity_scheduler.py`，没有另造一个简化成本引擎。
必须传入 `market_days.jsonl`，每行对应一个实际市场交易日，字段是：

```json
{
  "session": "2024-02-02",
  "corporate_processing_complete": null,
  "marks": [{"instrument_id": "600000.SH", "price_fen": 1000}],
  "contexts": []
}
```

这是**未准入模板**，会因公司行动/上下文缺失停止，不是一条有效可交易行情。
`contexts` 是现有 `ResearchSession` 的字段映射：execution_date、next_session、
evidence_date、calendar_verified、market_open、corporate_actions_processed、
raw_close_fen、low_fen、high_fen、down_limit_fen、up_limit_fen、prior20_amount_fen、
prior20_asof、prior20_sessions、session_amount_fen、session_volume_shares、participation、rules、fees。
日期用 ISO 字符串，Decimal 费率用字符串，金额是分、成交量是股。
rules/fees 的字段与已有 `ResearchQuantityRules` / `ResearchFeeScenario` 完全一致。

特别注意：

- t 日已知的过去 20 日成交额用于事前容量预算；t+1 的成交额/成交量只用于模拟成交约束。
- 股数由 t 日原始收盘价和实际现金/持仓确定，不能拿 t+1 的价格重写历史目标。
- 佣金费率、每笔最低佣金、买卖印花税、其他费用、滑点都要显式给出且按生效日期分段。
  已知项目报价尚不能自动变成历史已核验费率；未知字段保持 null。
- 不能把所有股票一律写成 100 股一手或 10% 涨跌停，必须使用生效日/板块对应规则。
- 当前内核是收盘情景成交约束，不是逐笔撮合证明。参与率、滑点和封板成交可行性仍是研究假设。
- 缺估值/公司行动证据时停止完整路径，不把停牌价格自动前填并继续输出“完整回测”。
- 公司行动使用明确事件和登记日权益快照；现金股息先记应收、到账才成为可买入现金。
  送股在除权日记入股数并锁定至声明的可卖日期；整数比例拆股保持现金不变。
  分红率是显式 NET 情景，不会自动推算 A 股持有期税；相关真实税费仍须适配和核验。
- `corporate_processing_complete=true` 仍只是来源声明，不代替事件文件。没有事件也必须
  提交带来源和覆盖区间的空事件表；不能因为不知道就写空。
- 除权日取消相关证券的旧股数订单，下一决策日再按新股数/价格调整，不猜测交易所调单规则。
- 未卖出的持仓保留。买入准入只使用前日现金、仓位及剩余名额，不预支同次收盘卖出。
  ML 调度显式传入预筹买入现金预算；跳空时即使卖出已模拟完成，新增卖出资金也不能扩大预算。
  这会使满仓换名通常分两次调仓完成，可能多持现金；报告必须显示这项保守假设的影响。
- 已完成成交绝不因收盘风险超限而回滚。事后只报告风险偏离，后续决策再处理；
  目标上限不是每一时刻都能保证的实际仓位上限。
- 风险每天检查，普通换仓按五日节奏；买不到不会在之后每一天自动重试。

`initial_marks.json` 是首个执行日前一交易日的原始收盘估值数组，格式为
`[{"instrument_id":"600000.SH","session":"2024-02-01","price_fen":1000}]`。
回放日期为执行日，故要比首个打分日晚一个交易日，且交易日历需覆盖最后一天的下一交易日。

```bash
uv run quantlab ml replay --bundle data/experiments/ml_v2_inputs/bundle \
  --run data/experiments/ml_v2_run_001 \
  --market-days data/experiments/ml_v2_inputs/market_days.jsonl \
  --initial-marks data/experiments/ml_v2_inputs/initial_marks.json \
  --corporate-actions data/experiments/ml_v2_inputs/corporate_actions.json \
  --start 2024-02-02 --end 2026-08-31 --capital-cny 50000 200000 1000000 \
  --output data/experiments/ml_v2_replay_001
```

每个模型/资金规模有独立 ledger、attempts、positions、decisions、summary、terminal 和
逐日 `sessions/` 检查点，绑定训练结果及市场输入。`positions` 保留持仓批次、可卖日和原始估值。
同输入中断恢复加 `--resume`；完成标志是原子发布的 `completed.json`，可用 `status --path` 核验。
缺少真实证据而主动停止的路径会封存其停止结果；补齐证据后创建新输出，不能拼接两套输入。
不做末日强制清仓，最终权益包含仍持有的股票，报告明确标注。
费用敏感性应预先冻结 5/10/20 bps 等滑点情景，分别绑定输入运行；不能挑最好情景汇报。

## 评估与研究纪律

- 信号：逐日 IC / rank IC、非年化 ICIR、十层真实标签收益、Top20 真实标签覆盖、
  头部收益、名单变动、打分自相关。持有期重叠的标签收益不得直接复利成每日净值。
- 组合：实际模拟成交的日收益、费用、单边换手、零无风险利率口径 Sharpe、最大回撤，
  加上停止日期/原因、拒绝/部分成交、残余现金和目标与实际持仓差异。
- 小样本、停止路径、缺失标签与低覆盖都要报告。不同模型有效区间不同，先按共同区间比较。
- 先比较 Ridge / rank-MSE。分类、LambdaRank、5 日标签等各是一份新配置，记录尝试次数。
  不把同一段历史反复优化后的结果称新 OOS，不自动以某个 IC 门槛晋级实盘。
- 框架提供行业/个股暴露与可选风格暴露报告、双时间源数据选择和 lineage 验证接口。
  不包含风险模型优化器、分钟撮合、供应商修订历史自动补齐或券商下单。
  这些依赖数据、证据和后续独立验收；复杂模型不会自动修复数据与成本缺口。

## 参考依据

- [Qlib Alpha158 数据处理](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py)：
  学习标签处理与预测特征处理分开，时间对齐显式定义。
- [Qlib 组合策略](https://qlib.readthedocs.io/en/latest/component/strategy.html)：
  TopKDropout 的持仓缓冲思想；本实现另外加入实际账本、行业和换手预算。
- [LightGBM early_stopping](https://lightgbm.readthedocs.io/en/latest/pythonapi/lightgbm.early_stopping.html)：
  独立验证集决定迭代数，不拿训练集或测试期决定早停。
- [LightGBM 参数](https://lightgbm.readthedocs.io/en/latest/Parameters.html)：
  LambdaRank 日期分组/整数等级/label_gain；采样同时设置 bagging_freq。
- [Alphalens](https://quantopian.github.io/alphalens/alphalens.html)：
  IC、分层和换手分别诊断，forward returns 的价格时间应与研究定义一致。
- [AQR 动态交易与交易成本研究](https://www.aqr.com/Insights/Research/Journal-Article/Dynamic-Trading-With-Predictable-Returns-and-Transactions-Costs)：
  将信号持续性与交易成本一起纳入持仓调整，而不是每天无条件重置名单。

这些来源支持设计原则；本项目具体参数和保守交易规则仍须用自己的历史证据检验。

## 公司行动文件与证据边界

`--corporate-actions` 为 JSON，包含 `coverage: {start, end, source_id}` 和 `events`。
每个事件都需要 `event_id, instrument_id, kind, record_date, ex_date, settlement_date, source_id`。
现金事件额外需要 `net_cash_per_share_fen`（字符串 Decimal，单位为每股分）；
送股/拆股额外需要正整数 `share_numerator, share_denominator`。
`kind` 仅支持 `cash_dividend / bonus_shares / split`。bonus 的比例表示新增股数，split 表示总股数比例。
现金股息按登记日持股计算，除息日确认应收，到账日或之后首个交易日入现金。
非整数股结算、配股、退市清算和持有期相关税务不推断；需要本地有证据的扩展适配。
应收股息包含在权益中，不能作为提前可用现金。送股未到可卖日期不能卖出。
整个事件处理是纯函数，只有当天完整成功才提交，不会在恢复时重复记账。

## 来源时点与特征依赖

`feature_contract.json` 格式为 `{"feature_dependencies":{"特征名":["source_id"]}}`，
必须恰好覆盖 allowlist。lineage 表含 `trade_date,instrument_id,source_id,known_at,effective_at,revision_id`。
每个日期、证券、来源必须唯一；每个声明依赖都必须存在，并且可知/生效时间不晚于特征可用时刻。
一条来源行表示该特征组已选择的版本或已聚合依赖的最晚可知时间，不把今天下载的时间伪装成公告时间。
`pit.asof_facts` 用于选择“当时已知、当时生效、该有效期最新修订”的源记录，避免最新财报覆盖旧历史。
这些检查证明输入契约自洽，不替供应商的历史公告时点做真实性认证。

已有输入包不原地加文件。使用 `prepare-inputs` 将原文件及新增 lineage 复制封存为新包：

```bash
uv run quantlab ml prepare-inputs --features PATH/features.parquet --prices PATH/prices.parquet \
  --calendar PATH/calendar.json --feature-names PATH/feature_names.json \
  --provenance PATH/provenance.json --lineage PATH/pit_lineage.parquet \
  --feature-contract PATH/feature_contract.json --output data/experiments/ml_inputs_verified
uv run quantlab ml check --bundle data/experiments/ml_inputs_verified \
  --start 2024-02-01 --end 2026-08-31 --require-lineage
```

训练如带 lineage 会自动验证各训练窗口；`--require-lineage` 可禁止使用缺少来源收据的包。
股票池、历史行业、ST 和退市覆盖仍要从真实来源核验；单靠特征命名检查无法证明没有泄漏。

## 评估报告

`benchmark.parquet` 恰好包含 `session,benchmark_return`，收益是同一个交易日的 close-to-close
简单收益率，须明确指数价格/全收益口径，不能混入 H 日标签。每个共同交易日都要有基准。
可选风格表含 `session,instrument_id,available_at` 及数值暴露列（如 size_z、volatility_z、beta）。
报告单列暴露覆盖的净值权重，不会把未知暴露补成零。

```bash
uv run quantlab ml report --replay data/experiments/ml_v2_replay_001 \
  --training data/experiments/ml_v2_run_001 --benchmark PATH/benchmark.parquet \
  --exposures PATH/style_exposures.parquet --output data/experiments/ml_report_001
```

输出 report.md / report.json、共同区间日收益、风格覆盖/暴露。包括净收益、基准收益、
相对净值收益、跟踪误差、信息比率、beta、最大回撤、实际换手、费用、滑点与风险偏离。
分层先固定 score 成员再取标签，各组均显示覆盖；IC 用长度 H+1 的循环分块 bootstrap
给出均值区间，小样本不报区间。它不做多重试验校正，也不是策略有效性的自动通过门槛。
同成交序列成本加回只解释成本；不能称为重新撮合的零成本策略。

## 模型注册、每日预测和信号情景回放

注册只复制已封存、可验证的模型与预处理，记录真实注册时刻。启用独立记日志，不能回填日期。

```bash
uv run quantlab ml register --run data/experiments/ml_v2_run_001 \
  --fold 2026-08 --model lightgbm --registry data/experiments/ml_registry
uv run quantlab ml activate --registry data/experiments/ml_registry \
  --model-id REGISTER_RETURNED_ID --effective-from NEXT_VALID_SESSION
uv run quantlab ml predict --registry data/experiments/ml_registry \
  --features PATH/today_features.parquet --calendar PATH/calendar.json \
  --as-of YYYY-MM-DD --output data/experiments/ml_signals/YYYY-MM-DD
```

每日文件必须恰好一个交易日，复用训练时的特征顺序和预处理，不读取标签，不现场重新拟合。
北京时间 16:00 前产生且模型/特征当时已可用的当日信号才标为 forward_eligible。
截止后补算标为 late_recomputation_not_forward。每天信号封存且不覆盖，监测特征缺失、
训练分布均值漂移和预测覆盖；漂移提示仅作调查线索，不自动调仓或换模型。

`shadow` 使用逐日已经封存的前向信号，不用今天新模型重算过去。CLI 参数与 replay 一致，
将 `--run` 换成 `--signals data/experiments/ml_signals`，并将命令改为 `shadow`。
每个决策交易日都需要信号，即使该日不进行正常换仓。每日更新市场证据后输出一个新的
shadow 截止日期快照；内部仍用相同股数/费用/公司行动内核。不同快照按原信号复放，
不会绕过输入绑定强行续接新增数据，也不构成券商实盘接入。

模型更换需明确 activate；停止定时 predict/shadow 即停止本研究服务，不影响任何真实账户。
新模型不自动晋级，历史收益不自动赋予交易权限。

## 研究登记与操作约定

```bash
uv run quantlab ml init-study --output data/experiments/ml_study \
  --development-end YYYY-MM-DD --holdout-start YYYY-MM-DD --holdout-end YYYY-MM-DD
```

train 可加 `--study ...`；开发实验不能跨开发期边界，`--final-holdout` 只能占用该 study
预先声明的最终区间且绑定一个配置/输入/代码/输出规格。失败重试相同规格可以恢复，换规格拒绝。
登记保留所有试验尝试；它不能抹除此前已经看过该测试区间的事实。已看过的 2023–2026
不重新命名为 untouched holdout。无 study 的实验明确保留 retrospective 标记。

操作顺序是 `status/check → train → replay → report → register/activate → predict → shadow`。
训练和回放 resume 只用于相同代码/配置/输入的进程中断；更新数据、改代码或参数都新建实验。
公司行动、交易证据缺失而停止时先保存停止原因，再补齐证据建立新运行；禁止续造缺失净值。
生成数据、注册表和信号放在被 Git 忽略的 data/experiments 下，代码和文档才提交 Git。

## 进一步参考

- [Qlib 在线模型管理](https://qlib.readthedocs.io/en/latest/component/online.html)：区分训练模型、在线启用和每日信号。
- [LEAN 分层架构](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/overview)：区分选股、组合、风险和执行。
- [LEAN 公司行动](https://www.quantconnect.com/docs/v2/writing-algorithms/securities/asset-classes/us-equity/corporate-actions)：账户事件处理；不能直接照搬其美股规则到 A 股。
- [NautilusTrader](https://nautilustrader.io/docs/latest/concepts/overview/)：研究与运行共享状态和执行逻辑的设计原则。
- [Zipline 数据包](https://zipline.ml4trading.io/bundles.html)：可复用的数据输入与调整信息。
- [VeighNa 多因子研究流程](https://www.vnpy.com/forum/topic/34625-veighnaduo-yin-zi-ru-men-xi-lie-1-mo-kuai-zong-lan-yu-zhun-bei)：个人开发者的本地投研组织方式。

参考这些设计没有引入额外整套量化框架。当前能力是可审计的日频研究/前向模拟，
不是历史数据已认证、逐笔撮合已重现或收益已证实的交易系统。

## 当前日常主入口

每天实际记录组合决策使用 `init-service → run-day → service-state/service-report`，详见
[每日服务手册](ml_service_zh.md)。它与历史回测共用决策/结算函数，逐日追加账户状态；
`shadow` 继续作为基于前向分数的组合情景对照，不是当时已承诺订单的证明。

信号与日决策以 `published.json` 中写入完成后的真实可用时间为准；旧产物缺少此回执不自动升级
为合格前向证据。训练/回放出现输入变化并被标记 invalidated 后，恢复原输入也不能继续复用该运行。
