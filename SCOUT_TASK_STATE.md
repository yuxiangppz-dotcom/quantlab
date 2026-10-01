# Scout 本轮任务状态

- 目标：按 2026-09-30 的 `Scout_Codex_Development_Taskbook.md`，完成多入口发现、AKShare 热榜快照、AI 证据筛选、冻结盘前报告、1/3/5/10 交易日前瞻观察与累计展示。
- 起点：`f624a8836f06e2f292aca21e179db92d30c20ded`，分支 `codex/scout-evidence-integrity`，工作树起始干净，PR #192 草稿。未发现并发 Scout 或 agent loop 写入进程。原自动心跳保持暂停。
- 交付代码 HEAD：`c1cc9089c2d9cc9ca643c45d552b97bc58066575`；前一提交 `a29e895`。两份代码提交均已推送至 PR #192，PR 正文已更新且远程 quality 检查通过。状态文件本身的后续提交以 `git rev-parse HEAD` 读取最新 HEAD。
- 数据：旧报告和不可覆盖运行保存在 `/home/administrator/projects/quantlab-pr189/data/scout_live_20260929`；原 canonical 不写。本轮新运行继续使用该隔离副本和独立输出目录。
- 已核查：旧 Scout 为 21 日量价扫描、最多 20 深查、DeepSeek 三阶段分析、旧式 1/3/5 收盘观察。新任务取消历史回测、交易账本、中证800和规则对照前置要求。
- 已实现：AKShare 东方财富 Top100 热榜适配、原始快照与可比变化；巨潮市场公告抽样独立事件入口；回撤/温和放量、板块、关注与趋势多入口；160 廉价召回/24 AI 深查及预算外原因；持仓/自选独立程序事实卡；目标交易日与冻结版本；1/3/5/10 交易日前瞻观察和跨报告累计。新口径不混旧报告。
- 验证：`uv run pytest` 118 passed、`uv run ruff check .` passed、`git diff --check` passed、`uv lock --check` passed；独立只读审查发现并修复事件前80截断、跨公司证据绑定、热榜41–100遗漏、空名单理由与个股卡缺口。真实 AKShare 快照 `/home/administrator/projects/quantlab-pr189/data/scout_live_20260929/source_checks/akshare_hot_rank_20260930_evening.json` 有 100 条，后续实时接口暂时断连，显式复用该晚间真实快照并标注抓取时间。
- 真实运行：`taskbook_runs/20260930T232216-81e436fc` 和 `20260930T233156-3cfaa455` 是保留的 `incomplete` 失败记录（最终模型引用范围有误，被校验拦截）；`20260930T233810-1b45ecc2` 是修复后的首份成功报告；最终审查后成功报告为 `taskbook_runs/20261001T000355-b1ecb95e`。它在北京时间 2026-10-01 00:03 生成，行情截至 9/30，目标交易日 10/8，非 9/30 盘前；100 条热榜、13 条可映射市场公告索引、160 廉价候选、24 深查、3 重点/5 观察，3 重点的此前收盘均等于已知涨停价，效果尚待观察。模型三次调用约 70,623 token；本轮不再重复付费运行相同会话。
- 前瞻产物：`tracking/20261001T000355-b1ecb95e-981b59039449.json` 为新报告 32 条 1/3/5/10 固定期限记录，现均为 `d_open_not_yet_due`；`tracking-summary.json` 和 `tracking-summary.md` 只选 10/8 最新有效版本，重点 H5 原候选3、可计算0。另对旧报告 `20260930T060351-9407f6e2` 生成了独立 legacy 观察，不混入新累计。
- 约束：不输出凭据、不下单、不虚构历史预测、成交或净收益；模型调用限于已有授权与单次实际报告预算。没有足够证据时允许空重点名单。
- 下一步：10/8 及后续目标日行情成熟后，在隔离 canonical 副本完成授权的数据增量更新，再按文档运行 `--track-run` 与 `--tracking-summary`；不能补造漏日预测。当前功能交付完成，策略效果仍待前瞻样本判断，不自动恢复已暂停的持续优化心跳。
- 恢复命令：在 Scout 工作树进入仓库，`uv run scout --doctor --config config/scout_taskbook.example.json --canonical-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/canonical --output-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/taskbook_runs`。真实运行需本机私密环境变量；不在状态文件记录值。

## 2026-10-01 TuShare 升级任务（进行中）

