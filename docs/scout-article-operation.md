# Scout 八步短线研究：运行与操作

当前研究模式：`codex_browser_daily_v1`。按用户最后指令，第7步由本线程主 agent 在雪球浏览日K；不调用 DeepSeek，不保存或上传新截图。其余八步顺序、沪深主板范围、两种形态与参与上限保留。

## 日常流程

1. 程序在独立 `scout_article_*` 数据卷检查最新完整行情、交易日历和真实交易状态。6000接口用于方向与候选证据；单接口权限不等于全部覆盖。
2. 计算市场→行业/题材→两种形态→资金/榜单/风险/价位。按方向与路线分配最多24只深查；不靠涨幅四项均值排序凑名单。
3. 保存固定 `config.json`、完整 `sources.json`、事实包 `research.json` 与内部 `diagnostics.json`。状态是 `awaiting_codex_browser_daily_review`，不能把准备包当成完成推荐。
4. Codex 读取 skill 和事实包，在雪球实际浏览，填写本次 `research_hash` 绑定的 review。读不到图线或日期就记录 partial/unknown；行情数字与日线计算不能冒充已看过图线，也不称分时承接。
5. 程序复核主体、事实引用、程序上限和市场名额，冻结HTML/Markdown/JSON；默认0–5只，未知环境最多3只观察、没有优先。新观察记录独立追加，不修改原名单。

该模式需要 Codex 在线完成第4步。当前不能宣传为电脑关闭后云端每日自动完成 Codex 研究；旧 DeepSeek 云端预测已按用户新指令暂停。历史报告与数据保留。

## 命令

在项目目录使用 Python 3.12+ 与 `uv`。`--live` 配合 `--article-strategy` 只授权隔离数据/公告取数，不调用外部模型。

```bash
uv run scout --article-strategy --doctor

# 真实取数，准备研究包；TUSHARE_TOKEN由受限环境文件提供
uv run scout --article-strategy --live --article-root /path/to/scout_article_YYYYMMDD

# 回放已保存的真实数据和源包，不联网
uv run scout --article-strategy --offline --article-root /path/to/scout_article_YYYYMMDD \
  --article-source-pack /path/to/sources.json

# Codex完成本次浏览与研究后冻结；不能复用另一个包的review
uv run scout --article-strategy --article-root /path/to/scout_article_YYYYMMDD \
  --article-finalize /path/to/scout_article_YYYYMMDD/article_runs/RUN_ID \
  --article-review-file /path/to/codex-review.json

# 数据更新后持续跟踪；该命令本身只读本地已保存的真实行情与资格快照
uv run scout --article-strategy --article-root /path/to/scout_article_YYYYMMDD \
  --track-run /path/to/scout_article_YYYYMMDD/article_runs/RUN_ID
```

实际股本、解禁、历史成员、单位与统计范围不明时保持未知。历史成员与交易资格仅使用当日真实已冻结的证据，不把今天下载的旧数据回填成当时已知。上市龙虎榜的单位与交易所原文联合核查未完成之前，不启用该事件的绝对卖出阈值。

## 研究与展示

每股显示研究理由、最强反证、下一步具体观察、程序价格参考及风险。报告隐藏全市场排除清单与研究漏斗；完整审计仍保存在JSON。价格参考不是订单或成交，结构失效位不保证成交。

参数目前是任务书的初始研究参数，未经收益验证。D1/D3/D5尚未到期显示待观察；主观察为D1开盘至D3收盘，分别记录全部冻结名单与开盘条件通过子集，保留可参与性未知。至少20个独立交易日后再汇总覆盖、形态、资金冲突与表现，不能凭本次名单或一个涨停声称赚钱能力达标。

## 中断恢复

先读 `SCOUT_TASK_STATE.md` 并检查活跃写入者。研究包、review、报告与观察均独立保存；重复冻结已完成运行返回原目录，不启动任何模型任务。中断后只补同一冻结报告的展示/观察，冲突文件拒绝覆盖。代码或配置改变后应重新准备研究包，不能把旧 review 冒充新版研究。
