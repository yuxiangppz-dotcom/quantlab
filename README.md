# QuantLab

个人日频 A 股机器学习研究与模拟账户工具。主线是：**TuShare → 来源与规范数据 → 共享特征 → 滚动训练 → 组合/账本 → 每日运行与评价**。
当前支持本地研究和模拟；没有券商交易权限。工程验证、真实数据验收与策略有效性分别记录。

## 从这里开始

```bash
uv sync --frozen --extra research --extra qlib
uv run quantlab pipeline --project config/project.example.json plan
uv run quantlab ui
```

Python >=3.12，推荐 WSL2/Linux。新主线只使用 Qlib 的固定 Alpha158 表达式及保存的
Ridge/LightGBM 模型，不合并其他完整量化框架。

**当前推荐版本是历史中证800主线：[CSI800 操作与验收手册](docs/csi800_project_zh.md)。**
`project.example.json` 为 v2；旧 v1 项目保持原实验口径。
中证800按历史生效成分还原，不用今天的800只回填历史；ST不研究、不新买，已有持仓仍记账并尝试退出。

| 任务 | `quantlab pipeline --project 配置文件` 后的命令 |
|---|---|
| 检查路径、来源缺项与产物 | `plan` / `status` |
| TuShare 历史/增量获取，事务恢复 | `sync --execute` |
| 中证800月度观察资料（不等于历史生效证明） | `sync-index-observations --start 日期 --end 日期 --execute` |
| 历史股票池/资格、研究协议 | `universe` / `init-study` |
| Canonical→共享特征及来源契约 | `build` |
| 研究、真实股数现金回放、基准报告 | `train` / `replay` / `report` |
| 独立资金/滑点压力测试 | `stress --resume` |
| 模型登记、模拟准入、真实时刻启用 | `register` / `release` / `activate` |
| 同协议候选更新（不自动启用） | `refresh --as-of 日期 --parent-model-id ID` |
| 一次性空仓模拟账户初始化 | `init-account --as-of 日期` |
| 每日获取→特征→市场适配→账户决策 | `daily --as-of 日期 --sync --execute` |
| 持续账户状态与暂停 | `account-state` / `account-state --set paused` |
| 数据/账户健康、成熟信号监控 | `health` / `monitor --as-of 日期` |
| 隔离备份、自检、数据修订影响 | `backup` / `verify-backup` / `revision-plan` |

本地完成来源核验后使用 project.local.json；示例配置本身不表示数据齐全。
[本地 Codex 任务](docs/ml_v2_local_completion_prompt_zh.md) 列出真实证据、部署与验收工作。

## 主流程与边界

- 新配置截止为北京时间 18:00，随模型/账户冻结；晚到数据不倒填成合格前向信号。
- **历史研究**：`check → train → replay → report`。默认下一交易日收盘开始的 10 日 rank 标签，
  882 日训练 +126 日验证，每月更新；Ridge 对照与 LightGBM，缓冲换仓。
- **每日模拟**：模型先注册/启用；每天接收一份只读数据快照，结算昨天封存的股数订单，
  对当日特征预测，以实际持仓构建下一日目标与订单，并封存完整决策和账户状态。
- 晚到可补记账户，不能事后补造订单；输入/配置不允许覆盖完成日。模型过期、漏日、缺少证据
  有明确停止或阻断状态。相同输入可安全重试；训练输入变化导致的失效运行必须新建输出。
- 下一收盘撮合是研究假设；配股、零碎股、持有期税和退市结算等仍需真实证据及专项适配。
- 默认组合参数是研究起点，不是经过实证的最优配置。无真实数据时不发布收益、容量或有效性结论。

[训练与回放接口](docs/ml_daily_v2_zh.md) · [每日服务操作手册](docs/ml_service_zh.md) ·
[架构与迁移](docs/quantlab_architecture_zh.md) · [实现与验收边界](docs/ml_framework_completion_zh.md)

## 模块职责

| 模块 | 职责 |
|---|---|
| `pipeline/` | 唯一推荐项目编排：同步、特征、研究、每日输入和状态 |
| `data/` | 来源、日历、证券历史、Canonical 存储及更新 |
| `alpha/` 与共享 research 数据工具 | 特征及数据推导，不操作真实账户 |
| `research/ml/` | 数据契约、训练、诊断、模型登记、统一决策与模拟服务 |
| `portfolio/`、quantity kernel/scheduler | 目标结构、股数/现金/费用/约束的共享内核 |
| `backtest/`、`execution/` | 既有回测语义、生命周期与执行规则能力 |
| `personal/` | 真实账户信息、手工成交和资金流水，独立于模拟账户 |
| `ui/` | 只读展示及明确用户触发的本地功能 |

共享基础设施不能因为名称带有旧实验编号就删除。历史研究继续保留原协议和结果；
[历史入口参考](docs/legacy_readme_reference.md) 仅用于复现。`quantlab daily`、顶层 `shadow`
仍是旧入口，不会静默改成新 ML 服务。新的日常操作统一使用 `quantlab pipeline`；`quantlab ml` 保留为底层/外部数据高级入口。

## 验证与开发

```bash
uv run pytest -q
uv run ruff check .
git diff --check
QUANTLAB_TEST_OPTIONAL_RESEARCH=1 uv run --extra research --extra qlib pytest -q tests/research/test_optional_runtime.py tests/research/test_ml_v2.py tests/research/test_ml_operations.py tests/research/test_ml_reliability.py tests/research/test_ml_service.py tests/pipeline
```

开发遵守 [AGENTS.md](AGENTS.md)：保护 PIT 与冻结语义，不伪造缺失事实，不提交本地数据或凭据。
真实数据只读接入、费用/公司行动核验、真实样本对账、WSL 定时任务和前向观察在本地验收。
