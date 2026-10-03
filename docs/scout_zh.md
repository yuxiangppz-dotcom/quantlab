# 多源短线研究助手 Scout

> 当前任务书版本的生成、热榜快照、前瞻跟踪与累计展示，请以
> [Scout 当前版操作说明](scout_taskbook_zh.md) 为准。本页保留旧接口与研究背景，
> 其中规则对照和旧式收盘观察不是当前版本的验收要求。

Scout 是此分支独立演进的只读研究工具。它合并量价异动、板块领先、信息关联三路候选，
使用配置的 OpenAI、GLM-5.3 或 DeepSeek 模型调查，再生成最多 3 只重点观察和 5 只普通观察候选。
它不接券商、不下单，不宣称已找到有效 alpha。旧策略、回测和界面代码已从本分支移除，
原项目保留在 `master`。仅保留兼容旧行情文件的精简数据适配层。

## 先运行，不需要密钥

在仓库根目录执行：

```bash
uv sync --frozen
uv run python scripts/run_scout.py --doctor
uv run python scripts/run_scout.py --demo
```

`--demo` 在临时目录创建明确标注的合成股票和行情，不写真实 canonical 数据，不联网。
它演示候选筛选、覆盖清单、报告落盘；不会伪造 GPT 分析或真实推荐。
命令输出 `report.md` 的路径；同目录还有可离线打开的只读 `report.html`。
每次运行生成独立目录，旧报告不覆盖。已有 `report.json` 可用
`scripts/render_scout_html.py` 另存为 HTML，输出路径必须尚不存在。
如公告索引的日期晚于行情日，报告顶部会提示这类线索不能倒填为行情日收盘时已知；
精确发布时间未知的公告也不能据日期证明收盘前可见。
报告汇总候选中的涨停价收盘和一价行情，但没有订单簿或次日价格，不能模拟可买入成交。

人工核查公告 PDF 正文后，可以保存独立的后置复核 JSON，再生成一个新 HTML 视图：

```bash
uv run python scripts/render_scout_html.py /absolute/path/to/report.json \
  /absolute/path/to/reviewed-view.html --review-json /absolute/path/to/review.json
```

复核文件格式如下。`run_id` 须匹配原报告，`reviewed_at` 须晚于原报告生成时间且带时区，
股票和 `source_url` 须与该报告里已保存的官方公告索引匹配。`source_sha256` 需由复核者
对原 PDF 独立计算；工具核对格式和链接归属，不会替复核者证明摘要真实或计算文件哈希。

```json
{
  "run_id": "报告中的run_id",
  "reviewed_at": "2026-09-30T04:30:00+08:00",
  "findings": [{
    "instrument_id": "000011.SZ",
    "category": "risk",
    "source_url": "https://static.cninfo.com.cn/finalpage/2026-09-30/1225588392.PDF",
    "source_sha256": "a899f04d531f92b8a5ecb26b80dd72e4945e1ac629aef475a6d3150337cdcf1b",
    "summary": "经人工核对的公告正文事实及其边界"
  }]
}
```

此叠加内容清楚标记为报告后的人工记录，不能倒填为模型输入或行情日收盘已知信息；
若该轮另有排序前机器提取的PDF正文，两者的来源时点分别展示，人工备注不改写模型输入。
原 `report.json`、`report.md` 和旧 HTML 文件保持不变。

开盘前可用 `scripts/recheck_scout_notices.py` 对已存完整报告中的最多8只最终候选重新查询巨潮公告索引，
输出到原run目录之外的新JSON文件。结果只指出新查询中哪些官方链接**不在原归档索引**；
不声称它们刚刚发布。失败、部分成功、截取和空结果均保留来源状态，不代表没有公告。
这项复查不调用AI，不读取PDF正文，不刷新行情，不改变原模型分级。
`--doctor --canonical-dir /absolute/path` 会显示预期交易日、最后具备连续 21 日
日线与复权文件的日期、落后交易日数。它只检查文件存在；真实运行还会检查内容。
如果数据落后，只能用显式 `--session` 做历史离线诊断，不应把旧候选当成今天的清单。

