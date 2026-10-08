# Scout 文章策略：40 项反例验收映射

基准：`Scout_Article_Strategy_Taskbook_20261008.md` 第 15 节，并应用用户最新范围更新：**停用DeepSeek，第7步由主agent在雪球实际查看日线，不要求截图**。此表保留40项原编号和历史证据；被用户替代的旧图像／分时强制条件不会被改标为通过。

## 当前模式与授权变化

- 当前研究模式为 `codex_browser_daily_v1`。基础股票池、市场环境、方向、A/B形态、资金／龙虎榜／价位限制、名额分配、冻结和跟踪继续遵循八步规则。
- 第7步改为主agent真实打开雪球日K页面并记录URL、证券主体、可见日期范围、行情截止、实际浏览时间、支持、最强反证和未知。链接或页面按钮不等于已看日线；不依赖截图保存、图片上传或外部模型调用。
- 旧编号20中的分时部分，以及28–34、38的强制截图／外部视觉模型部分，由最新用户指令替代；证券／日期匹配、时点、不编造观察、负面证据和有界访问原则保留并应用到日线浏览。
- 此模式需要主agent在线浏览，**不表示电脑关闭后云端可以无人值守每天完成同等研究**。既有微信发送功能是否启用由根代理另行核对，不由本技能说明宣称已部署。
- 新模式的真实雪球浏览与最终冻结验收，须使用本次实际记录。下表历史C/A测试和旧视觉能力试验不是当前模式的浏览回执。
- 当前记录以 [article_review.py](../src/quantlab/scout/article_review.py) 为合同：外层research_hash绑定reviews，每维fact_ids；日线view=daily、visible_dates、status=complete/partial/unavailable，observed_at不早于准备cutoff。页面打开但canvas不可读只记partial、quality=unknown、正面support空。名称、复权等补充字段不等于程序自动核验了整幅画面。

## 阅读方式与执行证据

- **离线映射**：存在对应断言，通常使用合成价格、模拟接口或模型传输回调；本表不因此声明真实模型服从该反例。
- **部分／待补**：已验证其中一些机制，但编号中的另一条件仍缺独立反例或实际链路证据。
- **用户替代**：旧强制项目不再作为当前前置条件；保留历史测试映射和失败，不记作通过，也不以取消项目代替实际日线浏览。
- 每行函数名可直接作为 pytest node ID。下方别名均位于 `tests/scout/`；本轮全仓结果以最终交付回执为准，函数存在不等于本代理执行了全仓测试。

| 别名 | 测试文件 |
|---|---|
| E | [test_article_engine.py](../tests/scout/test_article_engine.py) |
| D | [test_article_data.py](../tests/scout/test_article_data.py) |
| S | [test_article_sources.py](../tests/scout/test_article_sources.py) |
| N | [test_article_notices.py](../tests/scout/test_article_notices.py) |
| R | [test_article_risks.py](../tests/scout/test_article_risks.py) |
| L | [test_article_levels.py](../tests/scout/test_article_levels.py) |
| C | [test_article_charts.py](../tests/scout/test_article_charts.py) |
| A | [test_article_ai.py](../tests/scout/test_article_ai.py) |
| P | [test_article_report.py](../tests/scout/test_article_report.py) |
| T | [test_article_tracking.py](../tests/scout/test_article_tracking.py) |
| I | [test_article_input.py](../tests/scout/test_article_input.py) |
| V | [test_article_review.py](../tests/scout/test_article_review.py) |

已由本代理执行的范围证据：引擎 29 项测试通过；引擎实际窗口覆盖审计单项通过；压缩输入范围测试在启用真实日线回放后 **11 passed / 1 skipped**。跳过的是需要另行提供真实 24 股 program JSON 的回放，不是实际 11 股回放。Skill 的 `quick_validate.py` 通过。其余测试执行结果由根代理完整检查记录。

