# S5 v1.1：PIT 特征物化与行业成员证据适配

GitHub Issue：#131；前置内核：#128 / PR #129；上层多策略研究计划：#127。

本任务在读取任何 S5 历史收益结果之前冻结。它只把调用者提供的时点数据转换成已冻结的 S5 v1 输入，不运行历史收益/NAV，不修改 S5 阈值、排序、Dynamic-N 权重，不调用 provider，不写 Canonical，不登记 Forward Shadow，不晋级策略，也不产生订单。

## 输入边界

调用者必须提供：

- 严格递增、最后一个交易日等于 `as_of` 的共同交易日历；
- 基准复权研究收盘序列；
- 行业复权研究收盘序列；
- 股票复权研究收盘序列与原始成交量序列；
- 带来源与有效区间的行业成员证据；
- 带来源与 PIT 声明的当日 research-universe eligibility。

序列允许携带 `as_of` 之后的观测，但物化只能读取共同日历到 `as_of` 的前缀；未来观测既不能改变历史特征，也不能改变该历史物化指纹。缺少共同交易日上的必要观测时保持 unknown，不 forward-fill 停牌/缺失 bar。

## 固定窗口与计算约定

全部窗口按共同市场交易日计数，均包含 `as_of` 端点。

1. `drawdown_from_120d_high`
   - 最近 120 个共同交易日的复权收盘；
   - `close_t / max(close_{t-119..t}) - 1`；
   - 少于 120 日、窗口中缺价或非正价格 => unknown。

2. `new_low_rate_20`
   - 对最近 20 个共同交易日逐日判断；
   - 某日若收盘 `<=` 该日及之前共 60 个共同交易日窗口的最低收盘，则记为一次 trailing-60 low；并列最低也计入；
   - 因此完整计算至少需要 79 个收盘；
   - 任一所需日缺价/非正 => unknown；
   - 结果 = 20 日中命中次数 / 20。

3. `volatility_percentile_252`
   - 单日收益使用简单收益 `close_t / close_{t-1} - 1`；
   - 每个端点的 20-session realized volatility = 最近 20 个简单日收益的样本标准差 `ddof=1`；
   - 对最近 252 个端点的 realized-vol 形成历史分布，包含当前端点；完整计算需要 272 个收盘；
   - 当前分位 = `count(hist_vol <= current_vol) / 252`；
   - 任一所需价格缺失/非正 => unknown。

4. 行业 / 基准 20-session return
   - `close_t / close_{t-20} - 1`，需要 21 个共同交易日；
   - 行业相对收益 = 行业 20D return - 基准 20D return。

5. 股票相对行业 20-session return
   - 同一 21 日定义；
   - 股票 20D return - 所属行业 20D return。

6. `close_to_ma20_ratio`
   - `close_t / mean(close_{t-19..t})`；
   - 20 日窗口必须完整且价格为正。

7. `ma20_slope_5`
   - `MA20_t / MA20_{t-5} - 1`；
   - `MA20_{t-5}` 使用截至 `t-5` 的 20 个共同交易日，因此至少需要 25 个收盘。

8. `up_down_volume_ratio_20`
   - 使用最近 20 个单日股票简单收益（因此需要 21 个收盘）与对应 20 个端点日原始成交量；
   - 正收益日与负收益日分别计算平均成交量，零收益日忽略；
   - 若正收益日或负收益日任一为空、任一所需成交量缺失/为负、或负收益日平均成交量为 0，则 unknown；
   - 否则为 `mean(volume | return > 0) / mean(volume | return < 0)`。

## 行业成员与 eligibility

行业成员证据有效区间采用闭区间 `[effective_from, effective_to]`，`effective_to=None` 表示未声明结束。

对某股票声明的 `sector_id`：

- 当日只有一个无冲突、`pit_verified=True` 的有效成员事实且指向该行业 => `membership_verified=True`；
- 当日只有明确且已验证的有效成员事实指向其他行业 => `membership_verified=False`；
- 没有当日证据、只有未验证证据、或存在互相冲突的有效证据 => `membership_verified=None`，保持 unknown。

Research eligibility 只接受 `as_of` 当日且 `pit_verified=True` 的事实；否则输出 `None`。

## 物化指纹

物化结果必须有稳定 SHA-256 指纹，绑定：

- schema/version；
- `as_of` 与共同交易日历；
- 截至 `as_of` 真正被物化读取的价格/成交量值；
- 各 source id 与实际使用日期范围；
- 截至 `as_of` 已生效的成员证据；
- 当日 eligibility 证据；
- 物化器版本与固定窗口约定。

未来日期的新增/修改不得改变更早 `as_of` 的指纹；过去值、来源、成员事实或 eligibility 发生变化必须改变指纹。

## 输出

输出 immutable `S5Materialization`：

- `sector_observations`：可直接传给已合并的 S5 kernel；
- `stock_observations`；
- `issues`：按实体/字段列明 unknown 原因；
- `fingerprint`；
- `research_only=True`、`performance_claim=False`、`broker_order_authority=False`。

## 测试与停止条件

合成测试至少覆盖：

- 120/79/272/21/20/25 日窗口边界；
- 新低并列规则；
- realized-vol `ddof=1` 与经验分位；
- 缺共同交易日 / 非正价格 / 缺成交量 / 单边涨跌窗口保持 unknown；
- 行业成员有效期切换、无证据、未验证、冲突；
- eligibility 日期与 PIT 声明；
- 输入顺序不影响输出；
- 未来价格/成交量/未来成员记录变化不影响更早物化及指纹；
- 历史值、source id、历史成员变化会改变指纹；
- 不修改调用者输入。

验证：`uv run pytest -q`、`uv run ruff check .`、`git diff --check`、research-runtime CI 全绿。完成本任务后，下一步只允许先做“历史行业 taxonomy / membership 数据可用性审计 + 冻结诊断协议”，不能直接根据 S5 历史收益调参。
