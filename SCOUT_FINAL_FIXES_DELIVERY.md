# Scout 最终修复交付

日期：2026-10-01（北京时间）。依据 `E:/浏览器下载/Scout_Codex_Final_Fixes_Taskbook.md`。起点 HEAD `bc5d38f06434c250248a048fd96dd48ee8fd7b01`，起始工作树干净且未发现并发写入者。工程与测试提交依次为 `1f81cadff9f19e3292069584b3d02ed0bae27dc3`、`a1933bb5d41e03b8c610798fea1fc19833cffbc5`、`67e9038deb5002542402bb86fd8b137ce85b199a`、`0cc57e331424c17aae09771b748d3125cec56d6d`，均已正常推送至草稿 PR #192。交付文档和状态更新随后单独提交；最终 HEAD 以 `git rev-parse HEAD` 和 PR 当前头为准。PR 保持草稿，不合并。

## 四项缺陷与验证

| 复核问题 | 修复 | 核验结果 |
| --- | --- | --- |
| 核心数字只按绝对值/字符串放行 | `facts.py` 为收益、成交额、供应商净流和连板数建立带股票主体、期间、带符号原值、单位、来源位置的程序事实；最终 packet 含事实表，校验事实表与当时可见候选值一致。新模型 schema 逐条声明 `quant_claims`，验证声明、原句与事实匹配。未能解析的新核心量化表述拒绝进入新版报告，原始文本仍归档。 | 合成 5 日 -12.34% 上涨、五日负净流说成净流入、昨日负收益称正涨幅、2 亿元说成 200000000 亿元、无事实的 20 板均拒绝；合法符号、期间、舍入和 2 亿元换算通过。`H5` 与观察 5 个交易日不当作连板/回购次数。 |
| 同行比较拿本股数值核验 | 核心断言按同句点名的主体取事实；同行可用于明确市场指标比较。公司订单只接受本股绑定的来源，同行公告不支持本股事件。 | 合成乙公司 -6.78% 的同行句可通过；写成本股跌幅会拒绝。 |
| attention 假设仅加标签、不入队 | 假设 attention 与外部关注列表去重并入同一路，仍按新增唯一股票分配名额；`热度观察` 与纯讨论/用户线索列为弱路线。 | 只靠讨论或用户线索的合法股票进入研究池；外部热榜重复计一次，池外代码被过滤；只有弱线索不能升重点。 |
| 涨停历史摘要截半条 JSON 且漏最近日 | 先按最近交易日选择完整记录，再序列化到原有正文和整体预算；公共单位说明只放一次，记录源 ID、源日期、实际展示日、预算省略日及省略源 ID。 | 五日合成记录的 9/30 优先保留，正文完整可解析；极小预算明确省略，元数据不假称原文已展示。旧 packet 原样保留。 |

主要代码：`src/quantlab/scout/facts.py`、`ai.py`、`pipeline.py`、`report.py`、`scripts/scout_revalidate.py`；回归在 `tests/scout/test_final_fixes.py`，两处旧模拟输出测试随新 schema 更新。新输出 schema/验证器版本为 v4；原始模型文字没有被改写。渲染报告会区分程序核对的核心数字和仍属模型推断的其余理由。

## 旧输出兼容与真实离线复核

源 run `third_round_runs/20261001T155126-df32c0d4` 的报告及 AI 响应与原 manifest 双哈希匹配，精确最终 packet 已存。当前代码在**无供应商、无模型调用**下形成最终派生 `final_fixes_revalidations/20261001T230128-40951441`；其自身双哈希亦匹配，记录了验证器提交 `67e9038deb5002542402bb86fd8b137ce85b199a` 和四个验证器文件 SHA-256。它保留 5 观察、剔除两只原始重点，剔除码均为 `unknown_provider_unit`。

旧模型输出没有新版 `quant_claims`，不能假装模型当时给了结构化声明；派生记 `compatibility=legacy_unstructured_model_output` 和 6 个旧文字未结构化核验片段，状态 `posthoc_engineering_audit`、`primary_eligible=false`。原始 5 股名单、精确旧 packet 和此前 20 条前瞻记录都没有被覆盖或重复计入。现有 20 行来自同一目标日的五只股票 × 四个期限，均未到期；不代表 20 次独立实验，也没有可计算净收益。

