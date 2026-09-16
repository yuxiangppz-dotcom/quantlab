# 日频机器学习研究主流程 v2

状态：可运行的工程框架；真实数据与策略有效性尚待本地验证。

本次直接用户委托从 `ad5e16147929e8f6a35ede10936086c1adf691d6` 开始。
目标是把现有组件连成一致的研究链路，不以解释某条 -98% 曲线为前置条件。
旧 `alpha158_rolling_v1`、生命周期回测和已冻结实验保持原语义，避免改完后旧结果失去出处。
新股票 ML 实验使用 `quantlab ml`。Daily/UI 和 forward-shadow 不自动切换模型。

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

`manifest.json` 绑定以下四个文件的 SHA-256；封存后的输入不能原地更新。

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

真实训练要求干净、已提交的代码；每次实验创建新目录，失败保留 intent/failed，不覆盖。
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
- 公司行动 cash/share 变动还需要本地接入已有权益处理组件并验证；仅把布尔开关设 true 是错误的。
- 未卖出的持仓保留；买入不能依赖虚构卖出现金。若失败退出或跳空会使买入突破仓位约束，
  当天保守地只执行卖出方案并记录推迟买入，不偷偷放宽约束。
- 风险每天检查，普通换仓按五日节奏；买不到不会在之后每一天自动重试。

`initial_marks.json` 是首个执行日前一交易日的原始收盘估值数组，格式为
`[{"instrument_id":"600000.SH","session":"2024-02-01","price_fen":1000}]`。
回放日期为执行日，故要比首个打分日晚一个交易日，且交易日历需覆盖最后一天的下一交易日。

```bash
uv run quantlab ml replay --bundle data/experiments/ml_v2_inputs/bundle \
  --run data/experiments/ml_v2_run_001 \
  --market-days data/experiments/ml_v2_inputs/market_days.jsonl \
  --initial-marks data/experiments/ml_v2_inputs/initial_marks.json \
  --start 2024-02-02 --end 2026-08-31 --capital-cny 50000 200000 1000000 \
  --output data/experiments/ml_v2_replay_001
```

每个模型/资金规模有独立 ledger、attempts、decisions、summary，并绑定训练结果及市场输入。
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
- 框架目前不实现风险模型优化器、分钟撮合、自动财报 PIT 修订历史重建或券商下单。
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
