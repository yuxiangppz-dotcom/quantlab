# 共享程序规则

本轮读取实际程序输出的版本、配置哈希、`rule_results` 和资格上限。这些参数是初始研究设置，不是已验证的盈利规律。

- [市场、组别及形态配置](../../../src/quantlab/scout/article_config.py)：`ArticleConfig`、`config_dict`、`config_hash`。
- [程序筛选与两条形态](../../../src/quantlab/scout/article_engine.py)：`analyze_stock`、`assess_market`、`assess_groups`、`allocate_budget`。交易日窗口、信号日锚定复权、突破位排除当日、回调过程与确认日均由程序完成。
- [龙虎榜及资金](../../../src/quantlab/scout/article_risks.py)：`risk_config`、`normalize_lhb_events`、`evaluate_moneyflow`。同范围事件不盲目累加，F 与 G 口径分开；缺数不变零。
- [价带及参与情景](../../../src/quantlab/scout/article_levels.py)：`compute_price_levels`、`evaluate_frozen_level`、`freeze_entry_reference`、`check_open_reference`。已确认拐点与当前重建价带的可得时间不同，历史测试只能用当时冻结的带。
- [主 agent 日线审查契约](../../../src/quantlab/scout/article_review.py)：`research_hash`、`validate_review`、`bind_review`。research哈希、股票与事实范围、浏览时间和不完整日线不得作正面支持由程序检查；名称／复权补充字段和canvas是否实际可读仍需主 agent 如实核查。

主 agent 不复制公式及数值阈值。使用上述输出解释条件是否通过、为什么受限、待核查什么。规则版本或配置改变时冻结新记录，旧报告不重写。

`priority` 是盘前研究优先级；开盘参考情景、雪球日线定性观察、可成交性、实际收益是不同字段。`entry_check=pass` 仍不意味着订单成交。D1 价格变化只作诊断，不假设普通 A 股当天买入当天卖出。

当前 `review_mode=codex_browser_daily_v1`。第7步的实际浏览由主 agent 完成并记录主体、URL、可见日期与截止时间；截图和外部视觉模型不是验收前置项。八步中的基础股票池、环境、组别、A/B形态、资金／龙虎榜／价位限制、名额分配、冻结与跟踪保持原规则。运行程序准备了事实表不代表已经执行主 agent 浏览。