## 使用已有本地行情

```bash
uv run python scripts/run_scout.py --offline --session 2026-09-28
```

日期仅为用法示例，必须替换为本地完整数据的交易日。
默认读取当前工作目录下的 `data/canonical`，也可加 `--canonical-dir /absolute/path`。
读取既有 `ParquetStorage` 的证券表、SSE交易日历、连续21个交易日的日线和复权因子。
daily_basic、涨跌停价缺失时保留未知值和提示，不按固定涨幅推算涨停。
不完整历史、无效行情、停牌造成的缺行、主板以外和当前名称中的ST/退市股票被排除。
上市不足120个自然日和当日成交额低于配置阈值的股票不进入研究池。
当前证券名称筛选不等于完整的历史风险警示状态校验。

历史日期的离线结果只能看作当前数据快照下的诊断，不能当作时点正确的历史选股回测。
本工具不自动补齐或写 canonical 数据。旧数据更新入口只保留在 `master`，
需要时在另一份旧项目工作目录中运行。文件布局及单位见 [行情数据约定](market_data.md)。

## 配置联网研究

DeepSeek V4.1 Flash 用法：

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek API Key'
uv run python scripts/run_scout.py --doctor \
  --config config/scout_deepseek.example.json --canonical-dir /absolute/path/to/canonical
uv run python scripts/run_scout.py --live \
  --config config/scout_deepseek.example.json --canonical-dir /absolute/path/to/canonical
```

`deepseek-flash` 是官方 V4.1 Flash API 名称，默认思考级别为 `high`；
可用 `deepseek_reasoning_effort` 配置 `low`、`high` 或 `max`。
示例配置将 AI 深查候选限制为 8 只；20 只候选的首次真实高思考运行在调查阶段
耗尽 16,000 输出 token，整轮未完成。随后在相同 2026-09-29 行情会话、
32,768 输出 token 上限下，一次 20 候选运行完成，但最终 8 只中 7 只当日收盘
等于已知涨停价，另有 1 只被正式公告标题触发停牌核查。容量验证不是收益验证；
不要把增加候选数量当成选股效果提升。规则筛选仍可保留更多候选。
DeepSeek API 不提供本项目所需的内置网页搜索；Scout 只把已采集的 RSS、TuShare 新闻、
交易披露、评论样本和用户线索交给模型，并在覆盖表中标记网页搜索 `not_supported`。
没有来源时，模型可以分析量价，但不能凭空得出公告、业务关系或新闻事实。
可选的 `tushare_announcements: true` 会在联网运行时对最多 8 只规则候选逐股查询
[TuShare `anns_d` 公告索引](https://tushare.pro/document/2?doc_id=176)。该接口需要单独权限，
默认关闭；`--doctor` 不测试权限或发送请求。索引只给标题、公告日期和原文链接，
Scout 不读取 PDF 正文，因此将其标为未核实线索，不能据标题确认事件影响。
`rec_time` 仅在带明确时区且不晚于运行时间时作为发布时间；否则发布时间保留未知。
查询失败、空结果和每股截取上限均显示在覆盖表中，不能据此断言没有公告或已全量覆盖。
可选的 `cninfo_announcements: true` 使用巨潮官方公开公告索引，先读取证券代码映射，
再对最多8只规则候选逐股查询。每只股票最多读取30条索引；分页、请求失败、
解析拒绝及空结果都会在覆盖表提示。巨潮返回的 `announcementTime` 在已核样本中
仅映射到公告日期午夜，因此 Scout **不**把它当成精确披露时分；`published_at` 保持未知。
候选卡展示标题、公告日期和原文链接，若标题包含“停牌”会标出核查风险；
默认不读取PDF正文，不凭标题确认停牌的生效日。
此类股票在报告及JSON中保留模型研究分级，同时单列交易状态待核查标记；
只有核对公告原文和生效日期后才能判断具体交易日的可交易性。
若模型最终选出初始查询范围外的股票，Scout 会在分级完成后补查这些股票，
并在候选卡标出“模型分级后补查”；补查资料不进入已完成的模型分级。

可选的 `cninfo_pdf_bodies: true` 必须与 `cninfo_announcements: true` 一起使用。
它在模型分级前对初始定向公告索引中标题涉及停牌、业绩、财务或异常波动的资料，
每只股票最多读取一份，整轮最多8份。只接受巨潮固定HTTPS PDF路径，拒绝重定向；
每份最多2MB、12页、归档正文最多12,000字，模型输入另有字数上限。
机器提取结果保存官方链接、PDF SHA-256和读取时刻；无OCR，扫描件、加密文件、
超限文件或提取错误均显示覆盖缺口。提取文字未经过人工核实，不能用公告日期
推断精确发布时间，也不能倒填至行情日收盘。最终是否实际进入各阶段模型输入，
以归档的 `prompt_evidence_audit` 为准；后置补查的公告始终不进入既有分级。
HTML候选卡可折叠查看最多4,000字的机器提取片段，完整归档文本在 `report.json`；
页面将提取状态与原始PDF链接并列，便于回到官方原文核对。
可选的 `tushare_kpl_limit: true` 从
[TuShare 开盘啦涨停榜单](https://tushare.pro/document/2?doc_id=347)读取最多8只初始候选的
涨停题材标签和连板状态。供应商文档称前一交易日数据在次日06:00更新，因此 Scout
在该时点之前不查询；超过配置回看窗口也不采入。标签是第三方归类，不是公司公告、
股价上涨因果或可执行交易信号。接口无逐条原文URL和精确发布时间，Scout保留未知；
权限失败、空结果和拒绝记录进入覆盖清单。该接口需要相应TuShare权限，默认关闭。
为避免自由论述把席位方向、披露窗口或次日可成交性说错，报告仅展示模型分级和
程序计算的量价、披露事实；模型原文保存在原始响应文件中供审查，不直接当作结论。
参见[官方模型更新](https://api-docs.deepseek.com/updates/)与
[Responses API 工具限制](https://api-docs.deepseek.com/guides/responses_api/)。

GLM-5.3 用法：

```bash
export ZAI_API_KEY='你的 Z.ai 开放平台 API Key'
uv run python scripts/run_scout.py --doctor \
  --config config/scout_zai.example.json --canonical-dir /absolute/path/to/canonical
