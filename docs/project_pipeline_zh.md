# 从 TuShare 到每日账户的一条主线

> **版本说明：下面保留 v1 外部 context 接口的说明。当前推荐的 v2 中证800项目请先读
> [CSI800 主线手册](csi800_project_zh.md)，其 `universe → init-study → build`、压力测试、
> 发布条件和本地验收要求优先适用。不要直接照搬下面的旧启动命令跳过这些阶段。**

本项目定位是**个人 A 股日频研究与模拟账户**。行情源、特征、训练、组合、账本和运行状态形成
闭环；不承诺盈利，不包含券商自动交易。原模型与账本内核继续复用，新增 pipeline 负责串联，
不另建一套模型或回测引擎。

## 入口与存储职责

```bash
uv sync --frozen --extra research --extra qlib
uv run quantlab pipeline --project config/project.local.json plan
```

从 `config/project.example.json` 复制成本地配置（不要提交本机路径或凭据）。所有路径相对配置
文件解析。`workspace` 是一轮不可覆盖的研究产物；`account`、`registry` 是持续使用的账户和
模型登记目录。每月新建研究 workspace，保持 account、registry 不变，避免重训重置现金。

| 层 | 输入 | 输出与约束 |
|---|---|---|
| TuShare | 环境变量 token、明确日期与权限 | 原始响应、请求参数、实际获取时间；有限重试、节流、错误脱敏 |
| Canonical | typed 日行情/复权/basic/指数/涨跌停/ST/停牌 | 单位统一，日历一致性及覆盖校验；完整日事务回执 |
| 来源证据 | 当时股票资格、行业、数据发布时间与版本 | 未知保留未知；今天下载不意味着历史上当时已知 |
| 共享特征 | Canonical + PIT context | 固定 Qlib Alpha158 表达式；历史分批与单日采用同一个函数 |
| 训练 | 已封存特征包、成熟标签 | 月度滚动、purge、独立验证早停、Ridge/LightGBM、可恢复产物 |
| 组合/回放 | 分数、原始行情、规则/费用/公司行动 | 缓冲换仓；真实股数、现金、可卖性、容量、拒单、费用 |
| 模型运行 | 审核后登记/启用的模型 | 独立记录实际启用时间；不能事后倒填前向模型 |
| 每日账户 | 最新来源快照、昨日已封存订单 | 先结算再预测；封存下一日订单；漏日补记不能补造订单 |
| 评价 | 持仓/成交/现金/权益、同期指数 | 毛净收益、成本、换手、风险和信号诊断；失败保留证据 |

