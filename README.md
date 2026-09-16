# QuantLab

个人日频 A 股机器学习研究与模拟账户工具。主线是：**数据契约 → 滚动训练 → 组合与账户回放 → 每日封存决策 → 持续评价**。
当前支持本地研究和模拟；没有券商交易权限。工程验证、真实数据验收与策略有效性分别记录。

## 从这里开始

```bash
uv sync --frozen --extra research
uv run quantlab ml --help
uv run quantlab ui
```

Python >=3.12，推荐在 WSL2/Linux 下运行。Qlib 仅在复现旧适配器/可选测试时需要：`--extra qlib`；
新流程直接使用保存的 Ridge/LightGBM 模型，不要求把其他量化框架并入项目。

| 你要做什么 | 入口 | 说明 |
|---|---|---|
| 接入、检查历史数据 | `ml export-history` / `prepare-inputs` / `check` | 只读来源、PIT 与特征契约 |
| 做模型实验 | `ml init-study` / `train` | 月度滚动、成熟标签、独立验证、保存模型 |
| 评价可交易组合 | `ml replay` / `report` | 实际股数、现金、费用与市场约束的模拟 |
| 启用已核验模型 | `ml register` / `activate` | 记录真实启用时刻，不自动晋级 |
| 每日运行 | `ml init-service` / `run-day` | 先结算已封存订单，再形成下一日决策 |
| 观察/暂停/报告 | `ml service-state` / `service-report` | 连续状态、异常、实际持仓与同期基准 |
| 查看界面 | `quantlab ui` → ML 工作台 | 新流程默认入口；旧页面归入历史研究 |

所有命令上表省略了 `quantlab` 前缀。没有本地数据时先阅读
[本地 Codex 任务](docs/ml_v2_local_completion_prompt_zh.md)，不要制造输入使流程显示完成。

## 主流程与边界

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
| `data/` | 来源、日历、证券历史、Canonical 存储及更新 |
| `alpha/` 与共享 research 数据工具 | 特征及数据推导，不操作真实账户 |
| `research/ml/` | 数据契约、训练、诊断、模型登记、统一决策与模拟服务 |
| `portfolio/`、quantity kernel/scheduler | 目标结构、股数/现金/费用/约束的共享内核 |
| `backtest/`、`execution/` | 既有回测语义、生命周期与执行规则能力 |
| `personal/` | 真实账户信息、手工成交和资金流水，独立于模拟账户 |
| `ui/` | 只读展示及明确用户触发的本地功能 |

共享基础设施不能因为名称带有旧实验编号就删除。历史研究继续保留原协议和结果；
[历史入口参考](docs/legacy_readme_reference.md) 仅用于复现。`quantlab daily`、顶层 `shadow`
仍是旧入口，不会静默改成新 ML 服务。新的日常操作统一使用 `quantlab ml`。

## 验证与开发

```bash
uv run pytest -q
uv run ruff check .
git diff --check
QUANTLAB_TEST_OPTIONAL_RESEARCH=1 uv run --extra research --extra qlib pytest -q tests/research/test_optional_runtime.py tests/research/test_ml_v2.py tests/research/test_ml_operations.py tests/research/test_ml_reliability.py tests/research/test_ml_service.py
```

开发遵守 [AGENTS.md](AGENTS.md)：保护 PIT 与冻结语义，不伪造缺失事实，不提交本地数据或凭据。
真实数据只读接入、费用/公司行动核验、真实样本对账、WSL 定时任务和前向观察在本地验收。
