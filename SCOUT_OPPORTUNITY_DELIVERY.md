# Scout 机会选择升级交付

日期：2026-10-03。版本 `opportunity_v1`。**工程检查通过；新配置尚未经真实模型运行，选股效果待前瞻观察。** 本轮没有新股票预测，不替换桌面的 10/8 冻结名单。

## 基线、代码与状态

- 起点：`778bd6b47e88f31458f264356992c35da149f3ca`，任务书指定、工作树干净、远程已推送；检查无并发 Scout/agent-loop 写入者。
- 工作树：`C:/Users/Administrator/.codex/worktrees/scout-ai-assistant/quantlab-review-pr189`。
- 分支：`codex/scout-evidence-integrity`；草稿 [PR #192](https://github.com/yuxiangppz-dotcom/quantlab/pull/192)。提交/推送与最新 CI 状态见下方收尾记录，最终文档提交号用 `git rev-parse HEAD` 查询。
- 设计与定义：[SCOUT_OPPORTUNITY_DESIGN.md](SCOUT_OPPORTUNITY_DESIGN.md)。恢复状态：[SCOUT_TASK_STATE.md](SCOUT_TASK_STATE.md)。自动优化仍暂停。

## 四个工作包

| 包 | 实现与可检查结果 |
|---|---|
| A | `opportunities.py`：事件身份、重复/更新、首次采集和公开时间分开、追加索引、名义规模缺口、日期级价格反应与行业背景 |
| B | `market.py` / `pipeline.py`：事件、板块、关注、量价和回撤采用不同依据；原 160/24 上限，唯一证券分配；完整阶段与排除理由 |
| C | `opportunity_ai.py` / `facts.py` / `opportunity_view.py`：全部深查的相对排序、同类未选比较、完整性与事实校验；原报告中显示取舍、反证和集中度 |
| D | `ranking_tracking.py` / `cli.py`：独立冻结排名，全部深查 H1/H3/H5/H10、固定同行、开盘前价格差、日期等权 H5 组诊断；原 TopN 统计不变 |

新增配置 `config/scout_opportunity.example.json`，复用 DeepSeek Flash，未替换日常模型。新增合成/审计脚本及 `tests/scout/test_opportunity_selection.py`。完整文件范围可用起点至当前 HEAD 的 `git diff --stat` 查看；真实数据与模型输出均不进入 Git。

## 实际验收

| 检查 | 本轮结果 |
|---|---|
| 完整 `uv run pytest -q` | **182 passed**，10.48 秒 |
| 机会与 DeepSeek 定点回归 | 37 passed；包含 34 个机会回归及既有 3 个 DeepSeek 回归 |
| `uv run ruff check .` | passed |
| `uv lock --check` | passed，锁文件未改 |
| `git diff --check` | passed |
| CLI doctor / 旧报告 ranking / 合成排除 summary | 已实际执行，全程无网络 |
| 推送后 CI | 收尾记录补充实际远程结果，不沿用旧 HEAD 的成功 |

失败与修复：模拟集成曾被旧校验中的“只有…公告”正则跨逗号误拒，误将“承认只有量价假设，未补造公告利好”识别为穷尽公告声明；修复为同一句子片段内匹配，保留原反证，新增回归。自审还修复只展示索引却继承未展示 PDF 正文状态/规模的问题，扩展比较解释和参与条件的事实约束，并保存 schema/截断错误时模型的公开响应。格式与长行问题经 Ruff 修复。早期失败诊断、先前合成和审计版本保留为本地产物。

## 合成示例（不是投资证据）

统一产物根目录：

```text
/home/administrator/projects/quantlab-pr189/data/scout_live_20260929/opportunity_upgrade_20261003
```

最终示例：`synthetic_v3/runs/20260202T190000-9eb2d073/report.html`，同目录有 Markdown、JSON、manifest、脚本模型响应。

- 合成合格池 180 股，廉价召回 138 股（上限 160），实际深查 24 股；不能为了凑满预算补造发现线索。
- 全部 24 股完成比较：趋势 22、回撤改善 1、事件 1；人为模拟排序，最终 2 重点/4 观察。覆盖不足场景单独回归测试，不冒充模型自然判断。
- 模拟三次调用的 user prompt 字符数 625 / 34,898 / 128,927；实际模型和供应商调用均 0。未验证真实 token、费用或延迟。
- `synthetic_v3/synthetic_ranking_tracking/20260202T190000-9eb2d073-2daca524e57b.json`：96 行全部未到期。
- 同目录 `20260202T190000-9eb2d073-f5a7b217e915.json`：96 行全部按人工构造未来行情计算；校验 gap 与 R5 分离、固定同行、路径低点。
- `synthetic_v3/ranking-summary-d98bd96e4198.json/.md`：实际 CLI 排除合成报告，有效真实报告为 0，不将 24×4 当独立实验证据。

`synthetic_v1/v2` 未覆盖。合成报告明示模拟标识，不能当作当天推荐。

桌面另有 `Scout机会逻辑升级_2026-10-03/最新合成流程示例_不是真实推荐.html`，与旧预测文件夹分开。浏览器安全策略拒绝本轮 `file:` 预览，因此没有浏览器截图验收；HTML 内容、转义和结构由回归检查验证，用户可以双击文件查看。早期正则误拒的复现记录在 `failure_archive/false_positive_diagnostic.json`；原 pytest 临时运行文件未作为持久实验包交付，不能声称交付了该临时失败的完整 raw 包。

## 本地旧资料兼容审计

`local_workspace_v2/local_compatibility_audit.json` 已实际生成；`local_workspace` 旧版保留。两份真实旧报告 manifest 与字节哈希核验，原文件未改变：

| 原运行 | 旧最终股票数 | 映射事件数 | 新排序兼容 |
|---|---:|---:|---|
| `taskbook_runs/20261001T000355-b1ecb95e` | 8 | 23 | 旧结构，不补造 |
| `third_round_runs/20261001T155518-afd585f2` | 5 | 2341 | 旧结构，不补造 |

第二份事件档案中：长期背景 1824、历史不足待核实 421、可能更新待核实 93、重复 2、例行 1。这些数量是取得资料的映射，不是 2341 个新利好，不代表新增信息有效。索引标记实际写入时间，历史覆盖仅两份报告。

旧最新报告的 CLI 排序观察快照在 `local_workspace_v2/ranking_tracking/20261001T155518-afd585f2-28b1d600ba04.json`：`legacy_structure_no_ranking`，0 行，明确没有当时完整比较。原 5 股及 20 行 TopN 跟踪保持原位，`tracking.py` 没有改动。10/8 尚未开盘，不填真实收益。

本机只读 doctor 结果：本地完整行情截至 9/30；日历到 10/15；当前 shell 未加载 DeepSeek key，仅提示存在与否，无密钥输出。正常运行需使用原本机私密环境；本轮无需读取凭据。

## 操作说明

在 Scout 工作树的 WSL 中执行。以下 doctor 和两种观察命令只读行情；`--live` 是下次用户正常运行的付费/供应商链路，**本轮未执行**。不要重复同一市场会话追求好看名单。

### 1. 核对配置和本地数据

```bash
cd /mnt/c/Users/Administrator/.codex/worktrees/scout-ai-assistant/quantlab-review-pr189
DATA=/home/administrator/projects/quantlab-pr189/data/scout_live_20260929
WORK=$DATA/opportunity_upgrade_20261003/local_workspace_v2
uv run scout --doctor --config config/scout_opportunity.example.json \
  --canonical-dir "$DATA/canonical" --output-dir "$WORK/runs"
```

`WORK/event_index` 已有部分旧资料索引。换一个全新根目录会失去该部分历史，首次见到资料的新颖性更应保持未知。doctor 不测试供应商权限；本地分区存在不代表每条内容有效。

### 2. 下次正常研究运行

```bash
# 使用原本机私密配置载入 DEEPSEEK_API_KEY / TUSHARE_TOKEN，不写入仓库或命令正文。
uv run scout --live --config config/scout_opportunity.example.json \
  --canonical-dir "$DATA/canonical" --output-dir "$WORK/runs"
```

成功后打开输出的 `RUN/report.html`：先看重点/观察，机会部分查看类型、为何优先它、反证与交易条件；展开内部表查看全部深查比较。检查 `opportunity.validation=complete` 和冻结哈希。若 schema 截断、漏评、数字或引用矛盾，报告显示 incomplete，保留原输出，不输出可用排名。新 prompt/schema 的真实遵循情况是下次运行验收项。

### 3. 行情成熟后更新观察

```bash
RUN="$WORK/runs/实际运行ID"
# 仅使用已合法取得、成熟且完整的本地行情；这些命令不会自动下载。
uv run scout --track-ranking "$RUN" --canonical-dir "$DATA/canonical" \
  --output-dir "$WORK/runs"
uv run scout --ranking-summary --output-dir "$WORK/runs"
# 原 TopN 观察仍独立存在，如需同时观察：
uv run scout --track-run "$RUN" --canonical-dir "$DATA/canonical" \
  --output-dir "$WORK/runs"
uv run scout --tracking-summary --output-dir "$WORK/runs"
```

排序快照在 `$WORK/ranking_tracking/`；累计命令打印带哈希的新 `ranking-summary-*.json`，同名 Markdown 可读。旧 `tracking/` 和旧统计定义不混用。当前真实数据的未来分区未到期；成熟后缺 bar/因子/价格限制分别保持未知，不默认停牌或成交。

### 4. 无网络复现合成示例与本地映射

```bash
# 输出目录必须全新；不覆盖 synthetic_v1/v2 或旧 run。
uv run python scripts/scout_opportunity_example.py --output-dir /tmp/scout-opportunity-new-example
uv run python scripts/scout_opportunity_audit.py \
  --run "$DATA/taskbook_runs/20261001T000355-b1ecb95e" \
  --run "$DATA/third_round_runs/20261001T155518-afd585f2" \
  --output-dir /tmp/scout-opportunity-new-local-audit
```

## 剩余限制与达标判断

开发标准：代码与合成流程已完成，最后提交/CI 见收尾记录。效果标准：**待观察，尚无可信净收益优势证据**。真实模型的新完整比较、来源真实性/经济关系、事件新颖性及真实上下文成本需下次正常运行复核；前瞻收益必须等待目标日及 H5 成熟。

没有新增平台评论流、盘口或昂贵接口；评论仍是用户导入样本。没有新增多模型投票、阈值寻优或自动优化。没有订单、模拟成交、canonical 修改、历史名单重选、密钥提交或 PR 合并。完成此次有边界升级后停止扩展，固定版本积累观察；20 个成熟目标日可做第一次诊断，不能等同效果证明。

## 提交、推送与 CI 收尾记录

实现与检查已完成，提交及远程 CI 正在收尾；确认后在本节记录实测提交号和链接。