- 新任务书：`E:/浏览器下载/Scout_Codex_Tushare_Upgrade_Taskbook.md`。本轮起点 HEAD `babc322b3e0b9552c643a206ad80da08ec5883c6`，Scout 工作树起始干净，检查未发现并发 Scout 或 agent-loop 写入者。旧数据与 10 月 8 日冻结报告保留原位。
- 已复现并修复：原流程先用模板替换模型论述再验证；现在先验证原始选择，逐条剔除无效选择并记录原因，然后保留可核查的原始机会假设及程序补充风险。原始选择、验证记录和最终展示分别入报告。高度重叠路径使首路径吃满剩余预算；现在按实际新增唯一股票分配基本名额，余量轮询，各阶段保存路径分配诊断及个股首次分配位置。
- 已调整补证时序：24 只深查中，初始 8 只之外的定向巨潮公告索引及有界 PDF 正文机器抽取，均在最终模型输入冻结前执行；取消最终分级后追加公告却暗示参与判断的旧步骤。具体覆盖随真实运行记录，标题仍不当正文。
- 针对性用例和旧测试当前 `uv run pytest -q` 为 121 passed，`uv run ruff check .` 与 `uv lock --check` 通过。此处尚未宣称新的选股效果或完成六包接入。
- 2026-10-01 12:45 续记：账号的 15 个必做接口小样本均可返回数据，脱敏矩阵在隔离数据目录 `source_checks/tushare_matrix_20261001_upgrade.json`。TuShare 六包、申万行业、原始不可覆盖快照、状态矩阵、截断按日期拆分与 24 只深查股票的逐只解禁核对已接入。深查逐只解禁 24 次：1 次有数据、23 次空响应待确认；市场层多个单日仍达 6000 行上限，明确保留截断状态。
- 代码修复中发现并保留了 `upgrade_runs/20261001T120233-6dfeeee0`、`20261001T122501-74f9555b`、`20261001T123056-9363ba8c`、`20261001T124209-5f31dbc0` 等 incomplete 运行；另有初版派生 `20261001T120934-2f471585`、空名单版 `20261001T123632-39d72209` 及其中间重校验版。故障包括：市场概述数值误拒、候选行业字段沿用上一股票、模型引用未入最终输入的数值、旧校验未归档实际截短正文。均未覆盖或抹去；旧派生版不作为最终名单。
- 本轮最新可复核报告为 `upgrade_runs/20261001T124559-dae19e79`：源自 `20261001T124209-5f31dbc0` 的真实 TuShare+DeepSeek 三阶段输出，通过已归档的精确最终提示词证据做确定性重校验，没有新增付费调用。行情截至 9/30，信息截点 10/1 12:42，北京时间 10/8 为目标交易日；160 只初筛、24 只深查，五入口深查分配 5/5/5/5/4。原始模型 3 重点/5 观察；最终 0 重点、2 观察（000678.SZ、603200.SH），其余 6 条因数值无依据或“唯一公告”超出抽样覆盖而剔除。观察股 2/2 此前收于已知涨停价，表现尚不成熟，**没有达到可信选股效果的证据**。
- 前瞻：`tracking/20261001T124559-dae19e79-12bf7e92b4a3.json` 有 8 条 H1/H3/H5/H10，全部 `d_open_not_yet_due`。`tracking-summary.json/.md` 采纳该最新版本，1 个目标日、0 重点、可计算 0。不下单、不虚构成交或净收益。隔离 canonical 仅供读取，旧报告与快照完整保留。
- 测试：修复行业串股、提示词实际节选、中文日期、股票代码和无依据数值后，`uv run pytest -q` 134 passed；之后增加证据原始快照引用与回归用例，相关 Scout 测试 119 passed、Ruff passed，最终完整检查仍待提交前执行。新接入 TuShare 证据将原始快照路径放在归档 `snapshot_refs`，提示词节省预算不带路径。最新报告生成于该元数据补丁之前，原始快照仍可通过本轮 `tushare_source_matrix` 查到。
- 交付：P0 提交 `74d4dec`、六包与证据闭环提交 `bfba74ff56e97afc241c2c0f7adb93551aa083a2` 均已推送到草稿 PR #192；PR 正文已更新，远程 `quality` 检查成功。本次最终 `uv run pytest -q` 134 passed、`uv run ruff check .`、`uv lock --check` 和 `git diff --check` 全通过。此状态收尾提交以 `git rev-parse HEAD` 为最新 HEAD。按 `docs/scout_tushare_upgrade_zh.md` 操作；10/8 后再取成熟行情做前瞻观察，不能回填今天的胜率。草稿 PR 保持未合并，效果判定为待观察、未达可信收益证据标准。
