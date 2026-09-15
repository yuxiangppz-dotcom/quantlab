# 研究状态查询

运行 `uv run python -m quantlab research-status`；加 `--json` 输出 JSON。

退出码：0 表示报告正常生成（可有明确识别的旧摘要）；1 表示存在损坏或未识别产物；2 表示报告无法组合。JSON 模式失败时也输出 JSON，含 `overall_status=composition_failed` 与 `error`。这些状态描述报告完整性，不表示策略就绪。

只有明确匹配已知生产版本的旧摘要归为 `identified_legacy`。历史引擎版本还必须匹配 `portfolio_engineering_backtest` 类型；有未知 experiment_schema 时不能依靠引擎版本降级。合法 JSON 的未知版本归为 `unrecognized_format`；解析失败、非对象 JSON、当前契约损坏归为 `malformed_evidence`。严格目录入口仍拒绝不合约产物。

三类不兼容产物均不获得证据资格，也不修改原文件。已识别旧版只表示认识该格式，不认证其内容。

本轮本地只读验收：44 项已识别旧版、6 项未识别、6 条目录证据；文本与 JSON 退出码均为 1。查询前后所有被检查 summary.json 的字节哈希保持一致。不得将目录条数解释为策略有效或可交易的证明。
