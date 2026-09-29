# 多源短线研究助手 Scout

Scout 是 QuantLab 内的独立只读研究工具。它合并量价异动、板块领先、信息关联三路候选，
使用 OpenAI Responses 的联网搜索调查，再生成最多 3 只重点观察和 5 只普通观察候选。
它不接券商、不下单、不改变原有策略/回测/风控语义，不宣称已找到有效 alpha。

## 先运行，不需要密钥

在仓库根目录执行：

```bash
uv sync --frozen
uv run python scripts/run_scout.py --doctor
uv run python scripts/run_scout.py --demo
```

`--demo` 在临时目录创建明确标注的合成股票和行情，不写真实 canonical 数据，不联网。
它演示候选筛选、覆盖清单、报告落盘；不会伪造 GPT 分析或真实推荐。
命令输出 `report.md` 的路径。每次运行生成独立目录，旧报告不覆盖。

## 使用已有本地行情

```bash
uv run python scripts/run_scout.py --offline --session 2026-09-28
```

日期仅为用法示例，必须替换为本地完整数据的交易日。
默认读取仓库内 `data/canonical`，也可加 `--canonical-dir /absolute/path`。
读取既有 `ParquetStorage` 的证券表、SSE交易日历、连续21个交易日的日线和复权因子。
daily_basic、涨跌停价缺失时保留未知值和提示，不按固定涨幅推算涨停。
不完整历史、无效行情、停牌造成的缺行、主板以外和当前名称中的ST/退市股票被排除。
上市不足120个自然日和当日成交额低于配置阈值的股票不进入研究池。
当前证券名称筛选不等于完整的历史风险警示状态校验。

历史日期的离线结果只能看作当前数据快照下的诊断，不能当作时点正确的历史选股回测。
本工具不自动补齐或写 canonical 数据；需要时使用原仓库已有的数据更新入口。

## 配置联网研究

```bash
cp config/scout.env.example .env.scout
# 在本地编辑 .env.scout，填写API Key、可用模型及TuShare Token
source .env.scout
uv run python scripts/run_scout.py --doctor
uv run python scripts/run_scout.py --live
```

必需环境变量：

- `OPENAI_API_KEY`：API项目的密钥，API费用独立于ChatGPT订阅。
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

复制 `config/scout.json` 为 `config/scout.local.json` 后使用 `--config` 指定。
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
4. 第一次GPT调用：联网发现近期多主题线索，提出有引用的股票关联。
5. 第二次GPT调用：对合并候选调查公告、产业、业务关系和反证。
6. 第三次GPT调用：在证据包内选最多3只focus和5只watch，允许为空。
7. 代码验证股票属于候选池、引用ID存在，并要求每只引用自己的行情快照。

直接/产业链/题材/名称情绪关系分开记录。网页来源存在只证明检索返回过该来源，
不代表模型对来源的理解正确，也不保证其首次发布时间。未知、传闻和反证仍需人工核对。
只靠未核实线索进入的股票不应作为重点候选。无事件但量价明显的股票可以继续调查。
历史名次、当前强势和下一日买入价值不等同。

## 输出与失败行为

`data/scout/runs/<时间-唯一ID>/` 下包含：

- `report.md`：中文候选、指标、失效观察点、覆盖清单、可点击来源及限制。
- `report.json`：基线名单、完整候选与合格股票指标快照、行业映射、结构化依据、数据时间、模型用量。
- `ai_responses.json`：本次调用得到的原始响应，仅本地存储。
- `manifest.json`：内容哈希，后续观察前核对原报告是否被更改。

`offline_diagnostic`：未调用AI，只有规则候选。
`demo`：合成数据演示，禁止作为真实研究表现。
`live_research_unvalidated`：AI流程完成，但选股效果未经验证。
`incomplete`：调用、结构或引用校验失败，不输出AI推荐，进程退出码2。

新闻和RSS的单个来源失败会显示失败，不伪造内容；AI任一步失败会使最终选择整体作废。
调查期间新发现的网页会有真实获取时间，不强行伪装为最初运行时已知的信息。
配置缺失或行情不完整会在调用付费模型前停止。

每次最多3次模型调用、每次搜索调用上限默认5、每次输出token上限默认6000。
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
