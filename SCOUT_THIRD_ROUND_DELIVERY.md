# Scout 第三轮交付与 Astra 复核入口

日期：2026-10-01（北京时间）。任务书：`E:/浏览器下载/Scout_Codex_Third_Round_Taskbook.md`。起始 HEAD：`cb4c7e38a125c400b740a9a14db9b45e0b82e976`。代码工作树为 `C:/Users/Administrator/.codex/worktrees/scout-ai-assistant/quantlab-review-pr189`，草稿 PR #192。本文记录本轮实际完成和仍未知的部分，不把工程校验当作投资收益证明。

## 工程结果

| 工作包 | 已实现与核查 | 剩余边界 |
| --- | --- | --- |
| A 事实校验 | 新选择 schema 增加 `fact_ids`；候选输入列出程序事实 ID，跨股事实须绑定被点名对象；校验昨日涨跌方向、成交额等价单位/错误单位、无证据订单、未知封单金额单位、复合否定及抽样公告的“唯一”断言。逐股拒绝保存字段、片段、错误码、支持值或缺口和处理。 | 自然语言语义只能覆盖明确实现的高风险模式；事实 ID 与来源 ID 不能证明所有推断。被剔除等级不静默恢复。 |
| B 召回路线 | 公司事件、题材/行业、关注、趋势、回撤分别标注路线；同主题成员轮换，新增唯一证券占名额；160 只廉价候选的 24 只子集进入深查。新旧第三方题材均为弱证据。 | 路线提供发现机会，不等于公司催化或预测优势。 |
| C 时间与财务 | 回购/增减持公告查询截至取证允许的信息日；报告期动态选择；风险计划覆盖目标 D 至第 10 个交易日的自然日区间，含周末，记录修订/已完成和日历不足；解禁结果检查日期、身份、截断状态；最终模型输入截止与报告生成时间分开。 | 日期级公告的具体发布时分未知；当前证券主表不能倒称历史逐时点可得。单日 6000 行解禁响应可能截断，空响应仍未知。 |
| D 证据进入最终判断 | 关键风险、官方正文、量价、财务按类有界保留；重复涨停压缩成历史摘要。报告归档精确最终 packet、证据正文节选及已取得/已展示/被引用计数。 | 最终 packet 受预算限制；未展示的原文不能追认模型当时知道。 |
| E 八股审计 | 逐只核对旧原句、当时可见节选、相关官方 PDF 与处理，见下方审计文件。 | 旧源只归档了 39 条最终证据节选，没有逐字完整最终候选 packet；旧派生仅作事后工程审计。 |
| F 派生完整性 | 同时验证源报告和 AI 响应哈希，核对精确最终 packet 与证据；记录源哈希、校验版本及选择变化。目标日已过/缺精确 packet 时只产出非主报告的事后工程审计。 | 仅证明保存文件的一致性，不证明供应商数据真实或模型推断正确。 |

## 八只原始选择与新运行

旧源 `upgrade_runs/20261001T124209-5f31dbc0` 的 8 条原始选择为 3 重点、5 观察。完整逐句表在隔离目录 `third_round_audit/eight_stock_sentence_audit.md`。主要结论：平高电气和江淮汽车的金额换算被旧验证器误拒；康辰药业的 9218 万元舍入成立，但“唯一公告”不成立；国电南瑞的 8899.85 万元与来源 8898.50 万元不符；滨江集团跨股比较缺证据绑定；深物业 A 的“唯一公司公告”超出抽样覆盖。襄阳轴承的减速器收入 13.14 万元有当时可见官方文字支持，规模很小；上海洗霸的官方正文主要是减持、估值和盈利风险，没有证明新正向公司催化。六份相关官方 PDF 的重新取得字节哈希与旧机器抽取记录相符，但本轮没有做人工逐页法律解释。

