# Scout PR #192 五项审查修复交付

日期：2026-10-04。工程修复完成；选股收益效果仍待观察。

## 范围与版本

审查来源：`E:/浏览器下载/Scout_PR192_Review_fdfb54b_20261003.txt`。
起始 HEAD：`fdfb54b463db4f35d7e8beed4525d37a054a6b42`。
代码交付 HEAD：`f0692c5d5d394b07a85e070aa6303e65e9029650`。
文档收尾后的精确最终 HEAD 见桌面 `final_delivery_receipt.json`，亦可用 `git rev-parse HEAD` 核验。
[PR #192](https://github.com/yuxiangppz-dotcom/quantlab/pull/192) 保持草稿，未合并。

## 五项修复对照

| 项目 / 修复位置 | 复现输入 | 修复前输出 | 修复后输出 | 验证 |
|---|---|---|---|---|
| 公告身份 / opportunities.py:event_records | 两份不同 URL 公告使用相同采集占位正文；同 URL 索引与 PDF | 不同公告误合并 | 索引按文档 URL、回退标题与日期识别；两份公告各为一个事件；同文后补正文不当新催化 | 4 个新增回归原均失败；相关 38 项通过 |
| 同行事实 / ai.py:validate_selection | 完整 validate_comparisons 中引用同行 20 日、相对 5 日收益、额比、突破、合同比例 | 5 种合法事实均 core_fact_missing | 类型校验保留实际输入完整同行与 program_facts；自由数字兜底仍限原字段 | 正例和错主体、单位、方向、数值、缺事实负例；相关 54 项通过 |
| 盘后价格窗口 / opportunities.py:price_reactions | 9/30 19:00 公布、行情截至 9/30 | 发布前价格被算入反应 | 起点为已知发布时间前最近收盘；无发布后交易时段，变化 null、post_event_not_observed | 原 7 失败 / 2 通过；修复后相关 58 项通过 |
| 冻结分母 / ranking_tracking.py | 冻结两股，观察为空、部分或缺日历 | 原候选分母消失 | 从冻结名单左连接观察，原分母保留；缺失理由明确；重复、外来、身份不符记录拒绝 | 原 9 失败 / 2 通过；修复后相关 69 项通过 |
| 失败输入存档 / pipeline.py | 最终响应 schema 错误、截断、传输失败 | selection_input_packet 丢失 | 请求前保存 packet、证据、精确 user prompt、schema 和哈希；区分响应收到与送达未知；仍 incomplete、空名单、无有效冻结 | 原 3 项均失败；修复后完整测试通过，三份失败报告保存 |

已知发布时间按上海时区区分盘前、盘中、盘后、非交易日。盘中日线窗口包含公布前价格，明确标为混合窗口；日期级发布时间保持不确定，未知日期不使用取得时间替代。
旧索引不改写，历史已误合并身份不能据此自动恢复。未来新索引使用 index_document_v2。

## 提交、推送与远程检查

五项分别提交并推送；下列代码 CI 均为 SUCCESS：

- `28bfe56b9da8f58dab6090ff001304c497309d89`：[公告身份 CI](https://github.com/yuxiangppz-dotcom/quantlab/actions/runs/37137782342)
- `4f1b44b45127d4cae27be22aca468fb9c2840155`：[同行事实 CI](https://github.com/yuxiangppz-dotcom/quantlab/actions/runs/37137976227)
- `23b29124e7a2cdcb6a30bb743857ba2285ad9018`：[价格窗口 CI](https://github.com/yuxiangppz-dotcom/quantlab/actions/runs/37138249486)
- `1aa85ca8fb686ee53b9e02dfff0dd8f95f4e87c5`：[冻结分母 CI](https://github.com/yuxiangppz-dotcom/quantlab/actions/runs/37138540417)
- `f0692c5d5d394b07a85e070aa6303e65e9029650`：[失败存档及完整 CI](https://github.com/yuxiangppz-dotcom/quantlab/actions/runs/37139059379)

本地 `uv run pytest -q`：220 passed，11.97 秒。最终代码远程日志：220 passed，14.87 秒。新增回归 38 项；`uv run ruff check .`、`uv lock --check`、`git diff --check` 通过。
中间一次测试把内存 tuple 直接与 JSON list 比较，导致 3 个断言失败；已改为 JSON 规范化并双核对哈希，该阶段产物保留。未将这次失败归因为模型或市场效果。

代码变更：src/quantlab/scout/{ai.py,opportunities.py,pipeline.py,ranking_tracking.py}、tests/scout/test_review_fixes.py。恢复记录：SCOUT_TASK_STATE.md；价格定义同步：SCOUT_OPPORTUNITY_DESIGN.md。

## 可检查产物与运行说明

仓库：`C:/Users/Administrator/.codex/worktrees/scout-ai-assistant/quantlab-review-pr189`。
新增隔离产物：`/home/administrator/projects/quantlab-pr189/data/scout_live_20260929/review_fixes_20261004`。

- `p1-before.log` 至 `p5-before.log`：基线失败复现。
- `full-tests.log`：全仓测试日志。
- `five_fix_comparison.json`：实际函数输出与两份旧报告字节哈希核验。
- `before_failed_requests/`、`intermediate_tuple_list_assertion/`、`after_failed_requests/`：三个阶段的失败样本原档。
- `missing_calendar_case/`：两股冻结名单缺日历的人工案例。

上述新运行均为合成输入或模拟客户端测试，不是实际选股预测；实际模型调用、供应商调用、新真实预测均为 0。失败样本的最终输入有 7 个候选，不能当作真实 24 股深查运行。
复核：在 Ubuntu 的此工作树执行 `uv run pytest -q`、`uv run ruff check .`、`uv lock --check`。实际 Scout 配置及操作继续参考 SCOUT_OPPORTUNITY_DESIGN.md 和原交付说明；此轮不更改模型、策略阈值、数据接口或运行预算。

## 判断与下一步

五项工程问题已修复并有回归证据。原真实报告、失败记录、索引和观察均保留；两份旧成功报告的 SHA256 与先前记录一致。本轮没有更新 canonical、下单、虚构成交或收益，也未恢复自动优化。
新机会配置尚未进行真实模型验证；目标交易日 10/8 的旧冻结名单尚未形成成熟 H5 结果。当前不能宣称胜率提高或具备可信净收益优势。后续应在授权与预算内做有限真实链路验证，并使用到期行情观察冻结结果；本轮没有执行这些操作。