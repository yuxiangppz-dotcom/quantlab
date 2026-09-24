# CSI800 主线输入依赖与失效链（2026-09-18）

证据 → universe → bundle → 训练/预测 → market → replay → report 各步骤的
输入绑定与失效规则如下。规则：输出目录存在时，resume 只在 intent 中记录的
输入/代码哈希与当前完全一致时继续；任何绑定输入变化都会使旧输出失效，
必须新建输出（`research_output` 同时拒绝覆盖已完成产物）。

| 步骤 | 输入（哈希绑定） | 变化后的失效范围 |
|---|---|---|
| universe（compile_universe） | membership/industries/event_coverage 的 sha256、availability、calendar、securities、code_changes、每个会话回执哈希 | 行业/成员/ST/可得性任一变化 → 该 universe 输出 resume 失败，需新目录重建 |
| bundle（build） | context（universe 输出的 context.parquet）、availability、calendar、securities、code_changes + 各会话回执 | universe 重建 → 旧 bundle 不自动更新；需新 bundle 目录全量重建 |
| training | bundle manifest、config、代码 identity、study 绑定 | bundle 重建 → **必须重训**（industry 进入 features.parquet 的 context 列，属于训练输入） |
| market（prepare_market） | execution_policy 与 corporate_actions 的 sha256、bundle manifest、会话回执聚合摘要、代码 identity、benchmark 会话 | 费用政策/公司行动/行业（经由 bundle）任一变化 → market 失效重建 |
| replay | training 输出（scores/completed 哈希）、market 三件、capital、config、代码 identity | 以上任一变化 → replay 失效重放 |
| report | replay、benchmark、training、exposures、baseline/momentum/replay 产物哈希 | 任一上游变化 → report 失效重出 |

结论示例：
- 仅费用政策变化：market→replay→report 重跑；**universe/bundle/training 无需重跑**
  （不绑定 execution_policy）。如果同时改代码，代码 identity 也会变化，不能以
  “仅改费率”为由绕过训练等阶段各自的 identity 检查。
- 行业或成员证据变化：universe→bundle→**重训**→market→replay→report 全链重建。
- 公司行动证据变化：market→replay→report 全链重建；不得在旧 market
  或 replay 上替换 JSON 后继续 resume。
- 688088 路径实例：2023-06 的旧 bundle 中其行业为空（当时缺口未补）；新行业
  文件不会自动进入旧 bundle。离线重建存在该日期的 bak_basic 快照区间，
  但成员认证仍阻断新 universe，不能称新 universe 已产生；新实验不能复用旧 bundle。

历史成员与 ST 证据的准入阻断（未解决，保持阻断）：
- 成员：半年度规则的通用生效日不能证明具体某月名单即为生效日名单；需要
  逐次调样的公告时间+名单+生效时间三件套并绑定身份。临时调整目前仅有
  观察界定（月度截面粒度）。
- ST：2354 会话原始响应与 Canonical 一致只排除传输/转换问题；异常状态
  翻转与独立历史来源交叉核验尚未完成；105 个抽验不构成全集认证。
- 可得时间：known_at 为声明界；历史公开时间/供应商修订时间/本次下载时间
  三者未分离认证；`historical_publication_certified` 保持 false。


## 2026-09-19 接手修复后的证据重建

此次范围是行业缺口/冲突和真实生成路径验证，未完成成员、ST、税务或退市补证。
行业按声明的 `[in_date, out_date)` 约定转换；退出日包含性及历史公开时间仍未认证。
入退同日为空区间，只记 issue、不产生覆盖；无效日期或倒序边界拒绝生成。
未来才开始的开区间不影响此前窗口；空快照标签不能字符串化为确定行业。
不同标签同时有效时生成 `industry: null` 并保留冲突记录；未知区间不会被
bak_basic 后备来源覆盖。缺少观测的开市日、日历本身缺日期都不能拼接；
只有两交易所明确一致的休市日可以连接两端同标签观测。合成测试已经通过
真实缓存→生成器→产物→universe 编译器验证冲突阻断。

以下命令只读取已有缓存，不请求供应商，不替换当前配置引用的旧证据。
将 `NEW_EVIDENCE_DIR` 替换成未使用的绝对目录（两个命令可以共用该目录）：

```bash
uv run python scripts/build_csi800_evidence.py --project config/project.local.json industries --output-dir NEW_EVIDENCE_DIR
uv run python scripts/build_csi800_evidence.py --project config/project.local.json corporate-actions --output-dir NEW_EVIDENCE_DIR
```

