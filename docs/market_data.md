# Scout 行情数据约定

Scout 使用本地 Parquet 数据，不需要旧 QuantLab 代码运行。
用 `--canonical-dir /absolute/path` 指向数据根目录；默认是当前目录下的 `data/canonical`。
旧项目 `master` 上生成的以下六类数据可以直接读取，文件布局和字段单位保持兼容。

## 必需数据

| 相对路径 | 字段 | 说明 |
|---|---|---|
| `securities/securities.parquet` | `instrument_id, symbol, name, exchange, market, board, list_status, list_date, delist_date` | 当前证券主表，不是历史时点证券状态 |
| `calendar/calendar.parquet` | `exchange, trade_date, is_open` | Scout 按 `SSE` 的开放交易日扫描；日历至少覆盖运行当天 |
| `daily/year=YYYY/month=MM/YYYY-MM-DD.parquet` | `instrument_id, trade_date, open, high, low, close, pre_close, volume, amount` | 每个交易日一个文件；价格为未复权价，成交量为股、成交额为元 |
| `adj_factor/year=YYYY/month=MM/YYYY-MM-DD.parquet` | `instrument_id, trade_date, adj_factor` | 累计复权因子；计算时使用原始价格乘因子 |

日期列使用 Parquet 日期／时间类型，读取后转为 `datetime.date`。
`instrument_id` 示例为 `600000.SH`、`000001.SZ`；`market` 是 `SH`／`SZ`，
`exchange` 是 `SSE`／`SZSE`。上市状态 `L`、板块 `主板` 才进入当前候选范围。
`list_date` 必须有值，`delist_date` 可以为空。

需要截至所选日期的连续 21 个交易日分区。整个必需分区缺失就停止；个股历史不齐、
日线无效、复权因子缺失或流动性不足时，排除该股票并在报告记录数量。
历史离线运行仅供数据诊断，不能当作消除了幸存者偏差的回测。

## 可选数据

| 相对路径 | 字段 | 单位与缺失处理 |
|---|---|---|
| `daily_basic/year=YYYY/month=MM/YYYY-MM-DD.parquet` | `instrument_id, trade_date, turnover_rate, total_mv, circ_mv` | 换手率存小数，例如 0.052 表示 5.2%；市值为元。报告 `turnover_rate_pct` 转为 5.2；缺失保留未知 |
| `daily_price_limit/year=YYYY/month=MM/YYYY-MM-DD.parquet` | `instrument_id, trade_date, pre_close, up_limit, down_limit, exchange, source, source_record_id` | 供应商实际涨跌停价，保留来源；不按统一百分比推算 |

切勿直接把 TuShare 原始 `vol`、`amount`、`turnover_rate` 等字段按名字复制进来：
必须先按上述单位转换。否则筛选阈值和报告指标会失真。

## 如何复用旧数据

如果旧项目已经有完整数据，直接传入其绝对路径：

```bash
uv run scout --offline --canonical-dir /path/to/old-quantlab/data/canonical
```

旧的数据下载／更新命令属于 `master`，已从 Scout 分支移除。
如需继续使用，应在单独的旧项目工作目录中更新数据，再让 Scout 只读该目录。
本次精简不会删除磁盘上已有的行情或研究报告；这些文件不在版本控制清理范围内。
未来独立行情采集器可以输出同一格式，但当前尚未实现。

数据适配层保留的 `save_*` 方法用于临时演示和测试夹具。
`--live`、`--offline` 和 `--track-run` 不调用这些写入方法。