当前日线审查合同 `V` 范围 **12 passed**：`test_partial_daily_can_be_frozen_as_unknown_without_external_model`、`test_incomplete_daily_does_not_count_as_support`、`test_unreadable_daily_cannot_supply_positive_support_or_quality` 验证不完整观察保持未知；`test_complete_error_list_catches_wrong_subject_peer_fact_and_future_daily`、`test_peer_funds_cannot_be_borrowed_for_this_stock`、`test_old_observation_and_empty_date_are_both_errors`、`test_review_cannot_be_reused_on_changed_facts` 分别覆盖主体／事实、日期／浏览时点与research哈希绑定。其余参数化节点处理损坏结构。这些都是离线合同反例，不证明真实canvas已读或全部可选字段已核验。

### 已保存的实际证据（与离线测试分开）

产物根为 `/home/administrator/projects/quantlab-pr189/data/scout_article_20261008`，不提交实验数据：

1. **6000 权限探测**：`permissions-real-6000.json`。DC 目录、成员、行情、板块资金与 THS 热榜均有 `available` 回执；档案明确为 `permission_probe_only`。它证明账户与此次取数能力，不证明正常研究已覆盖所有成员。
2. **完整来源包**：`full-source-pack-20261008T230102.json`。当时 `dc_member` 存在 available、partial、failed 三种回执，不能将整个成员目录宣称为完整；其他四个必接接口各有 available 回执。正常链路最终覆盖模式须以冻结 run 为准。
3. **已停用模式的真实图像与模型能力试验**：`article_vision_capability/20261008T225808/`。含真实截图、manifest、请求及响应、两次received回执；`corrected-result.json`为2调用、16349token、errors=[]。范围明确为`capability_only_not_selection`；600418.SH被模型评为reject、图评mixed，覆盖记录为9/24、9/28、9/29、9/30、10/8。这是用户停用前保留的能力档案，首轮错误同样保留；它不是当前主agent雪球日线浏览、正式候选发布或收益验收，也不授权再次调用DeepSeek。
4. **实际市场窗口审计**：`E::test_actual_saved_market_windows_coverage_and_unknown_history` 读取已存数据，无下载与写入。10/8 预期／日收益有效数均为3028；3日窗口3025/3028，MA20窗口3017/3028。收益中位数为+0.4599%，MA20上方比例33.4107%；历史状态缺口令R4保持unknown、市场状态unknown，未回填ST历史。
5. **真实日线输入回放**：`I::test_real_stock_daily_evidence_budget`使用9/30已存日线，实际找到11只A/B量价形态通过的样本，压缩文本74255字符。未证明主板、板块或风险资格，这11只不是正式候选。没有复制股票凑到24只；24股规模压力另由明确标注的合成测试覆盖。此证据验证输入组织，不证明主agent看过这些股票；外部模型已停用，不据此恢复真实发送。

当前主agent的真实雪球日K浏览记录应另存为`review_mode=codex_browser_daily_v1`，并与上述旧能力档案和日线回放分开。没有完整本次访问记录时不填写complete；canvas无可读证据记partial或页面不可取得记unavailable，未知不作正面支持。

## 逐项映射

