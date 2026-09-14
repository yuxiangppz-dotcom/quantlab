# 首条历史回放：剩余输入缺口与回放卡草案（2026-09-13，基于 v4）

依据 `data/products/s4_first_replay_admission_v4/`（生成一次、独立核对
all_ok=true）与本地封存证据。本文不读取策略收益，不启动回放；经济路径
保持 0。

## G1 prior20 窗口（两个证券、六个停牌日期）

已核实事实：
- 000301.SZ：2021-12-22 停牌（suspend_type=S、timing 空，来源
  `lifecycle_context_v1/suspensions/year=2021/month=12`，记录 id
  e34e5f29…）、2021-12-23 复牌（R）；封存重请求
  `s4_entry_raw_precision/attempts/daily_000301.SZ_20211222` 为已验证空
  响应；本地日线分区无该日行。
- 000777.SZ：2021-12-07/08/09/10/13 连续停牌（S、timing 空）、12-14
  复牌；五次封存重请求均为已验证空响应；本地无行。
- 三类证据（空响应/无本地行/S 记录）同指一天，但同源（Tushare 派生），
  不构成独立来源。

待决定的研究规则（二选一，事前冻结）：
- 规则甲（维持现状）：窗口必须含 20 个有成交额的交易日 → 两券
  prior20 未知 → 提案保持 BLOCKED；
- 规则乙（停牌剔除补足）：S 日不计入 20 日窗口、向前补足 20 个有成交
  交易日 → 两券可形成 prior20 → 解锁。
两案都必须在结果前写入回放卡；不允许看结果后选择。

## G3 身份、市场状态与数量规则（固定 20 只）

本地已有正证据：
- 涨跌停上下限与日线：`daily_price_limit` 分区 1212 个交易日全覆盖；
  2022-01-04 的原始 OHLC/涨跌停值在封存
  `s4_first_entry_plan/plan.json` 的逐券 context 内（v4 包已带入）。
- 数量规则：各提案 context 携带逐券 buy/sell 网格与单笔上限（来源为已
  审阅的收盘 LIMIT 基本规则，见
  `docs/development_tasks/closing_quantity_evidence_v1.md`）。
- 停复牌记录：`lifecycle_context_v1/suspensions` 年/月分区（执行日上下
  文可按同路径核对 2022-01）。
- 精确执行日成交量：7 只经 reconciliation 验入、13 只封存整数，已全部
  落入 v4 包。
仍未知（缺口，类别=本地证据待映射）：
- market_open（20/20）：需把 2022-01-04 各券日线行（有量成交即为正证
  据）与当日停复牌记录映射为逐券正证据；文件与日期已定位（canonical
  `daily/year=2022/month=01`、`lifecycle_context_v1/suspensions/
  year=2022/month=01`），属一次有界映射任务。
- 历史身份/信号资格（20/20）：需以
  `lifecycle_context_v1/stock_st`、`securities/securities.parquet`
  （上市/退市日期、板块）在 2021-12-31 时点核对 20 只；同类有界映射。
类别标注：G3 全部为"本地证据待映射"，无外部资料缺口。

## G5 费用

已知口径（用户申报）：佣金万分之 0.86（20 万元内）、最低 5 元；卖方印
花税按日期分档（2023-08-28 前千分之一、之后万分之五）。
可声明的研究情景：基准=申报佣金+当期印花税+5bp 不利滑点；压力=15bp 滑
点（沿封存配置，不新增版本）。
未核实事实：申报佣金是否已含过户费/征管费等、2022 年历史口径——
`additional_fee_rate`/`additional_fee_fixed_fen` 保持 None。
类别="确实缺外部资料"，最小动作：用户一句话确认（含 or 不含，含则数
值）；不默认为零，不重复计收。

## 下一条有限回放卡（草案，不授权本轮执行）

- 身份：S4-A 首条历史基线，`run_id=s4_first_replay_c80_v1`；
  economic path 1/80（基准成本场景）。
- 输入：v4 输入包（20 提案、名义数量、执行上下文）；
  `RiskLedgerCheckpoint.start(signal_date=2021-12-31,
  initial_cash_fen=20_000_000, pending_orders=build_first_pending_orders(
  package), config=<C80 绑定>)`；`LedgerSessionEvidence` 由
  `research_session_from_context` 组装，marks=2022-01-04 原始收盘。
- 前置（全部满足才开跑）：G5 用户答复写入费用场景；G3 两项映射完成并
  落入上下文；G1 规则决定冻结（甲→两券剔除出 20 名单按第 21 名顺位？
  否——按委托不换人，两券名额留现金；乙→补足窗口）；执行规则适用期
  覆盖 2022-01-04。
- 停止条件：任一执行证据未知（preflight 已给 2022-01-04 停止点）、
  净值非正、数据分区缺失；停止保留最后完整日。
- 报告：逐日净现金/持仓/费用/决定、目标 vs 实际仓位、未成交与锁仓；
  不输出年化收益宣称，结果只作工程验收。

## 类别汇总

| 项 | 类别 | 最小动作 |
| --- | --- | --- |
| G1 窗口规则 | 研究规则待决定 | 冻结甲或乙（一句话） |
| G3 market_open/身份 | 本地证据待映射 | 一次有界映射任务（文件/日期已定位） |
| G5 附加费用 | 确实缺外部资料 | 用户一句话确认费用构成 |