TuShare 单位：daily 的 vol 手→股、amount 千元→元；daily_basic 市值万元→元、换手率百分比→小数。
原始价进入股数现金账本，复权价只作研究特征/标签。`pre_close` 不能当成前一日未复权成交价。
[日行情文档](https://tushare.pro/document/2?doc_id=27)、
[每日指标文档](https://tushare.pro/document/2?doc_id=32)。

## 一次性历史接入

```bash
# 此命令明确允许请求供应商、写入本地 Canonical；先核对日期和权限。
uv run quantlab pipeline --project config/project.local.json sync --execute
# 首次接管旧分区，审查后才加 --adopt-existing；不覆盖已完成的来源事实。
uv run quantlab pipeline --project config/project.local.json build
uv run quantlab pipeline --project config/project.local.json train
uv run quantlab pipeline --project config/project.local.json replay
uv run quantlab pipeline --project config/project.local.json report
uv run quantlab pipeline --project config/project.local.json status
```

默认同步从 start 前一年开始，给因子/容量窗口留足历史；明确 `--start/--end` 可以缩小请求。
首次下载按日期分区，不把多年股票行情塞进一次可能截断的 API 请求。触及响应行数保护阈值会
明确失败，须按该接口支持的粒度拆分，不能把截断响应当完整数据。节流参数不是接口权限保证。
ST/停牌为空响应仅说明该接口该日返回空；仍须本地核对权限、接口覆盖和返回语义。
停复牌按 `suspend_type` 解读：R 为复牌，有行情时不误作停牌；S 的明确日内区间按 15:00 收盘判定，
全日停牌阻止交易。同日无法排序的 S/R 冲突、未知类型和无法解析的时间保持 unknown；缺行情不凭复牌补价格。
[停复牌字段定义](https://tushare.pro/document/2?doc_id=214)。

完整交易日由七类分区及元数据快照共同绑定。先验证、持久化待提交事务，再发布数据和完成回执；
中途崩溃会继续同一事务。已经完成的分区只核验，不原地刷新；供应商修订须建立新来源版本并
重新生成研究产物。旧文件存在不等于有来源凭据，采用旧文件的事实会写入回执。

`build` 每批最多 63 个决策交易日并补足 lookback，分批写 Parquet；内存估算超预算会停止，
不会静默抽股票。特征路径读取并绑定 `config/security_code_changes.csv`：旧代码使用原上市日期及
变更前的有效区间，新代码从生效日开始。保留原代码，不把两段价格/持仓静默拼接；未知身份明确报错。
新合同含 `dated_code_lifecycles_v1_no_cross_code_stitching`，已有旧合同产物需要隔离重建，不修改指纹。
历史价格保留全部已声明研究股票的标签端点，不因当期退出股票池而丢掉价格。
`start/end` 覆盖训练与标签尾部；`test_start/test_end` 是打分范围。框架自动从首个打分日的
**下一交易日**开始回放，避免第一天拿尚未存在的信号成交。

## 必须有来源的四份输入

这些是数据证据，不是要求本地重新实现框架。缺少任一份时，plan/status 会列出；不得伪造。

1. `context` Parquet：恰好 `trade_date,instrument_id,eligible,industry,known_at,source_id,revision_id`。
   唯一日期/证券；eligible 是布尔或未知；行业为当时分类。股票池用历史资格，不用今天的上市
   名单/成分反推。它应包括研究窗口的所有决策日，逐日保留退出股票的必要历史信息。
2. `availability` Parquet：恰好 `trade_date,known_at,source_id,revision_id`，每个源交易日一行，
   覆盖因子 lookback。known_at 是该日所用行情/复权等输入版本都已可知的最晚时刻，必须有出处。
   回溯收集的历史事实只能形成显式假设的研究证据，不能认证历史真实发布时间；每日路径还会
   加上真实本地获取时间作为下界。时间戳必须含时区。
3. `execution_policy` JSON：schema 为 `quantlab_execution_evidence_v1`；policies 数组各项含
   instruments（精确证券代码列表）、start/end、source_id、known_at、participation、rules、fees。
   每个证券/日期恰好一项，规则和费用对象使用 `ResearchQuantityRules/ResearchFeeScenario`
   的完整字段及有效期。佣金、最低佣金、印花税和其他费用分开；滑点/参与率属于显式研究假设。
   不能以统一零费率替代未知事实。详见 `quantity_kernel.py` 数据类及合成端到端测试里的结构；
   **测试费率不是经核验的实盘报价**。可选 valuation_overrides 含
   instrument_id/session/price_fen/source_id/known_at，仅用于没有当日 bar 的有据估值，不能覆盖
   已观察行情，也不能据此把停牌股票变成可交易。
4. `corporate_actions` JSON：coverage(start,end,source_id) 与 events，沿用 `ml/corporate.py`。
   空事件也必须有“该区间确无相关事件”的来源覆盖。现金分红净额、送股、拆股按已支持结构；
   配股、非整数股、持有期税和特殊退市清算缺证据/适配时停止，不跳过持仓。

共享特征契约及 lineage、market.jsonl、initial_marks、benchmark 和每日输入 manifest 都由新
链路生成，不再要求本地 Codex 重写这些适配器。指数字段产生的是**价格指数基准**，不冒充全收益指数。

## 每日与每月运行

新推荐配置在北京时间 **18:00** 截止，成交假设仍是下一交易日收盘。TuShare daily_basic
官方更新时间为 15:00–17:00，因此不能把 16:00 写死成所有来源都能满足的时间。
截止时间绑定配置、模型、账户与封存回执；旧配置省略该字段时保留 16:00。旧 16:00 账户迁移
应新建明确配置的模拟账户并记录对照，不能修改已封存历史。

```bash
uv run quantlab pipeline --project config/project.local.json register --fold YYYY-MM --model lightgbm
uv run quantlab pipeline --project config/project.local.json activate --model-id MODEL_ID --effective-from YYYY-MM-DD
uv run quantlab pipeline --project config/project.local.json init-account --as-of YYYY-MM-DD
# 后续每日：一次入口完成当天同步、共享特征、市场适配、账户结算和下一日决策。
uv run quantlab pipeline --project config/project.local.json daily --as-of YYYY-MM-DD --sync --execute
uv run quantlab pipeline --project config/project.local.json account-state
uv run quantlab pipeline --project config/project.local.json account-state --set paused
```

init-account 从明确的空仓现金开始；不能用来覆盖已有真实账户。初始日期要在研究 bundle 的
日历里，最新每日输入会保留原日历前缀并追加后续交易日。历史补跑使用 `daily --as-of ...`
而不加 sync，只使用已经接入的数据。只按交易日依次推进；重复同日同输入为幂等。
空仓开户可包含更宽的报价范围，首日采用实际输入范围，共同证券价格仍须一致。每日成交证据范围
包括今日特征证券、实际持仓以及昨日冻结订单涉及证券，避免待买证券退出当日股票池后无法结算。

本地建议 17:15 后启动，失败可在 18:00 前有限重试。若过了 18:00 才拿到完整数据，仍可结算
昨天的已封存订单并记录账户，但当日不会创建合格的新前向订单。运行不应伪造当时已拿到数据。
模型过期或无合格启用模型时，继续可验证的结算，阻断新决策并报告原因。
截止前修复模型或解除暂停后，可用同一命令重试。账本不改写，决策追加尝试；成功计划只冻结一次，
超时重试不获得前向资格。次日结算、账户状态及报告使用同一份有效决策，详见 `ml_service_zh.md`。

每月新 workspace 同参数重训，检查报告后 register/activate；保留同一 account、registry。
更改组合/标签/截止参数属于新策略配置，不能让旧账户静默接纳。需要定时器、日志保留、失败
通知、磁盘预算和备份恢复演练；实际本机部署及至少连续 20 个交易日观察是本地验收任务。
前向模型不能因回测最好就自动启用。学习数据、评分模型与累计账户三者分开保留。

## 可以退出日常使用的东西

- `ml prepare-inputs/export-history`：保留给已有封存历史和外部特征接入；新 Canonical 主线无需手工调用。
- 顶层 `daily/shadow`、固定年份及 round/weekly 实验：留作复现，不再作为新日常入口。
- 手工生成逐日 market、feature contract、initial marks、benchmark 的重复脚本：主线已有适配器。
- 历史实验产物/可再生缓存可按保留政策另行清理；不能删除唯一原始数据、来源凭据或账户链。

`data/`、quantity kernel/scheduler、公司行动、personal 真实账本和被调用的 Alpha158 工具不是
可以随便删的“旧代码”。目录名老不等于功能重复；这次没有为了整洁破坏冻结实验的兼容性。

## 验证和限制

云端合成测试覆盖从模拟供应商到训练、回放、报告和连续账户；可选环境还比较真正 Qlib 的
历史/单日特征一致性。它证明所覆盖的程序行为，不认证真实历史、收益、容量或长期运行。
本地须验收接口权限/单位/完整性、PIT 来源、费用/公司行动、真实样本对账、资源消耗及定时恢复。
ML 文档中的底层接口依旧可用；若与本手册入口、时间、交接任务描述冲突，以本手册为新主线。