缺少 SW/L1 分类缓存会阻断，不能隐式联网；`--fetch` 才启用现有供应商请求路径。
L1 分类归档校验响应哈希；receipt 记录缓存、日历、观察文件及转换代码的哈希，
并明确 `historical_publication_certified=false`。哈希证明绑定的本地输入未变，
不能证明供应商历史完整或当时已公开。所有行业 issue 完整保留，不再截取前60条。

最终离线重建有4106个行业区间，其中106个未知区间、涉及66只证券。
公司行动12555个事件、42条 unresolved（14 missing_ex_date、26 conflicting_duplicate、
2 missing_div_listdate）；这不是“两条 missing_pay_date”。2条缺股份上市日分别为
002309.SZ / 2024-12-24、300116.SZ / 2020-04-01。记录数量不等于受影响证券数。
这些缺口未解除，不将旧运行收益重新纳入验收。

2026-09-23 本地输入更正：上面的12555是旧产物计数，不应再作为当前 v3 输入。
旧 `data/evidence/corporate_actions.json` 的22个冲突组仍带有一条可执行候选，
且旧回执未绑定生成代码/输入哈希。按已修复的生成器离线重建到
`data/evidence/corporate_rebuild_20260923/` 后是12533个事件、42条 unresolved；
没有新增候选，22条冲突候选全部撤下。新产物 SHA256 为
`0c7bd55072b6f66689c992580ef49a2f754bb22206bafb5a55d33c5242fb0a70`，
回执的1708个输入哈希均经重新校验。本机 `config/project.local.json` 已指向
该新文件；旧产物及旧 workspace 保留但不得当作修复后的运行证据。
在其他机器上应先用 `corporate-actions --output-dir <新目录>` 离线重建并核验，
再将项目配置指向其产物。文件路径和哈希的匹配仍不构成特殊重整事件、
持有期税或历史公开时间认证。
读取器现会拒绝任何同时含有冲突组 unresolved 与该组可执行事件的旧产物，
避免配置误指向旧文件时静默进入市场或回放输入。

月度成员观察中的后继证券代码，若出现在已核实改码生效日前，证据构建时按
`config/security_code_changes.csv` 还原为当时代码；原始观察不改写，
成员回执绑定该表哈希。这只修正证券身份表示，不把月末观察认证为逐日成员名单。
公司行动事件和行业区间也按同一生效日裁剪代码生命周期；供应商在新代码下
返回的改码前历史分红不再与旧代码重复进入账本输入。

顺序仍是：保全旧证据→隔离离线重建→补齐成员/ST等准入证据→新目录 universe→
build→train→market/replay→report。当前只完成前两步和合成路径验证；
禁止为得到完整曲线跳过 universe 的成员认证阻断。市场缓存回归测试分别验证
相同输入可复用、代码 identity 改变拒绝复用、会话回执改变拒绝复用。

## 2026-09-23 四笔转增股来源校正

`scripts/reconcile_csi800_share_conversions.py` 只合并发行人实施公告已核对、
且供应商多个“实施”行仅在空现金与零现金表示上不同的四笔普通资本公积转增：
000528.SZ/2018-10-26、000939.SZ/2017-06-20、688516.SH/2022-11-22、
688516.SH/2023-11-17。输入为当前代码生命周期修正后的公司行动文件、
`reviewed-v4/reviewed_facts.json`、四份发行人 PDF 及其来源元数据、原始
dividend parquet。脚本逐笔比对比例、登记日、除权日、股份上市日、现金腿及
PDF SHA256，产出新文件与回执；旧文件不覆盖。其余现金分红合并、特殊受益人、
税费与缺日期事件保持 unresolved。新文件从 12528 事件/42 unresolved 变为
12532 事件/38 unresolved。

股份到账只在账户权益数乘以转增比例为整数时执行。尾股如何按小数尾数与
登记结算规则分配，无法仅凭单个账户重现；遇到非整数权益即阻断回放，
不再向下取整并丢弃。此修复不认证成员/ST/行业输入，不会使 universe 自动放行。

## Public-source supplement (2026-09-19)

See [the source intake and remaining gates](csi800_public_evidence_intake_20260919_zh.md).
The isolated fact register is not an executable evidence bundle and must not bypass
membership, ST, industry or corporate-action gates. The 300114→302132 mapping now
has an issuer-announcement source; its mapping and effective date are unchanged.
