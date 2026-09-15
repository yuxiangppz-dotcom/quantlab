# S6 市场观测记录与 PIT 选择契约

状态：无数值、仅证明来源时点的冻结边界（v1）

## 解决的问题

市场字段定义证明“这个字段是什么意思”，但不能证明某只股票某个交易日的记录在历史决策时点已经可见。本契约增加记录层证据，并把每条记录绑定到市场字段定义的精确指纹。

它与财务版本契约采用同一原则：未来可见的数据不能提前使用，事后修订只能从修订实际可用的时点开始替换原记录。

## 观测记录

每条记录包含：

- 股票、交易日、provider、source 与原始字段；
- 市场字段定义指纹；
- 修订 ID；
- `observed_at`：字段所描述的市场观察时点；
- `available_at`：来源侧可供消费的最早时点；
- `retrieved_at`：QuantLab 实际取得记录的时点；
- 内容指纹和证据状态。

三个时间必须为 UTC，且满足：

`observed_at <= available_at <= retrieved_at`

记录不包含数值，只保存可审计的身份与时点证据。因此它证明的是来源时间语义，不会虚构 QuantLab 在历史当日已经本地持有该数据。

## 证据状态

- `original_observation_verified`：原始观察版本已核验；
- `correction_chain_verified`：修订链已核验；
- `latest_only_unverified`：只能确认当前最终版本，历史版本不可证明；
- `unknown`：证据未知。

目录完整保留全部状态，但只有前两种可被 PIT 选择器选中。

## 按 as_of 选择

选择器要求一个已经语义准入的市场字段定义，并精确匹配股票、交易日、provider、source、raw field 和定义指纹。

结果分为：

- `admissible`：选择 `as_of` 前最后一个已核验版本；
- `definition_not_admissible`：市场字段定义本身未通过；
- `definition_mismatch`：目标记录存在，但绑定的是另一版定义；
- `not_yet_available`：匹配记录尚未到达来源可用时点；
- `unverified_observation`：当前可见的精确记录只有弱证据或 unknown；
- `missing`：没有目标记录。

修订版不会回填替换修订发布以前的历史选择。

## 明确不授权

本契约没有真实数值，不调用供应商、不写 Canonical、不运行公式、不排序、不回测，也不授予绩效、组合、风控、账户或订单权限。后续数值物化必须同时通过字段定义、观测记录、财务版本与公式输入绑定，并另行接受数据覆盖审计。