初次派生 `20261001T225451-8666a349` 曾因操作时误填不存在的提交号而留下错误出处，**不得引用其提交号或名单作结论**。随后添加了自动读取并校验真实 HEAD 的机制；中间派生 `20261001T225510-84def0c0`、`20261001T225843-cd8c13a6` 保留，最终以 `20261001T230128-40951441` 为本轮工程审计。上述文件未被覆盖，错误及修复均可追溯。

## 五只观察股与两只剔除重点

逐股原句、精确 packet 位置、证据 ID、符号/期间/单位、风险和兼容性边界见隔离目录 `final_fixes_audit/five_stock_evidence_audit.md`。五只均有 9/30 收盘价及已知涨停价：**4/5 收于涨停价，1/5 未涨停，未知 0；5/5 均非一价交易日**。襄阳轴承的少量减速器业务收入有当时可见官方节选，但短期业绩贡献和资金流构成反证；康辰药业说明会是日程而非新催化；传艺科技引用的是旧年度预告；深物业 A 的官方正文主要提示估值与信息风险；我爱我家 9/30 未涨停且历史三板不能说成当日连板。多项 `1/3/5 日净流` 的斜杠合写在旧输出中仍标未结构化核验，不借数字相同宣称语义完全通过。

滨江集团 `E[4]` 的 `fd_amount=120629190.0`、东风科技 `E[36]` 的 `fd_amount=32680616.0` 在当时可见供应商材料中都没有已核实金额单位，因此“约 1.21 亿元”“约 3268 万元”不能成立；东风科技另有“唯一正向盈利预告”的比较覆盖不足。两只不恢复重点。所有这些是行情日与当时输入的事实审查，不推断 10/8 实际可买或上涨。

## 可上传审计包与操作

隔离数据根目录为 `/home/administrator/projects/quantlab-pr189/data/scout_live_20260929`。新包为 `final_fixes_audit/Scout_Final_Fixes_Audit_20261001.zip`，SHA-256：`570fb07f8bbc7ea28c88de469ba617ddb4cf02839dd9463e5519fc89d79d6866`，大小 104512 字节，已复制并核验至 Windows 路径 `E:/浏览器下载/Scout_Final_Fixes_Audit_20261001.zip`。包内 `README.md` 与 `bundle_inventory.json` 列出 13 个文件及逐文件 SHA：五股审计、源精确最终 packet、原始模型可见输出、源/派生报告子集与原 manifest、来源节选及快照映射、旧前瞻快照。包不含密钥、环境变量或内部思维文本；子集文件的包内哈希与原 manifest 所指**完整**报告/AI 响应哈希是不同对象，不能混用。可直接上传此 ZIP 给 Astra；本轮未自动外发。

在 Scout 工作树用 WSL 执行离线检查：

```bash
uv run pytest -q
uv run ruff check .
uv lock --check
git diff --check

uv run python scripts/scout_revalidate.py \
  --source-run /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/third_round_runs/20261001T155126-df32c0d4 \
  --canonical-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/canonical \
  --output-dir /home/administrator/projects/quantlab-pr189/data/scout_live_20260929/final_fixes_revalidations
```

最后一条会新建一个不可覆盖派生目录，不应为提高名单重复运行。当前本地完整检查为 **148 passed**、Ruff 通过、`uv lock --check` 通过、`git diff --check` 通过；推送后远端 `quality` 检查对工程提交 `67e9038` 为 SUCCESS。文档收尾提交的远端结果另行核对。既有原始报告、模型输出、manifest、前瞻及 canonical 保持不变。

## 结论

四个定点缺陷的工程验收与离线证据复核已完成，建议冻结 Scout 此版本，等待原定目标日及期限成熟后的前瞻观察。尚未证明选股准确率或可信净收益优势；不开展新一轮付费运行、策略扩张或自动优化。本轮未调用新供应商数据/模型服务，未下单、未造假成交、未写原 canonical、未合并 PR。