uv run python scripts/run_scout.py --live \
  --config config/scout_zai.example.json --canonical-dir /absolute/path/to/canonical
```

GLM-5.3 使用官方 Chat Completion API，思考级别为 `max`，输出进入本地 JSON Schema
校验；前两阶段使用搜索，最后比较阶段不搜索。搜索结果中的摘要仍是未核实线索，
网页标注日期不自动当作正式发布时间。程序不把推理过程写入报告。
这里需要 Z.ai 开放平台的 API Key；Coding Plan 订阅按其[官方使用规则](https://docs.z.ai/devpack/usage-policy)
仅供支持的编程工具使用。模型参数见[官方 GLM-5.3 文档](https://docs.z.ai/guides/llm/glm-5.3)。

原 OpenAI 用法：

```bash
cp config/scout.env.example .env.scout
# 在本地编辑 .env.scout，填写API Key、可用模型及TuShare Token
source .env.scout
uv run python scripts/run_scout.py --doctor
uv run python scripts/run_scout.py --live
```

必需环境变量：

- `OPENAI_API_KEY`：OpenAI API项目的密钥，API费用独立于ChatGPT订阅。
- `OPENAI_MODEL`：账户有权限且支持 Responses、`web_search` 与 JSON schema 的模型。
  未预设默认型号，避免假定账户权限。模型名也可写入配置的 `model`，环境变量优先。
- `TUSHARE_TOKEN`：可选，但启用新闻和实时行业分类需要；已有日线不需要重新拉取。

密钥仅由环境读取，`.env*` 已加入忽略列表，不放进代码、JSON报告或GitHub。
不要把密码/密钥发到聊天。官方说明：

- https://developers.openai.com/api/docs/quickstart
- https://developers.openai.com/api/docs/guides/tools-web-search
- https://help.openai.com/en/articles/9039756-managing-billing-for-chatgpt-and-the-api-platform

`--live` 只支持最新已结束的数据日。北京时间18:00之前保守地使用上一个已完成交易日。
交易日历必须至少覆盖运行当天；所需日线/因子分区不齐就停止。
这不是盘中打板系统。新闻信息截点是本次运行期间，行情截点是报告注明的交易日收盘。
不能把当天现查网页用于过去日期的“AI回测”。

## 多源信息如何接入

新增龙虎榜、大宗交易、评论导入与候选发现对照，详见 [信息源说明](information_sources.md)。
`--demo` 也包含这三类信息的明确合成样本，未连接真实平台。

未传 `--config` 时使用内置默认值；`config/scout.json` 是同值的可编辑模板。
复制为 `config/scout.local.json` 后使用 `--config` 指定，直接编辑模板不会自动加载。
代码已实现这些入口，而不是宣称已连接所有网站：

| 入口 | 当前实现 | 权限/覆盖说明 |
|---|---|---|
| TuShare行情 | 读取本地canonical，不重复调用provider | 需要完整本地历史 |
| TuShare新闻 | `news`，默认请求`cls`来源 | 新闻可能需另开权限；失败显示failed |
| TuShare行业 | `stock_basic`的industry | 当前行业快照，不是假定完整的题材关系网 |
| RSS/Atom | 用户配置的公开HTTPS feed | 默认空；需选择有权使用的真实地址，无凭证/内网/跳转 |
| 手工线索 | `--clues`读取JSON | 可导入社交帖子内容；保留未核实身份 |
| OpenAI联网检索 | 全市场线索发现＋候选专项调查 | 可检索公开公告、产业、政策与讨论，非全量订阅 |
| 社交/付费公告流 | 未直接连接 | 明确显示not_connected，不声称全量覆盖 |

RSS配置格式（必须替换为实际允许读取的feed，不要照抄示例域名运行）：

```json
{"rss": [{"name": "自行选择的信息源", "url": "https://example.org/feed.xml"}]}
```

来源内容视为不可信数据，不能作为系统指令。时间必须包含时区；缺失发布时间明确未知，
不会填成今天或倒填成过去。已知发布时间超出默认72小时的内容不作为新事件输入。
重复标题和正文合并；最多80条已收集的本地信息进入发现提示词，完整入档内容仍保留。
TuShare每个新闻来源最多使用100条，报告注明截断；不能声称已经覆盖全部新闻。

手工线索可以复制 `config/scout_clues.example.json`，填写真实内容后：

```bash
uv run python scripts/run_scout.py --live --clues /path/to/clues.json
```

`instrument_ids` 可填已核实的完整股票代码，但这只是候选提示，不是公司关系证据。
没有URL的用户线索不伪造引用。抓取社交平台、登录会员和购买数据授权尚未实现。

可用 `--sectors /path/to/sectors.json` 提供行业映射：

```json
{
  "observed_at": "2026-09-29T08:00:00+08:00",
  "memberships": {"600000.SH": "银行", "601398.SH": "银行", "601939.SH": "银行"}
}
```

示例日期必须替换为实际观察时间。在线模式拒绝超过7天的映射。行业组至少3只合格股票，
组内上涨比例不少于60%、平均日涨幅超过1%时，从中取规则分数前2名。
该行业统计只覆盖经过筛选的股票，并非供应商完整行业指数。

## 候选和AI流程

1. 本地量价：连续21日复权价格，1/5/20日收益、5日成交额比、20日突破和收盘位置。
   基线分数为1日涨幅、5日涨幅、5日成交额比、20日突破幅度的截面百分位均值。
   这是一套未经验证的透明启发式，不是涨停概率。
2. 两类量价入口：日涨幅>2%且成交额比>1.2；或突破前20日高点且5日收益>0。
3. 行业入口和信息入口拥有保留名额，避免全部被涨幅排序挤掉。
4. 第一次模型调用：搜索近期多主题线索；DeepSeek 模式只处理已给来源。
5. 第二次模型调用：对合并候选调查业务关系和反证；DeepSeek 模式仍只处理已给来源。
6. 第三次模型调用：在证据包内选最多3只focus和5只watch，允许为空。
7. 代码验证股票属于候选池、引用已进入最终证据包且绑定该股票，并要求每只引用自己的行情快照。

直接/产业链/题材/名称情绪关系分开记录。网页来源存在只证明检索返回过该来源，
不代表模型对来源的理解正确，也不保证其首次发布时间。未知、传闻和反证仍需人工核对。
只靠未核实线索进入的股票不应作为重点候选。无事件但量价明显的股票可以继续调查。
历史名次、当前强势和下一日买入价值不等同。

## 输出与失败行为

`data/scout/runs/<时间-唯一ID>/` 下包含：

- `report.md`：中文候选、指标、失效观察点、覆盖清单、可点击来源及限制。
- `report.json`：基线名单、完整候选与合格股票指标快照、行业映射、结构化依据、数据时间、模型用量。
- `ai_responses.json`：本次调用响应，仅本地存储；GLM 与 DeepSeek 的私有推理内容不入档。
- `manifest.json`：内容哈希，后续观察前核对原报告是否被更改。

`offline_diagnostic`：未调用AI，只有规则候选。
`demo`：合成数据演示，禁止作为真实研究表现。
`live_research_unvalidated`：AI流程完成，但选股效果未经验证。
`incomplete`：调用、结构或引用校验失败，不输出AI推荐，进程退出码2。

新闻和RSS的单个来源失败会显示失败，不伪造内容；AI任一步失败会使最终选择整体作废。
调查期间新发现的网页会有真实获取时间，不强行伪装为最初运行时已知的信息。
配置缺失或行情不完整会在调用付费模型前停止。

每次最多3次模型调用、每次输出token上限默认6000；DeepSeek 示例设为24000，
用于容纳思考与最终 JSON，上限32768。此前16000的真实运行曾在第二阶段截断，
见[官方模型输出上限](https://api-docs.deepseek.com/api/list-models/)与
[思考模式说明](https://api-docs.deepseek.com/guides/thinking_mode/)。OpenAI 搜索调用上限默认5；
GLM 搜索每阶段最多返回5条结果，最多两个含搜索的阶段。
限制是请求数量/token上限，不是美元硬封顶；搜索结果token和输入仍计费。
没有自动重试，避免失败时重复消耗。先从少量运行测量实际用量，再决定是否定时。

## 推荐后观察

```bash
uv run python scripts/run_scout.py \
  --track-run data/scout/runs/实际运行目录 \
  --output-dir data/scout/observations
