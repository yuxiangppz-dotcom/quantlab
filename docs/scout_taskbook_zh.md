# Scout 当前版操作说明

TuShare 升级接口、真实运行和证据边界见 [升级说明](scout_tushare_upgrade_zh.md)。

Scout 是按需运行的盘前研究助手，不下单，也不将价格观察称为成交或净收益。旧 QuantLab
量化研究和旧 Scout 报告独立保留。此版不要求历史回测、交易账本、中证 800 范围或
AI 与简单规则的收益对照。

## 准备

在本仓库运行 `uv sync --frozen`。实时研究需要完整的本地日线、复权因子和交易日历，
以及已获授权的模型密钥。密钥只放在本机环境变量，不放在配置文件或报告中。
示例配置 [`config/scout_taskbook.example.json`](../config/scout_taskbook.example.json)
使用 DeepSeek、最多 160 只廉价召回和 24 只 AI 深查；这些数量是工程初值，不是拟合出的最优值。
将以下路径换成自己的绝对路径：

```bash
CANONICAL=/absolute/path/to/isolated/canonical
RUNS=/absolute/path/to/scout/runs
uv run scout --doctor --config config/scout_taskbook.example.json \
  --canonical-dir "$CANONICAL" --output-dir "$RUNS"
```

`--doctor` 只看本地文件与环境变量是否存在，不联网，也不证明接口权限。缺少最新完整
交易日数据时，先由已有数据维护流程更新**隔离副本**；Scout 不写原 canonical。

## 四项操作

1. 保存热榜快照（免费公开 AKShare 接口，可选单独调用）：

   ```bash
   uv run scout --hot-snapshot --output-dir "$RUNS"
   ```

   成功文件位于 `$RUNS/../hot_snapshots/`。原始接口行和规范化 Top100 一起保存。
   接口未给精确榜单发布时间；首次抓取或股票不在上次 Top100 时，排名变化为未知。
   接口暂时失败时，可用 `--hot-file /absolute/path/to/recent-snapshot.json` 传入
   24 小时内已保存的真实快照；报告会标明缓存抓取时间。

2. 生成并冻结一份报告：

   ```bash
   uv run scout --live --config config/scout_taskbook.example.json \
     --canonical-dir "$CANONICAL" --output-dir "$RUNS"
   ```

   有缓存快照时，在命令末尾加 `--hot-file`。每轮独立目录包含 `report.json`、
   `report.md`、`report.html`、`ai_responses.json` 和 `manifest.json`。若模型失败，
   仍保存 `incomplete` 记录，但不列为有效盘前版本。完全相同的输入指纹会复用已成功
   报告，避免重复付费。可同日重新运行以纳入新证据；各版本均保留。
   如需单列持仓与自选，可附加 `--portfolio-file /absolute/path/to/portfolio.json`。
   文件格式如 `{"observed_at":"2026-09-30T20:00:00+08:00", "holdings":["600418.SH"],
   "watchlist":["000002.SZ"]}`。清单不占新候选名额；即使股票不符合新候选条件，
   仍显示已知身份、行情和风险缺口。此入口目前是程序事实观察，未另行调用模型深查。

3. 补齐某份真实冻结报告的走势：

   ```bash
   uv run scout --track-run /absolute/path/to/run-directory \
     --canonical-dir "$CANONICAL" --output-dir "$RUNS"
   ```

   补齐结果放在 `$RUNS/../tracking/`，原始报告不会改变。数据尚未成熟或个股停牌、
   缺开盘/收盘价时，显示相应状态。日线数据准备好后可重复执行，内容未变化则复用。

4. 查看累计观察：

   ```bash
   uv run scout --tracking-summary --output-dir "$RUNS"
   ```

   输出 `$RUNS/../tracking-summary.json` 和可读的 `tracking-summary.md`。
   每个目标交易日只采用开盘前最后一份有效报告，重点与一般观察分开；无重点日也计入。

## 候选与证据

默认研究沪深主板、非当前 ST/退市标记、上市满 120 天且当日成交额至少 1 亿元的
普通股票；缺少完整 21 个交易日行情或复权因子的股票不当作正常候选。候选由
巨潮市场公告索引、行业领先、回撤/温和放量、趋势/量价和热榜独立召回。
巨潮市场索引每个交易所最多读取两页，明确是抽样；发布时间未知的公告不能倒填成
前日收盘已知。热榜和评论只提供关注线索，不证明公司业务或资金方向。
`report.json` 的 `candidate_stages` 记录证券是否进入廉价召回和深查、以及预算外原因；
行情准入失败的聚合原因在 `market.rejected`。最终最多 3 只重点和 5 只观察，均可为空。

模型作结构化筛选并须引用实际输入证据；程序展示可计算事实和未经校准的研究优先级，
不会将模型自由文本当作已证实事实。报告明确行情截至日、信息截点、真实生成时间与
目标交易日。夜间生成标为下一交易日准备；开盘后完成的报告不能算该日盘前版本。

## 前瞻观察口径

目标日 D 算第 1 个交易日，H=1/3/5/10 的终点固定为交易所日历中的第 H 个交易日。
观察 D 日开盘跳空以及 D 日开盘到终点收盘的复权价格变化；主期限预设为 5 个交易日。
停牌或缺 D 日开盘时不顺延到复牌日；缺失、未成熟和公司行动口径问题不填零。
这是相关的价格观察样本，不是实际买卖、扣费结果或稳定胜率证明。旧报告采用原有
收盘到收盘口径，继续保留，但不混入新累计结果。尚无成熟终点时显示“待观察”。

可选的历史命令 `--offline`、`--demo` 只用于诊断或合成演示；旧公告人工复核工具
仍可独立使用。此前的规则基线对照和旧式历史跟踪不再作为当前交付入口。
