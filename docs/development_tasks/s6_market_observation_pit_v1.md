# S6-A v7 市场观测记录与 PIT 选择

- GitHub Issue：#171
- 父任务：#132（归属 #127）
- 前置：#167/#168、#169/#170
- 起始 master：`3672b790aff2ee865f74f9b39fb55e302f786a16`
- 分支：`chatgpt/s6-market-observation-pit-v1`

## 目标

冻结市场观测记录的来源可用时点和修订选择规则，使通过语义绑定的市场字段不能使用未来发布、弱证据或另一版定义的数据。

## 包含

- 股票/交易日/provider/source/raw field 身份；
- 市场字段定义指纹与修订 ID；
- observed/available/retrieved UTC 时间链；
- original/correction/latest-only/unknown 证据状态；
- 确定性目录、证据计数和内容指纹；
- 按 `as_of` 选择最后一个可见且已核验的精确版本；
- 定义未准入、定义错配、尚未可用、未验证和缺失 verdict；
- 构造器不变量与纯合成测试。

## 不包含

任何数值、供应商调用、Canonical 写入、公式执行、排序、回测、绩效、组合、风控变更、晋级、账户或订单。

## 验收

选择不能跨定义指纹，不能早于 `available_at`，不能使用 latest-only 或 unknown；核验修订仅在其可用时点后替换原始版本；直接构造不能绕过身份、时间、排序和汇总状态；完整 CI 通过。
