# Scout · AI 选股研究助手

面向 A 股短线研究：从**量价异动、板块领先、信息关联**三条路径发现候选，
用 AI 联网调查业务关系和反证，输出有来源、可复查的中文观察报告。

本分支 `feat/shortline-research-assistant` 独立演进。原 QuantLab 量化项目保留在
[`master`](https://github.com/yuxiangppz-dotcom/quantlab/tree/master)。
本分支已移除因子训练、历史回测、组合管理、交易执行、旧界面与 Agent 调度系统。
保留的 `quantlab.data` 只是兼容旧 Parquet 数据的精简适配层，不需要安装或启动旧项目。

**当前阶段：盘后研究原型。** 离线演示和模拟 API 流程已经验证，真实 API 与当天行情仍需
本地配置后验证。筛选规则未经选股效果验证；报告不表示涨停概率或可成交收益。

## 三分钟运行

需要 Python 3.12+ 和 uv。在当前分支的仓库根目录执行：

```bash
uv sync --frozen
uv run scout --doctor
uv run scout --demo
```

`--demo` 无需密钥、不联网，使用临时合成行情演示候选筛选和报告。
命令会打印 `report.md` 的绝对路径。演示不会伪造 AI 分析或真实推荐。
`--doctor` 只读检查本地证券表、日历和日线／复权分区，显示预期交易日、
最近连续 21 个交易日的完整分区日期及落后交易日数。它只核对文件存在；
正式研究运行仍会校验文件内容。行情落后时，使用显式 `--session` 可做历史离线诊断，
不能将其称作当日结果。

也可以使用 `uv run python -m quantlab.scout`，或原来的
`uv run python scripts/run_scout.py`，参数相同。

## 它如何选出候选

| 步骤 | 输入与处理 | 输出 |
|---|---|---|
| 规则扫描 | 连续 21 个交易日行情；价格强度、成交额变化、突破 | 量价候选与透明排序基线 |
| 板块发现 | 有获取时间的行业映射、组内涨幅与上涨比例 | 板块领先候选 |
| 信息发现 | 新闻、公开网页、RSS、手工线索；AI 检索关联股票 | 有来源的事件、产业链、题材或情绪假设 |
| 专项调查 | 合并候选，查询公告、合作关系、澄清和负面信息 | 支持证据、反证及缺失项 |
| 比较输出 | 行情快照与证据包 | 最多 3 只重点观察、5 只普通观察，可为空 |
| 后续观察 | 报告发布后的收盘数据 | 1／3／5 个交易日价格变化，与规则基线对照 |

无重大事件但量价明显的股票也能进入候选。公司名称相似只算情绪联想，不能冒充股权或业务关系。
模型输出必须引用候选自身的行情和实际提供给最终比较、且已绑定到该股票的来源；
引用存在不等于内容已经核实。
报告中的 AI 仅给出候选研究优先级；展示的量价和披露说明由程序从输入生成。
模型自由论述封存在原始响应中，未经事实审查不作为报告依据。

## 接入真实数据与 AI

**1. 准备本地行情。** Scout 只读取行情，不自动下载或更新 canonical 数据。
可以直接指向另一份 QuantLab 工作目录已有的数据，无需复制整个旧项目：

```bash
uv run scout --offline --canonical-dir /absolute/path/to/quantlab/data/canonical
```

默认使用当前目录的 `data/canonical`。需要股票基础信息、覆盖当前日期的交易日历、
连续 21 个交易日的日线和复权因子；换手率、涨跌停价可选。具体格式见
[行情数据约定](docs/market_data.md)。缺失必需数据时停止，不会偷偷联网补齐。

**2. 在本地配置 AI。** 默认仍为 OpenAI Responses。使用 DeepSeek V4.1 Flash 时：

```bash
export DEEPSEEK_API_KEY='你的 DeepSeek API Key'
uv run scout --doctor --config config/scout_deepseek.example.json \
  --canonical-dir /absolute/path/to/quantlab/data/canonical
uv run scout --live --config config/scout_deepseek.example.json \
  --canonical-dir /absolute/path/to/quantlab/data/canonical
```

官方 API 名称是 `deepseek-flash`。Scout 默认使用 `reasoning_effort=high` 和本地结构校验；
可在配置中改为 `max`，但复杂多股调查可能耗尽输出 token 并使整轮标记为未完成。
DeepSeek 示例将深查候选限制为 8 只；20 只候选的真实高思考运行在调查阶段耗尽
16,000 输出 token，结果标记为未完成。规则候选池与 AI 深查容量是两种口径。
DeepSeek API 在此模式没有内置网页搜索，模型只调查 Scout 已采集的行情、新闻、
披露及导入线索。报告会标明网页搜索未覆盖，不允许模型编造新 URL。
需要配置真实信息源并检查覆盖状态，才能评估调查内容。
[模型名称](https://api-docs.deepseek.com/updates/)、
[Responses 工具限制](https://api-docs.deepseek.com/guides/responses_api/)。

若使用 GLM-5.3，复制
[`config/scout_zai.example.json`](config/scout_zai.example.json) 并在运行时传入：

```bash
export ZAI_API_KEY='你的 Z.ai 开放平台 API Key'
uv run scout --doctor --config config/scout_zai.example.json \
  --canonical-dir /absolute/path/to/quantlab/data/canonical
uv run scout --live --config config/scout_zai.example.json \
  --canonical-dir /absolute/path/to/quantlab/data/canonical
```

GLM-5.3 接口使用 `reasoning_effort=max`、JSON 模式和前两阶段联网搜索；
程序仍会在本地校验 JSON 结构、来源归属和完整结束状态。每轮最多 3 次模型请求，
GLM 每次搜索最多返回配置的 `max_tool_calls` 条（上限 5），并非美元费用上限。
请使用 Z.ai **开放平台 API Key**；Coding Plan 的订阅凭据有独立使用范围。
联网请求和真实账户权限尚需本地联调。[GLM-5.3 接口](https://docs.z.ai/guides/llm/glm-5.3)、
[Coding Plan 使用规则](https://docs.z.ai/devpack/usage-policy)。

继续使用 OpenAI 时：

```bash
cp config/scout.env.example .env.scout
# 编辑 .env.scout，填写 OPENAI_API_KEY 和 OPENAI_MODEL
source .env.scout
uv run scout --doctor
uv run scout --live --canonical-dir /absolute/path/to/quantlab/data/canonical
```

上述加载环境变量的例子用于 Bash／zsh。Windows PowerShell 可设置
`$env:OPENAI_API_KEY`、`$env:OPENAI_MODEL`，不使用 `source`。

`OPENAI_MODEL` 必须是账户可用、支持 Responses、联网搜索和结构化输出的模型。
`TUSHARE_TOKEN` 可选，用于新闻、当前行业分类与交易披露；相关权限需自行确认。
密钥只放本地环境变量，不写进配置 JSON，也不要发到聊天或提交 Git。

**3. 按需增加信息源。**

| 信息源 | 接入方式 | 当前边界 |
|---|---|---|
| TuShare 新闻、行业分类 | 本地 token 与配置 | 接口权限不足会在报告中显示失败 |
| 龙虎榜、席位明细、大宗交易 | TuShare 接口或 `--disclosures` 文件 | 默认近 3 个交易日；保留披露窗口和获取时间 |
| 同花顺等平台评论样本 | `--comments` 文件 | 样本去重与线索调查；不代表平台全部评论或真实持仓 |
| 公开网页与公告 | AI 联网发现和专项检索 | 不是全量公告订阅；网页时间可能未知 |
| RSS／Atom | `config/scout.local.json` 的 `rss` | 默认未配置，使用真实可访问的公开 HTTPS feed |
| 自己看到的帖子或新闻 | `--clues /path/to/clues.json` | 作为未核实线索；参考 `config/scout_clues.example.json` |
| 微博、小红书等完整信息流 | 尚未直接连接 | 不假定模型能读取登录内容或全部帖子 |

自定义参数时复制 `config/scout.json` 到 `config/scout.local.json`，运行时加
`--config config/scout.local.json`。默认每轮最多 3 次模型请求，每次搜索最多 5 次工具调用；
实际费用取决于模型、输入输出及搜索用量，不是固定金额封顶。

交易披露默认每轮最多 9 次接口请求；评论目前支持导入，不自动抓取平台。
数据口径、文件模板和使用方法见 [交易披露与讨论样本](docs/information_sources.md)。

## 运行结果

每轮写入新的 `data/scout/runs/<时间-唯一ID>/`，不会覆盖历史报告。
路径默认相对当前工作目录，也可用 `--output-dir` 指定。

| 文件 | 内容 |
|---|---|
| `report.md` | 中文候选、依据、风险、失效观察点、来源与覆盖状态 |
| `report.html` | 可离线打开的只读候选卡、公告风险、来源索引和覆盖表；不加载脚本或外部资源 |
| `report.json` | 行情指标快照、规则基线、行业映射、证据、配置和模型用量 |
| `ai_responses.json` | 本地保存的 AI 响应；GLM 与 DeepSeek 的私有推理内容不入档 |
| `manifest.json` | 内容哈希，用于后续核对原报告 |

要给已有报告生成单独的 HTML 视图，指定一个尚不存在的输出文件：

```bash
uv run python scripts/render_scout_html.py data/scout/runs/实际运行目录/report.json /absolute/path/to/scout-view.html
```

该命令只读取归档 JSON；输出文件已存在时会报错，避免覆盖旧视图或实验产物。

观察报告之后的价格变化：

```bash
uv run scout --track-run data/scout/runs/实际运行目录 \
  --canonical-dir /absolute/path/to/quantlab/data/canonical \
  --output-dir data/scout/observations
```

结果从报告发布后第一个收盘价开始计算，缺失值保留为空。
它不是实际买卖收益，不假设涨停能买到或跌停能卖出。

## 代码结构与开发

| 路径 | 职责 |
|---|---|
| `src/quantlab/scout/` | 命令入口、候选发现、来源采集、AI 调查、报告与后续观察 |
| `src/quantlab/data/` | 六类本地行情记录与兼容 Parquet 适配层 |
| `config/scout*` | 默认配置、环境变量模板、手工线索模板 |
| `tests/scout/`、`tests/data/` | 研究流程与数据兼容性测试 |
| `docs/` | 使用说明、数据格式和本次精简记录 |

```bash
uv run pytest
uv run ruff check .
git diff --check
```

CI 只验证 Scout 与必要的数据适配层，不再安装 Qlib、LightGBM、Streamlit 或运行旧研究任务。
Python 包路径暂保留 `quantlab`，项目安装名为 `quantlab-scout`，命令为 `scout`。

详细说明见 [Scout 使用说明](docs/scout_zh.md)。下一步先验证一次真实研究流程和信息覆盖，
再积累前瞻观察；盘中盯盘、交易执行、持仓管理和自动调度尚未实现。
