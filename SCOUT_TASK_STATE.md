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