旧源的离线重校验在 `third_round_audit/revalidations/20261001T154242-6fecb822`：形式上 1 重点、3 观察、4 剔除，状态为 `posthoc_engineering_audit`，`primary_eligible=false`，**不是新预测**。

本轮只执行了一次当前真实 TuShare + DeepSeek 三阶段链路。原运行 `third_round_runs/20261001T155126-df32c0d4` 保留；随后修正验证器对股票代码/日期/期限数字的误拒，仅对该轮已存原始模型输出和精确 packet 做确定性重校验，得 `third_round_runs/20261001T155518-afd585f2`，没有第二次付费模型运行。行情日 2026-09-30，最终输入截止 2026-10-01 15:50（北京时间），目标交易日 2026-10-08。初筛 160、深查 24，五路深查分配 5/5/5/5/4；精确最终 packet 含 38 条证据。原模型选择 2 重点、5 观察；两只重点分别因将来源未注明单位的 `fd_amount` 转述为 1.21 亿元与 3268 万元而剔除。新验证后 **0 重点、5 观察**：`000678.SZ`、`603590.SH`、`002866.SZ`、`000011.SZ`、`000560.SZ`。观察名单不表示可买、会涨或已有净收益优势。

本轮 `tushare_source_matrix` 共 246 条调用/缓存记录：113 `cached_data`、36 `data`、62 `empty_unconfirmed`、17 `cached_empty_unconfirmed`、12 `possibly_truncated`、6 `cached_possibly_truncated`。3 次 AI 阶段调用合计记录 154,178 token。保存的来源矩阵与报告包含逐接口状态；空值和截断没有按无风险处理。

前瞻文件 `tracking/20261001T155518-afd585f2-9f4418a6ff69.json` 记录 5 只观察股的 H1/H3/H5/H10 共 20 行，现均为 `d_open_not_yet_due`；H10 终点超出现有日历时另标 `calendar_unavailable`。独立汇总在 `third_round_audit/tracking-summary.json`：目标日 1 个、重点 0、可计算 0，所有收益与胜率仍为未知。固定定义为目标 D 开盘至对应期限收盘的复权价格观察，不是实际成交或净收益。

## 可复核文件与操作

隔离数据根目录：`/home/administrator/projects/quantlab-pr189/data/scout_live_20260929`。完整原报告及哈希在各 run 目录。供 Astra 阅读的脱敏最小包是 `third_round_audit/astra_review_bundle.zip`（同名文件夹可直接查看），包含旧源/旧派生、新源/新校验的报告关键字段、原 manifest、模型可见输出、精确 packet（旧源仅有当时保存的证据节选）、引用源片段与快照映射、八股审计和前瞻汇总。不含 API Key、环境变量或内部思维文本；子集不是完整 `report.json` 的字节副本，原始文件哈希须回到原 run 核对。

在 Scout 工作树的 WSL 环境中运行：

```bash
uv run pytest -q
uv run ruff check .
uv lock --check
git diff --check

uv run scout --doctor --config config/scout_taskbook.example.json \
  --canonical-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/canonical \
  --output-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/third_round_runs

uv run scout --track-run /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/third_round_runs/20261001T155518-afd585f2 \
  --canonical-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/canonical \
  --output-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/third_round_runs
```

仅在 10 月 8 日及后续数据成熟、隔离数据副本可安全更新后重复前瞻观察；不会回填漏日预测。原 canonical、旧 run 和失败记录均未覆盖。本轮未下单、未造成交、未购买新权限。工程交付可复核；选股准确率与可信净收益优势 **待前瞻验证，当前未达证据标准**。

## 自审与检查

本轮新增 9 个针对性场景，仓库完整测试 `143 passed`；`ruff check .`、`uv lock --check`、`git diff --check` 均通过。旧基线 134 项，差额来自第三轮新增用例。最终 HEAD、提交及推送状态以 `SCOUT_TASK_STATE.md` 收尾记录和 PR #192 为准；本文不提前声称尚未执行的远程检查。
