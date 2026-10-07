# Scout TuShare 升级：运行与证据边界

本页记录 2026-10-01 的工程升级及实际运行。Scout 给出研究优先级，不下单；报告中的“重点”不是可成交或已验证有收益的股票。

## 本轮改变

- 模型原始选择先做逐只证据和论述校验，再展示；无依据事实和数值会移除该选择并记录原因。报告保留 `selection_raw`、`selection_validation` 和最终 `selection`。
- 事件、申万行业扩散、回撤、热榜关注、趋势五条入口按新增唯一股票分配深查名额；每只候选的首次分配入口、路径排名、重叠路径及阶段原因写在 `candidate_stages`。
- 深查剩余股票的巨潮公告索引和有界 PDF 正文在最终模型输入前补取。最终提示词证据 ID 在 `prompt_evidence_audit`；没有进入该输入的材料不能支撑原分级。
- 可启用 TuShare 升级包。历史涨跌停结构、KPL 题材成员、公司披露事件、财务与业务背景、解禁/停牌/披露日历、普通 `moneyflow`、申万一级行业分别用于召回、分析或风险展示。原始接口响应单独保存为不可覆盖快照；每次调用的状态、条数、耗时、缓存命中写在 `tushare_source_matrix`。新产生的 TuShare 证据通过 `snapshot_refs` 关联原始快照；模型输入不携带本机路径。
- 报告增加各阶段涨停集中度、近期涨幅和公司事实覆盖。数值和证据缺失保持未知；行业与关注度不等于上涨概率。

接口字段及权限依据 [TuShare 官方文档](https://tushare.pro/document/2?doc_id=298)，实际以本机探测矩阵和每轮 `tushare_source_matrix` 为准。`share_float` 的解禁比例是占总股本比例，窗口达到 6000 行上限时继续按日期拆分；仍达到单日上限就保留 `possibly_truncated` 状态，不宣称完整。资金净额采用同一普通 `moneyflow` 口径，1/3/5 日缺日显示 `null`，不填零或混合其他资金流接口。

## 本机实际结果

- 2026-10-01 对 15 个接口做了脱敏小样本探测：均返回数据。原始矩阵在隔离数据目录的 `source_checks/tushare_matrix_20261001_upgrade.json`；其中 `kpl_concept_cons` 和 `share_float` 命中接口行数上限，说明需要按题材/日期拆分。
- 第一轮升级运行 `upgrade_runs/20261001T120233-6dfeeee0` 保存为 `incomplete`。旧数值校验误拒模型市场概述；该失败记录保留。
- 首份模型输出曾派生为 `upgrade_runs/20261001T120934-2f471585`，但后续审查发现当时只归档了最终证据 ID，没有归档提示词中的实际截短正文；这份旧派生版保留作开发历史，**不作为本轮最终名单**。`20261001T122501-74f9555b` 暴露行业字段串股；`20261001T123056-9363ba8c` 暴露数值和股票代码校验边界；`20261001T123632-39d72209` 是保存了精确提示词证据、但旧校验过严的空名单版本。各失败和中间版本均保留，不覆盖。
- 最新真实输入及模型输出为 `upgrade_runs/20261001T124209-5f31dbc0`，后用同轮**已归档的精确提示词节选**做确定性重校验，得到 `upgrade_runs/20261001T124559-dae19e79`。没有再次调用模型或供应商。行情截至 2026-09-30，原输入截点 2026-10-01 12:42，目标交易日 2026-10-08；160 只初筛、24 只深查，五条路径在深查池分别分配 5/5/5/5/4 只。模型原始输出 3 重点/5 观察，经逐只核验剔除无依据数值、未引用的其他公司财务内容及“唯一正式披露”等超出公告抽样覆盖的表述后，**0 重点、2 观察**（`000678.SZ`、`603200.SH`）。两只观察股此前收盘都在已知涨停价，追高集中风险仍明显；这不是准确率改善的证据。
- 本轮 TuShare 源矩阵包含 24 次深查股票逐只解禁核对，其中 1 次有数据、23 次空响应待确认。市场层 `share_float` 多个单日达到 6000 行上限，完整性不足已记录；逐只调用用于减小深查股票的漏报风险。最终提示词证据 39 条，包含公司事件、涨停结构、普通资金流、官方 PDF 机器节选与一条已知未来解禁；来源仍可能缺失或滞后。
- 最新版本的前瞻跟踪文件有 8 条 H1/H3/H5/H10 记录，全部为 `d_open_not_yet_due`。累计摘要按目标交易日只采纳最新有效版本，当前 1 个目标日、0 重点日、可计算收益 0。没有成熟的胜率或净收益结果，不能判定选股效果达标。

## 操作

在 Scout 工作树中，先准备隔离的 canonical 副本、已有的模型和 TuShare 环境变量。不要把密钥放进参数、报告或 Git。

```bash
uv run scout --doctor --config config/scout_taskbook.example.json \
  --canonical-dir /absolute/isolated/canonical \
  --output-dir /absolute/isolated/upgrade_runs

uv run scout --live --config config/scout_taskbook.example.json \
  --canonical-dir /absolute/isolated/canonical \
  --output-dir /absolute/isolated/upgrade_runs \
  --hot-file /absolute/recent/akshare_snapshot.json

uv run scout --track-run /absolute/isolated/upgrade_runs/RUN_ID \
  --canonical-dir /absolute/isolated/canonical \
  --output-dir /absolute/isolated/upgrade_runs
```

示例配置开启 `tushare_upgrade`；默认程序配置保持关闭。热榜快照须满足原有 24 小时有效性规则。相同交易日重复付费运行只会产生新版本，不能据此证明准确率提升。后续目标日行情成熟后再运行前瞻跟踪；旧报告、失败记录、原 canonical 和原始快照不覆盖。

只有在保存了完整原始 AI 输出和**精确最终提示词证据节选**、而校验程序误判的 `incomplete` 或空名单版本上，才可使用 `scripts/scout_revalidate.py`。它校验旧报告哈希、最终输入内容/证据 ID 和信息截点，只重新运行确定性校验，不补入新事实，也不调用数据商或模型；产物另建目录并标注来源。缺少精确节选的旧报告不能按新规则重校验。正常使用应生成新报告。
