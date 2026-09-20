# 成员差异诊断 v6：修复与复现

本组起点为 `0003dd2ab8dea027f8318577690a15cec7df8847`。
这是离线诊断工具修复，不是历史成员认证，也不修改生产准入规则。

## 已复现并修复

旧脚本 `decompose_v4.py`（输出v5）将来源当作执行批次：同一事实两个来源、
或同来源完全重复，都会执行两次；同日相反事件的分类依赖来源名字母顺序。
旧测试只调用单批次函数，未覆盖上述完整路径，且包含常量True断言。

新的唯一入口为仓库版本管理下的 `scripts/audit_csi800_membership.py`：

- 去重与应用在同一完整路径完成，来源去重保留，但不增加执行次数。
- 按日期应用事件。同日不同证券互不依赖；同日同证券的相反动作或矛盾条款
  保留全部候选并阻断该窗口，不根据来源名称、端点或先卖后买猜测顺序。
- 所有窗口均应用已加载事件，包括两端观察相同的窗口。
  前置条件失败明确为invalid_events，不能凭端点吻合通过。
- 同日矛盾观察阻断整个推导，输出双方文件、哈希、名单及完整差异。
- 从同一份读取字节解析并计算输入哈希；保存116份观察和6份事实文件的绝对路径。
  脚本哈希、全部残差、事件来源和自动报告一起输出。已有输出目录拒绝覆盖。

此前隔离脚本、测试、v5产物均未改动；另保存修复前副本与反例结果。
同日先后关系即使将来取得证明，也需明确扩展输入语义后另行验证；当前工具不接受猜测。

## 运行顺序

在 `/home/administrator/projects/quantlab-pr189` 中运行，最后的输出目录必须不存在：

```sh
uv run pytest -q tests/pipeline/test_membership_audit.py
uv run python scripts/audit_csi800_membership.py \
  --observations data/provider_observations/csi800_weights \
  --facts-root /home/administrator/quantlab_evidence_continuation_20260919 \
  --intake-root /home/administrator/quantlab_evidence_intake_20260919 \
  --output /absolute/new-membership-audit-directory
```

本轮实际输出：
`/home/administrator/quantlab_evidence_continuation_20260919/membership/codex-repair-v6/result/`。
包含 `audit.json`、自动生成的 `report.md` 与 `artifact.sha256`。
父目录保存 `before.json`、`after.json`、旧文件副本、`validation.json`、
`targeted.log`、`pytest.log`、`run.log`。

输入改变后必须新建输出目录重新诊断，无resume或旧结果复用入口。
此工具没有写入universe、canonical、账户或生产配置的路径。

## 本轮真实数据结果与限度

115个窗口：88个无已加载事件且观察端点相同，19个端点干净匹配，
7个有观察变化但没有已加载事件，1个应用事件后端点冲突。
这与v5统计相同，修复解决的是潜在错误路径，不是新增外部证据。

7个缺已加载事件的窗口截止日为2017-02-28、2017-06-30、2017-12-29、
2018-06-29、2018-12-28、2019-06-28、2026-01-30。
2023-12-29残差为加入302132.SZ、移除300114.SZ，身份及时间仍待核验。
2020年6月和12月均匹配，不列为缺口。
没有已加载事件只说明本次证据集合缺项，不足以断言外部不存在公告。

起始锚点、月内事件完整性、历史公开时间、ST覆盖、公司行动和退市结算
仍是独立门槛。没有训练、回放、新收益或alpha结论，PR不合并。

## 验证

16项针对测试通过，覆盖完整去重应用路径、来源名称与输入顺序置换、
不同日期合法往返、同日相反事实、条款矛盾、端点相同但事件非法、
35/40个完整残差、实际parquet冲突经CLI阻断与拒绝覆盖、
真实parquet内容变化的输入哈希、事实原始字节绑定及报告生成。
`validation.json`另核验脚本、116份观察、6份事实哈希均一致，旧文件未变。
全仓3707 passed、11 skipped、56 warnings，92.95秒；ruff与diff-check通过。
实际日志见同目录pytest.log。
