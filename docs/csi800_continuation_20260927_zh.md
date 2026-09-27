# 历史中证800续作：行业可得时间与股息补税

本轮起点 `ba79d81`，工作区 `/home/administrator/projects/quantlab-pr189`。
WSL Git 视角干净、与远端主线一致，PR #190 开放；旧 detached review 工作区的
文件模式差异未触碰。开始时未见旧 executor/reviewer 进程。

## 行业时间仍未认证

当前申万文件 `data/evidence/industry_label_repaired_20260924/industry_intervals.json`
有 4107 条区间，`known_at` 仍为 `null`。2018-01-02 首个成员
`000001.SZ` 在 universe 仍报 `historical publication unknown`。
申万官网当前“下载全部成分股”页面是**当前下载快照**；申万 2021 版公告只确认
新旧分类的发布及切换边界。公开搜索本轮检索了 2017/2018 行业指数成分调整、
申万行业分类历史发布和当期留存附件，未取得覆盖 2018-01-02 全部当时成员的
带发布日期原始名单。这里没有给任何旧区间补 `known_at`，也没有把证监会行业
分类替换为策略中已冻结的申万约束。

每条行业归属将分别核对：分类生效日、当时公开日、供应商修订日和本次下载日。
生效日只证明内容适用期，当前下载时间只证明本次取得。供应商历史表没有逐次
修订快照时，修订日保持未知。可追溯公告的发布时间也只认证公告实际列出的
证券及区间，不能推及全表。旧版行业文件和阻断回执保留。

## 300116 转增与股息补税

新下载的发行人原公告、财政部两份税务规则保存在
`data/evidence/public_rules_v2_20260927/`，每份的 URL、获取 UTC 时间、字节数和
SHA256 见该目录 `receipt.json`。`config/csi800_public_rules_20260927.json` 和
`scripts/intake_csi800_public_rules.py` 可以在新目录重取，旧 v1 下载未覆盖。

发行人 2020-03-25 的 [实施公告](https://static.cninfo.com.cn/finalpage/2020-03-25/1207404907.PDF)
说明供应商所报每股转增 0.85 是**总股本转增**，普通合格原股东仅每10股获配
0.5股；李瑶、郭鸿宝和宁波坚瑞新能源不在普通获配对象中；登记日为
2020-03-31，股票不除权，转增股先登记到管理人专户再过户。此前封存的
2020-05-14/06-05 后续公告还显示普通账户和信用担保账户到账不同步。
因此 2020-04-01 的供应商 `ex_date` 和 0.85 不能转成统一可执行到账事件，
v18 的这条 unresolved 保留。

[财税〔2012〕85号](https://m.mof.gov.cn/zcfb/201211/t20121116_697495.htm)
规定证券账户先进先出与自然月/年持有期；
[财税〔2015〕101号](https://www.mof.gov.cn/gkml/caizhengwengao/wg2015/201510wg/201602/t20160202_1662802.htm)
对 2015-09-08 后登记的个人 A 股普通流通股适用卖出时 20%/10%/0% 的
股息红利税。新增可审计的**显式声明**路径：只有原公告和账户类型已证明
税前总额等于当时到账额、且发放时未预扣税时，事件才应声明
`tax_treatment=individual_a_share_2015`。读取器还要求事件的来源 URL、SHA256
与公司行动产物旁的原始文件逐字节相符；哈希绑定本身仍需人工核实公告确实
证明该账户类型与到账金额。记录日绑定原始买入批次；实际卖出时
按账本成交股数扣收，支付日不清掉未卖股的税务追踪。同一批次跨多次分红均计税。
补税单列为 `realized_dividend_tax_fen` 与公司行动流水，不混进交易佣金。
送转改变旧股税务归属而缺少登记结算分配规则时继续阻断。

旧公司行动文件 12533 条事件、15 条 unresolved，仍只含供应商“税后”每股值，
**没有**新增税务认证。市场输入现在对成员/持仓范围内未认证的现金事件
明确报 `corporate_cash_tax_basis_unverified`，不让旧净额账本在行业门槛解除后
误入完整实验。这意味着真实数据准入仍未通过，Ridge、LightGBM 和固定因子基线
均未重跑；旧 2023—2025 输出不能当新留出期或新净收益结论。

免费 [BaoStock 行业接口](https://github.com/zxygithub/baostock/blob/master/docs/data_download_plan.md)
可用于独立核对分类标签或发现缺行；它的当前查询结果同样不能单独证明
2018 年的首次公开时间。发行人公告与申万当期留存文件仍是时间认证主依据。

## 复现与下游失效

先核验 `receipt.json` 中 manifest、脚本及原始文件哈希；然后执行
`uv run pytest -q tests/research/test_dividend_tax.py tests/research/test_ml_v2.py tests/pipeline/test_csi800.py`。
真实行业公告逐条核验并生成**新版本** industry 输入后，按
`docs/csi800_input_dependencies.md` 从 universe→bundle→训练→market→replay→report
重建；公司行动文件更改则至少重建 market→replay→report。旧目录的 resume
应因输入或代码哈希变化失败，不在原位置覆盖。当前应运行 universe 并记录
2018-01-02 的实际阻断；不要用缺证分类或删除证券换取完整曲线。
