# 龙虎榜、大宗交易与讨论样本

本轮增加交易披露与用户评论两类输入，用于发现和调查候选。它们不自动加分，
不会被解释为涨停概率或资金意图。现在可运行合成演示：

```bash
uv run scout --demo
```

演示包含明确标注的虚构榜单、相同席位的买卖榜记录、大宗交易和重复评论。
不调用外部接口，不改写真实行情，没有真实选股结论。

## 联网获取交易披露

配置本地 `TUSHARE_TOKEN` 后，`--live` 默认请求截至行情截点的最近 3 个 SSE 交易日，
每个交易日调用一次 `top_list`、`top_inst`、`block_trade`，合计最多 9 次请求。
`disclosure_sessions` 可设为 1—5，`tushare_disclosures: false` 可关闭联网获取。
没有 token、离线模式或关闭开关时，不联网，用覆盖状态说明缺失。

权限不足、接口异常、空返回和可能截断分别显示；没有自动重试或自动向前找一个有数据的日期。
单接口只保留响应前 1,000 行，达到上限即标记 `possibly_truncated`，不声称全量覆盖。
空返回为 `empty_unconfirmed`，可能尚未发布或未覆盖，不能解释成没有相关交易。

| 来源 | 保留和展示 | 不作出的推断 |
|---|---|---|
| `top_list` | 上榜原因及供应商原始数值，按交易日期分开 | 文档未明确的金额单位不猜测；不跨窗口合计 |
| `top_inst` | 席位、买入／卖出金额（元）、买卖榜侧、原始上榜原因 | 不把营业部当作具体投资者，不合计为全市场净流入 |
| `block_trade` | 价格、数量、买卖方营业部；相对同日未复权收盘价的折溢价 | 不把折价当作必然利空，不猜测交易目的 |

龙虎榜相同日期、原因、席位及数值的记录合并展示，保留买榜／卖榜侧，记录
`duplicate_or_indistinguishable_rows`。这只处理重复或无法区分的披露记录，不识别真实资金主体。
单日与多日榜保持原始 `reason`，不相加。原始规范化快照保留在报告中。
`trade_date` 是披露统计窗口的截止交易日，不保证是单日资金。系统从 `reason` 标注
`single_session`、`multi_session` 或 `unknown`，并将窗口标签送入 AI；无法确认窗口时
保留未知。多日累计榜不能和单日成交额比较，重叠的单日榜与多日榜不能推断连续净买。

大宗 `vol` 从万股换算为股；展示的推导成交额使用 `price × vol × 10000`。
供应商 `amount` 原值保留，当前文档未明确单位，不自动换算。完全相同的大宗行也可能是
不同成交，因此保留，不直接去重或合计。折溢价使用**该笔交易当日**的收盘价，缺失时为 null。

事件日期、发布时间、获取时间分别保存。在线接口未返回发布时间时保留 null，不能把交易日
当作当时已经获知数据的时刻。当前抓取是当前研究快照，不可用于声称历史 PIT 回测能力。

接口文档：

- https://tushare.pro/document/2?doc_id=106
- https://tushare.pro/document/2?doc_id=107
- https://tushare.pro/document/2?doc_id=161

## 导入已取得的披露数据

使用 `config/scout_disclosures.example.json` 作为结构模板，替换日期和真实数据：

```bash
uv run scout --offline --canonical-dir /path/to/canonical \
  --disclosures /path/to/disclosures.json
```

每个 snapshot 包含 `dataset`、ISO 格式 `trade_date`、含时区的 `retrieved_at`、可为空的
`published_at`、`rows` 和可选 `possibly_truncated`。`rows` 使用对应 TuShare 接口的原字段，
必须包含 `ts_code`、`trade_date`；行内日期接受 YYYYMMDD 或 YYYY-MM-DD。
`top_inst.side` 使用 `"0"` 或 `"1"`；大宗记录需正的 `price`、`vol`。

每组仅允许一个“接口＋交易日”快照，日期须属于本轮配置的最近 N 个交易日。
发布时间不得晚于获取时间，获取时间不得来自未来或早于交易日期。
缺少的快照标为 `not_provided`；格式或时点冲突会拒绝整个导入。
文件最多 10 MB，单快照最多 1,000 行。

显式传入文件时替代该轮的交易披露联网请求，即使使用 `--live` 也不会再请求这三个接口。
用户文件的来源和时间未独立核实，报告标为 `user_import_unverified`。

## 导入同花顺等平台的评论样本

目前没有自动登录、爬取或完整评论流。支持你合法取得的导出样本；格式参考
`config/scout_comments.example.json`，所有示例时间和文本都需要替换。

```bash
uv run scout --live --canonical-dir /path/to/canonical \
  --comments /path/to/comments.json
```

顶层需要来源 `source`、采样方式 `sampling_method`、获取时间 `retrieved_at`、
采样窗口 `window_start`／`window_end` 与 `comments` 列表。
时间均需时区。窗口采用左闭右开，每条 `published_at` 必须落在其中，
窗口终点不得晚于获取时间。未来数据会被拒绝。

每条评论需要 `instrument_id`、`text`、`published_at`；可选 `comment_id`、`url`、`author_id`。
最多 500 条，单条正文 2,000 字符，文件最多 2 MB。仅使用当前合格股票的评论。
同一来源的 `comment_id` 必须唯一；重复相同记录去重，ID 内容冲突则拒绝导入。
账号标识只保存哈希，不在报告中保留原始 `author_id`；正文仍按原文保存，请自行检查导出内容。

每只股票统计样本条数、不同文本数、重复文本比例及样本中可识别账号数。
这些数值不代表独立投资者、平台总讨论量或持仓。没有可比的历史采样，`heat_growth`
保持 null；不生成情绪分数或胜率。每只股票仅取最多 5 条不同文本进入调查，
优先较新的文本，已知发布时间超过 `lookback_hours` 的证据不进入模型。
样本完整统计与实际进入模型的文本可能不同，两者的时间窗口、筛除数分别保存。

## 如何影响研究与比较

- 原有量价排序分数保持不变；披露与评论可以为合格股票增加信息入口。
- 新线索按既有量价分数稳定排序，不按评论数量或席位名气提高买入分数。
- AI 调查会看到披露与评论，核实其中说法，比较同题材股票，并解释与量价的冲突。
- 只有未核实讨论／名称联想入口的股票不能被最终校验接受为 focus，可列 watch。
- 专项调查和最终比较的证据列表各限制在 100 条、60,000 个序列化字符以内，
  单条正文最多 3,000 字符。截断标识与实际送入的证据 ID 记录在 `prompt_evidence_audit`。
  原始规范化快照完整保留在报告里；候选指标／提示词不计入这项证据正文预算。
- 最终引用仅允许使用实际送入最终证据包的 ID；引用缺失或流程失败不输出 AI 推荐。

`report.json` 增加 `disclosure_snapshots`、`disclosure_context`、`discussion_snapshot`、
`discussion_context`、`source_comparison` 和输入审计记录。
中文报告展示披露与评论摘要，以及加入来源前后的候选池差异。
后续观察增加 `rule_baseline`、`ai_focus`、`ai_watch` 分组和来源标签。

这只是候选发现对照和分组记录，**不是单来源 AI 消融试验或收益贡献归因**。
模型调用仍最多 3 次，没有额外付费生成多组推荐。选股增益需要后续前瞻数据验证。
