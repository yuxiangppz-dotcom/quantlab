# Scout 第二轮代码与选股复核包（给 GPT-6 Astra）

请独立审查**工程正确性、证据是否足以支持每只股票的等级，以及下一步改进优先级**。请把能复现的缺陷与策略判断分开，不要把候选变化解释成已验证的收益改善。本文件只陈述截至 2026-10-01 本机留存的结果；如无法读取原始运行目录，请注明判断仅依据本摘要。

## 审查版本和任务边界

- 仓库：[quantlab](https://github.com/yuxiangppz-dotcom/quantlab)，[草稿 PR #192](https://github.com/yuxiangppz-dotcom/quantlab/pull/192)，分支 `codex/scout-evidence-integrity`。本交接包依据代码 HEAD `5ca7a057b88ada6802895d15d5a2f243c5287852` 撰写；本文件的后续文档提交不改变所审代码。
- 最新任务书：本机 `E:/浏览器下载/Scout_Codex_Tushare_Upgrade_Taskbook.md`。它更新了原 Scout 范围，要求修复模型论述先被模板洗白、重复候选挤占路径预算、公告补证过晚，并在现有权限内接入 TuShare 证据包。不以历史回测、交易账本、AI 与简单规则对照或中证 800 限定为前置条件。
- 代码提交：`74d4dec`（原始选择验证、路径预算）、`bfba74f`（TuShare 六包与证据闭环）；`5ca7a05` 记录交付状态。上述提交已推送到 PR #192；上次远程 `quality` 检查通过。本次文档创建前工作树干净。
- 研究输出不是订单。没有实际成交、扣费净收益、成熟胜率，也没有证明 Scout 有可信正收益优势。

## 先看哪些实现

| 主题 | 代码入口 | 重点检查 |
| --- | --- | --- |
| 五入口召回及去重预算 | `src/quantlab/scout/pipeline.py`: `build_pool`、`candidate_diagnostics`、`run_scout` | 事件、行业/题材、回撤、关注、趋势是否真正召回不同公司；160/24 截断后的遗漏，重复路径如何分配。 |
| 四项发现分数与准入 | `src/quantlab/scout/market.py`: `scan_market` | 前一日/五日收益、放量和突破的相关性；流动性与 21 日线门槛；分数只用于发现，不能当上涨概率。 |
| TuShare 接口与证据 | `src/quantlab/scout/tushare_upgrade.py`: `TusharePack`、`collect_market_pack`、`collect_deep_pack`、`collect_sw_memberships` | 快照完整性、单日行数上限、发布时间/报告期、单位、空响应、缓存和接口失败是否保守处理。 |
| 模型和事实校验 | `src/quantlab/scout/ai.py`: `unsupported_numeric_claims`、`validate_selection`、`retain_valid_selection` | 原始论述是否在展示前验证；数字、公司身份、来源引用、抽样公告与全文推断是否被正确约束；误拒与漏放。 |
| 最终提示词证据 | `src/quantlab/scout/pipeline.py`: `evidence_packet`、`run_scout`；`src/quantlab/scout/sources.py` | 24 只深查的公告索引/PDF 是否在最后选择前进入输入；实际截短的证据正文是否归档；失败源是否仍标未知。 |
| 展示与前瞻 | `src/quantlab/scout/report.py`、`html_report.py`、`tracking.py` | 原始选择/拒绝原因/最终名单是否分明；停牌、涨停、未知可买性是否独立呈现；目标日 D 和 H1/H3/H5/H10 口径。 |

既有 [第一轮复核包](SCOUT_ASTRA_REVIEW.md) 对应旧代码 `ed03677` 和旧名单，不能把其中的 3 只重点/5 只观察当作这次的新最终名单。完整操作说明见 `docs/scout_tushare_upgrade_zh.md`，状态见 `SCOUT_TASK_STATE.md`。

## 可复核的真实运行

本机隔离数据根目录为 `/home/administrator/projects/quantlab-pr189/data/scout_live_20260929`，旧报告和每次失败运行均保留，未写原 canonical。最新报告目录：

```text
/home/administrator/projects/quantlab-pr189/data/scout_live_20260929/upgrade_runs/20261001T124559-dae19e79/
```

请优先读取其中 `report.json`、`report.md`、`report.html`、`manifest.json`，再交叉核对源运行 `upgrade_runs/20261001T124209-5f31dbc0` 中保存的原始 AI 输出和精确最终提示词证据。最新 `report.json` 的 SHA-256 为 `5b930192b7a7090565fb70f1989c94583c166e69dc459646901713998eb684cc`。最新版本是对**同一轮真实模型输出**做确定性重新校验的派生报告，没有再次付费调用模型或数据商。`revalidated_from_run_id` 记录其来源。

| 项目 | 实际结果 |
| --- | --- |
| 行情与信息时点 | 行情截至 2026-09-30；原信息截点 2026-10-01 12:42:09 北京时间；报告生成 12:45:59；目标交易日 2026-10-08。这是下一交易日准备版，不是 9 月 30 日盘前预测。 |
| 报告状态 | `live_research_unvalidated`，`primary_eligible=true`；仍需目标日开盘前重查公告、热榜与交易状态。 |
| 候选预算 | 准入 1,524、廉价候选 160、深查 24；深查五入口首次分配为事件/行业/回撤/关注/趋势 `5/5/5/5/4`。 |
| 模型原始输出 | 3 只重点、5 只观察，保存在 `selection_raw`。 |
| 校验后展示 | **0 只重点、2 只观察**：`000678.SZ` 襄阳轴承、`603200.SH` 上海洗霸，保存在 `selection`。6 只被剔除，原因逐只在 `selection_validation.rejected`：5 只论述含未获展示输入支持的数值，1 只把抽样公告说成“唯一公告”。 |
| 最终输入证据 | `prompt_evidence_audit` 的 `selection` 阶段有 39 个证据 ID；其中 PDF 机器节选 6 条，尚未人工逐份核查，精确发布时间部分未知。 |
| 涨停集中度 | 准入池 40/1524 前收盘等于已知涨停价，廉价池 38/160，深查池 9/24，最终观察 2/2。能发现强势股，但可买性和继续上涨均未知。 |

两只观察股的论述是**模型推断经程序校验后保留**，并非已独立核查的投资结论。襄阳轴承的可核查线索包括历史连板、供应商资金流和官方异动公告 PDF 机器抽取；相关减速器业务披露的收入体量很小，不能用题材标签推定业绩。上海洗霸的论述也应逐条对照其 `evidence_ids`、原文及历史行情，尤其要检查近期涨停后的价格风险。两只均需验证目标日是否可以买到；不要仅凭入选前涨幅讨论成功率。

本轮接口小样本探测中，15 个必做接口均返回过数据；探测矩阵在 `source_checks/tushare_matrix_20261001_upgrade.json`。最新报告的 `tushare_source_matrix` 是逐次调用台账，不等于所有接口都全量覆盖。市场级 `share_float` 若干单日达到 6,000 行上限并标可能截断；24 只深查股的逐只解禁查询只有 1 次有数据、23 次为空且保持 `empty_unconfirmed`。`kpl_concept_cons` 在探测中曾命中 3,000 行上限。最终输入的 39 条证据并未覆盖所有可用主题与事件；请检查选择阶段的信息压缩是否过度偏向历史涨停、资金流和被补查的公告。

**快照引用的版本差异**：当前代码给新 TuShare 证据写 `snapshot_refs`，但上面的最新报告产生于该元数据修复前，其原始快照须经 `tushare_source_matrix[*].snapshot` 查找。请不要因旧报告缺少逐条 `snapshot_refs` 就假称原始响应不存在；也请审查当前新代码能否保证每条新证据均可追溯。

开发期间另有 `20261001T120233-6dfeeee0`、`20261001T122501-74f9555b`、`20261001T123056-9363ba8c`、`20261001T124209-5f31dbc0` 等 `incomplete` 运行和早期派生版。它们暴露过市场概述数字误拒、行业字段串股、越权数值、未归档提示词实际截短正文等问题，全部保留；早期派生版不可当最终有效预测。若 Astra 判断最新校验仍有误拒或漏放，请给出最小可复现句子与对应证据节选。

## 前瞻观察与实际达标判断

- 最新观察文件：`tracking/20261001T124559-dae19e79-12bf7e92b4a3.json`，两只观察股 × H1/H3/H5/H10 共 8 条，均为 `d_open_not_yet_due`。同一数据根目录的 `tracking-summary.json/.md` 对 2026-10-08 只选最新有效版本：1 个目标日、0 个重点、0 个可计算收益。
- 观察定义是目标日 D 开盘到固定第 H 个交易日收盘的复权价格变化，不含排队成交、冲击、手续费或实际净收益。停牌/缺价应保留未知，不能填零或假设可交易。
- **目前工程任务完成，选股效果未达“可信收益优势已证实”的判断标准，状态为待观察。**名单缩短和证据校验更严格属于可审查的工程改进，不能据此声称胜率提高。不能用 9 月 30 日以后的价格反向修改已冻结名单。

## 请 Astra 重点回答

1. **代码缺陷**：按 P0/P1/P2 列出文件、触发条件、复现方法、影响和最小修复。优先看证据发生/公开时间与信息截点、模型数值的误拒和漏放、关联公司引用、供应商空响应及行数截断、报告派生的完整性。
2. **选股逻辑**：逐项评价四个相关的动量/量价分数与五入口是否真正提高候选多样性。为什么准入池 2.6% 前收盘涨停，深查升到 37.5%，最终 2/2？这可能是何种选样偏差，怎样在不针对单日名单拟合参数的情况下改进？
3. **逐股审查**：对襄阳轴承、上海洗霸核实可见证据、真正的公司事实、模型推断、反证和可买性；对被剔除的 6 只判断是合理排除还是校验器误拒。不要把原始 `selection_raw` 里的股票直接视为对用户正式推荐。
4. **信息源**：评估 TuShare 六包、AKShare 东方财富人气榜、巨潮抽样公告及 PDF 机器正文的时点和覆盖。说明哪些来源仅是线索，哪些可以支持公司事实；当前未接同花顺评论或全量新闻，不可当作已查询且无风险。
5. **下一步**：只建议能用新交易日冻结报告和后续真实行情验证的改动。若提出改排序、补证或前端，请说明预期改善哪一个可观测失败模式，以及如何避免再次只选封板股。不要在观察未到期时给出收益结论。

## 复核与运行命令

在 Scout 工作树运行无需密钥的检查：

```bash
uv run pytest -q
uv run ruff check .
uv lock --check
git diff --check
```

上次代码交付前完整检查为 **134 passed**，其余三项通过；PR 远程 `quality` 通过。这些只验证工程实现，不验证收益。只读环境检查：

```bash
uv run scout --doctor --config config/scout_taskbook.example.json \
  --canonical-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/canonical \
  --output-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/upgrade_runs
```

不要为复核重复付费运行同一市场会话、覆盖旧 run、补造旧日推荐、提交本机数据或输出任何凭据。