| # | 要求 | 对应测试函数 | 覆盖及剩余边界 |
|---|---|---|---|
| 1 | 暴涨、远离MA20不能靠旧动量成为优先 | E::`test_extreme_old_momentum_never_overrides_new_shape_limits` | 离线映射；新形态失败不被旧分覆盖。 |
| 2 | 箱体只用t−1以前 | E::`test_breakout_uses_prior_box_not_signal_high_and_close_must_confirm` | 离线映射；提高t日high不改变B。 |
| 3 | 盘中突破、收盘回箱体则A失败 | E::`test_breakout_uses_prior_box_not_signal_high_and_close_must_confirm` | 离线映射；使用收盘确认。 |
| 4 | 仅缩量下跌不能当B已企稳 | E::`test_recovery_requires_real_rise_pullback_confirmation_and_exports_dates` | 离线映射；确认日必要。 |
| 5 | 仅在MA20上方不能代替B的时序 | E::`test_recovery_requires_real_rise_pullback_confirmation_and_exports_dates` | 离线映射；p、q、上涨／回调／确认必须真实存在。 |
| 6 | 放量回调或结构破坏不能被解释为洗盘 | E::`test_recovery_increasing_amount_or_broken_fixed_ma_cannot_be_called_washout`；P::`test_mock_missing_funds_cannot_publish_priority_but_missing_chart_alone_can` | 离线规则和发布上限映射；模拟文字不是实际AI冲突案例。 |
| 7 | 单股涨停、组内普跌不成主线 | E::`test_single_limit_up_with_group_declines_does_not_create_mainline` | 离线映射。 |
| 8 | 当前成员／热榜／事后公告不改变旧决定 | E::`test_newly_downloaded_history_and_postcutoff_members_never_forge_persistence`；S::`test_ths_rank_time_later_than_cutoff_or_retrieval_not_used`；N::`test_index_after_cutoff_is_audit_only_and_does_not_reach_reader`；T::`test_integrity_rejects_changed_reports_tampered_previous_or_backward_clock` | 各层离线映射；跨全pipeline同时注入三类来源的回归仍待补，不据此宣布实际历史预测可得。 |
| 9 | 来源失败真实降级；不混成无资金／未上榜；分钟非前置 | S::`test_permission_failure_is_not_successful_empty_and_secrets_not_receipted`；S::`test_default6000_sources_all_enabled_no_independent_product_dependency`；P::`test_partial_moneyflow_and_failed_lhb_never_become_zero_or_unlisted` | 部分；需补单个6000必接源／公告源失败一路影响冻结报告的集成反例。实际证据1、2只按各自范围使用。 |
| 10 | KPL／申万未到、旧日、截断正确标状态 | S::`test_today_lhb_before_close_is_not_neutral_and_early_kpl_delayed`；S::`test_old_trade_date_not_used_as_current_and_raw_retained`；D::`test_dropped_raw_rows_do_not_prove_complete_membership` | 部分；KPL、旧日和丢行已映射；申万特定到达时点及达到返回上限的独立反例待补。 |
| 11 | 多理由不重复累加；机构side分算；同名不乱去重 | R::`test_multi_reason_duplicate_is_one_event_without_readding_institutions`；R::`test_real_same_name_same_amount_rows_are_not_name_deduplicated`；S::`test_lhb_reason_side_and_duplicate_institution_rows_preserved` | 离线映射，事件与排名侧分开。 |
| 12 | 单位一致；复权不制造暴跌／突破 | D::`test_canonical_units_not_double_converted_and_full_basic_preserved`；D::`test_explicit_raw_tushare_schema_converts_thousand_yuan_and_hands`；S::`test_amount_volume_percentage_units_are_source_specific`；E::`test_historical_prices_anchor_to_signal_factor_without_false_split_return` | 离线映射；不能由字段名猜单位。 |
| 13 | 高开8%、昨收涨5%不等于赚5% | T::`test_actual_holidays_calendar_and_high_open_does_not_mean_user_earned_close_gain` | 离线映射：开盘到收盘约−2.78%；价格观察，不称用户成交收益。 |
| 14 | 开盘超冻结上限，后续涨停也不改判 | T::`test_failed_high_open_is_never_upgraded_by_later_rise_or_bound_change`；L::`test_open_reference_keeps_failures_and_does_not_infer_fill_or_later_prices` | 离线映射；开盘参考判断冻结。 |
| 15 | 不模拟T+0退出；涨跌停／成交未知不删 | T::`test_actual_holidays_calendar_and_high_open_does_not_mean_user_earned_close_gain`；T::`test_open_at_actual_limit_stays_unobservable_without_inferring_fill`；T::`test_all_frozen_and_open_pass_subset_retained_separately_even_suspension` | 价格观察范围已映射，fill不推断。当前没有交易退出模拟器，不能据此声称已验证跌停卖出或实际净收益；若新增模拟需另补退出约束测试。 |
| 16 | 风险期不凑优先名单 | E::`test_market_risk_priority_and_unknown_limit_are_not_false`；P::`test_market_caps_are_enforced_without_silently_trimming_frozen_candidates` | 离线映射：risk_off优先上限0。 |
| 17 | 数据空选与主动放弃原因不同 | P::`test_data_failure_and_deliberate_risk_pause_have_distinct_empty_messages`；E::`test_market_denominator_does_not_use_candidate_liquidity_gate_or_hide_missing` | 离线映射；用户提示与内部原因分开。 |
| 18 | 行序不改规则或预算 | E::`test_shuffled_bars_keep_metrics_and_routes_identical`；E::`test_budget_balances_both_stages_routes_and_prioritizes_eligibility_globally`；R::`test_funds_order_invariance_and_no_future_injection`；L::`test_future_injection_and_input_row_order_cannot_change_frozen_levels` | 离线映射，包括候选与组顺序反转。 |
| 19 | 阈值、零振幅、停牌、缺数、上市、重复、除权边界 | E::`test_breakout_three_percent_inclusive_boundary_and_real_excess`；E::`test_inclusive_ratio_tolerance_does_not_relax_tick_or_liquidity_qualification`；E::`test_duplicates_invalid_values_zero_range_and_listing_use_explicit_states`；E::`test_conflicting_snapshot_and_confirmed_suspension_preserve_denominators`；D::`test_conflicting_same_date_code_adjustment_unknown_not_last_wins` | 离线映射；仅技术比例容差1e−12，价格／资格不放宽。 |
| 20 | 无分时／身份证据不写已确认承接或锁仓 | 历史A::`test_complete_error_list_and_unknown_images`；P::`test_text_page_or_image_id_without_actual_dates_cannot_count_as_reviewed_chart`；当前Skill::`references/contracts.md` | 分时强制部分由用户替代，不记通过。当前仅日线，仍禁止推导分时承接／资金身份；没有实际日K访问时也不得写已复核。当前浏览记录验证须由根代理补实际证据。 |
| 21 | 单日、多日不合并；范围未知为review | R::`test_overlapping_multi_day_and_single_day_do_not_net_or_sum`；R::`test_untrusted_lhb_is_review_not_hard_exclusion`；P::`test_independent_lhb_events_are_not_net_merged_and_institution_unknown_is_not_zero` | 离线映射。 |
| 22 | −4000万／5亿排除；／20亿警示；AI不解除 | R::`test_lhb_severe_absolute_and_ratio_warning_are_independent`；P::`test_mock_conflict_cannot_publish_ai_upgrade_over_program_severe_sell` | 数值和发布上限离线映射；后者明确是mock，未验证真实模型看到该冲突会主动遵守。 |
| 23 | 完整未上榜、失败、净买入区分 | R::`test_lhb_severe_absolute_and_ratio_warning_are_independent`；S::`test_empty_full_event_query_neutral_but_dense_source_missing`；P::`test_partial_moneyflow_and_failed_lhb_never_become_zero_or_unlisted` | 离线映射，成功空表与失败分开。 |
| 24 | 3日比率用总和之比；大单非身份 | R::`test_moneyflow_uses_ratio_of_sums_and_keeps_f_and_g_separate`；I::`test_compaction_does_not_mutate_or_promote_actions_or_recalculate_ratios` | 数值离线映射；供应商统计标签和F/G分开，模型身份归因仍不由这些金额证实。 |
| 25 | B温和流出不一刀切；严重流出＋破位排除 | R::`test_mild_pullback_outflow_is_not_excluded_or_demanded_to_be_positive`；R::`test_severe_funds_combination_excludes_but_standalone_only_watch` | 离线映射。 |
| 26 | 右侧2根确认；新价带不回填旧触碰；除权限价正确 | L::`test_pivots_need_two_completed_right_bars_and_equal_platform_is_not_a_pivot`；L::`test_current_atr_cannot_backfill_old_touches_or_change_a_saved_frozen_band`；L::`test_touch_events_are_separated_by_three_calendar_sessions_and_no_backfill`；E::`test_historical_prices_anchor_to_signal_factor_without_false_split_return`；T::`test_mfe_mae_and_prices_use_one_scale_across_ex_rights` | 离线映射；confirmed_at与band_frozen_at分开。 |
| 27 | 最近压力使区间无效则watch；不跳远、不抬S | L::`test_nearest_resistance_is_not_skipped_and_invalidation_never_moved_to_improve_rr`；L::`test_at_resistance_is_watch_and_route_reference_only_band_is_not_double_counted`；I::`test_missing_close_or_selected_band_does_not_infer_favorable_replacement` | 离线映射；最近压力ID压缩后不重选。 |
| 28 | 网页文字／按钮不等于看图 | 历史A::`test_complete_error_list_and_unknown_images`；P::`test_text_page_or_image_id_without_actual_dates_cannot_count_as_reviewed_chart`；当前Skill::`references/contracts.md` | 截图／上传强制由用户替代。当前主agent必须真实查看雪球日K，链接、日K按钮或摘要不算已看；待本次真实访问与主体／日期记录验收，不声称历史测试证明此项。 |
| 29 | 股票／日期匹配；单日不概括多日 | 历史C::`test_chart_cutoff_and_sha_not_just_url`；C::`test_partial_day_cannot_confirm_window`；A::`test_complete_error_list_and_unknown_images`；当前Skill::`references/contracts.md` | 旧像素／SHA条件由用户替代。当前保留证券代码、名称、可见日K日期和截止核对，只评价实际可见范围；尚需本次雪球真实记录，不能用旧manifest代替。 |
| 30 | 量柱／均价口径／坐标未知不精确宣称 | 当前Skill::`references/contracts.md` 日线观察边界 | 原分时视觉模型强制由用户替代，不记通过。日线量柱不清就不称放量／缩量，不能推导精确VWAP、分钟价或身份；数值仍用程序事实，实际浏览验收按本次记录。 |
| 31 | 旧日支撑只用当时结构，当前对旧图标事后 | L::`test_current_atr_cannot_backfill_old_touches_or_change_a_saved_frozen_band`；当前Skill::`references/contracts.md` | 程序价带时序仍适用；旧分时模型强制已替代。主agent日K对照当前支撑时必须称事后位置描述，浏览记录不可倒填历史可得时点。 |
| 32 | 保留反弹与后续弱势，不只选成功片段 | 历史A::`test_actual_images_are_sent_as_native_multimodal_blocks`；当前Skill::`references/contracts.md` | 多截图上传强制由用户替代，不宣称已通过双图模型测试。当前主agent浏览可见日线时须同时记录改善与后续弱势、最强反证；待本次访问记录验收。 |
| 33 | 跨日连接非瞬时跳水；除权尺度不明支撑未知 | T::`test_mfe_mae_and_prices_use_one_scale_across_ex_rights`；当前Skill::`references/contracts.md` | 五日分时连接线强制由用户替代。日线复权程序测试仍保留；雪球日K口径不能对应结构时保持unknown，不凭视觉重算价格，不把日际变化说成瞬时跳水。 |
| 34 | 好图不当目标日已确认；新意见不倒改报告 | 历史C::`test_target_chart_does_not_rewrite_prior_signal`；P::`test_recovery_dates_invalid_interval_and_open_condition_do_not_claim_fill`；T::`test_integrity_rejects_changed_reports_tampered_previous_or_backward_clock` | 旧目标日分时强制由用户替代；pending与冻结完整性仍有效。当前日线浏览判断不等于目标日盘中条件，冻结后新意见独立带时间戳。 |
| 35 | 无效引用／越硬限制／自改阈值拒绝，一次纠错不升级 | 历史A::`test_complete_error_list_and_unknown_images`；A::`test_single_compact_correction_then_retain_valid_peers`；P::`test_mock_conflict_cannot_publish_ai_upgrade_over_program_severe_sell`；当前Skill::`references/contracts.md` | 外部模型纠错测试保留为历史；当前主agent判断同样不能改资格、价格或名额。程序发布上限映射仍有效，当前浏览记录schema／不存在证据／越界判断的集成反例由根代理补验。 |
| 36 | 截图不生成精确分时收益或极值成交 | T::`test_actual_holidays_calendar_and_high_open_does_not_mean_user_earned_close_gain`；T::`test_unfinished_window_never_uses_injected_same_day_close_or_future_prices`；当前Skill::`references/contracts.md` | 当前不要求截图且不做分时策略；仅日线价格观察、不推成交边界继续有效。主agent不能从日K宣称用户利润、低点买入或当日可兑现收益。 |
| 37 | 同目标日卖出限制保留；首次下载非新利空 | R::`test_old_event_remains_history_and_same_target_replay_preserves_restriction`；S::`test_cached_first_seen_does_not_become_historical_known_time`；N::`test_earliest_index_and_body_receipts_survive_later_retrieval` | 离线映射；原始first_seen及effective_target_date保留。 |
| 38 | 图访问有界失败／换源／unknown，不绕访问限制 | 历史A::`test_uncertain_paid_delivery_not_automatically_retried`；S::`test_request_budget_does_not_reset_and_unknown_requests_are_not_paid`；当前Skill固定浏览预算 | 旧自动分时截图获取强制由用户替代，不记通过。当前雪球日K访问依然必须有界，遇到登录／验证受阻保留unknown；不得无限刷新或绕验证。是否取得页面及实际失败次数需本次浏览记录，模型预算测试不算该证明。 |
| 39 | 五接口默认启用、实际证据、降级标识；分类资金不按名称合并 | S::`test_default6000_sources_all_enabled_no_independent_product_dependency`；D::`test_dc_partial_member_scope_not_complete_and_no_history_fabrication`；实际证据1、2 | 部分；五接口真实权限已探测，完整成员覆盖不足如实保留。DC/THS同名但不同分类／金额不能相加的独立反例，以及单源失败冻结degraded的集成测试待补。 |
| 40 | 多理由不再加同机构；none_disclosed与unknown分开 | R::`test_multi_reason_duplicate_is_one_event_without_readding_institutions`；R::`test_none_disclosed_distinct_from_unknown_institution_response`；R::`test_event_id_cannot_override_contradictory_stock_date_or_scope` | 离线映射；关联便利event_id不能越过错股票／错期间／错范围。 |

## 当前尚需的验收

当前第7步需根代理提供实际 `codex_browser_daily_v1` 记录：本人在雪球看到日K、实际URL／主体／日期／截止与浏览时点、支持与最强反证、同组比较、未决未知及固定预算下的访问结果。技能和合成案例只规定做法，不能证明已经执行；也不能声称此模式无人值守云端每日自动完成。

**20、28–34、38** 的旧截图／分时／外部视觉模型强制条件明确由最新用户指令替代，停止为这些已取消的强制项新增外部模型调用。相关一般真实性与时间边界继续按日线浏览验收。**35** 保留程序硬限制与新浏览记录校验；**36** 保留价格观察、不推成交或净收益的约束。

来源与完整集成仍需补 **8、9、10、39** 中表内标明的剩余子条件。15在当前“仅价格观察、无退出模拟”的范围内保留边界说明；若以后加交易模拟，另行验收T+1、退出失败、费用与无法成交。

后续验收更新具体函数名与执行记录；保留原失败和首次真实模型错误。不将历史重跑、合成反例、旧能力试验、日线浏览或价格观察改标为真实盘前盈利验证。