```

从报告发布之后第一个收盘价开始，观察再往后1/3/5个交易日的复权收盘价变化。
缺失或尚未发生的结果为null，不填0。基线与AI名单均记录，不覆盖原报告。
每行分别记录首个观察日和目标日的价格状态：交易日未到18:00为 `not_yet_due`，
到时后若缺少行情或复权因子则为 `price_or_factor_missing`，日历不足另列
`calendar_unavailable`；缺数原因不自动判为停牌。只有两端价格都可用时才计算收益。
交易日跨度按本地交易日历确定，长假不会按自然日填造价格。
观察文件保留模型原始 `ai_focus` / `ai_watch` 分组，并单列 `official_notice_hold`；
`ai_focus_without_notice_hold` / `ai_watch_without_notice_hold` 排除公告标题触发暂停核查的股票。
未触发暂停核查不代表可交易，不能把这些分组当作可成交组合。
这不是次日开盘买入收益，也没有假设涨停可以买到、跌停可以卖出或忽略费用。
它只能帮助初步检验排序，不能替代可执行策略评估。现有数据可能修订，因此重新观察值
可能不同，每次观察单独保存。

## 开发验证

```bash
uv run pytest tests/scout -q
uv run pytest
uv run ruff check .
git diff --check
```

当前交付应区分离线/模拟API验证与真正线上验证。没有真实密钥、接口权限和本地真实行情，
就不能声称已生成当天实际推荐。下一步应先在用户本地更新行情、配置密钥，运行一次live，
检查来源覆盖和失败记录，再积累固定版本的前瞻观察。
