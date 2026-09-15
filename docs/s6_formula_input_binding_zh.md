# S6 公式输入语义绑定审计

状态：仅元数据的整合契约（v2）

## 作用

公式规范完整，不代表公式引用的字段已经可用。该审计把一条 S6 公式规范逐输入绑定到指定 provider/source 的财务字段目录与市场输入目录，并将每种阻断原因单独保留。

## 财务字段绑定

对每个 `financial_field` 输入，审计在指定 provider/source 中按语义字段 ID 查找：

- 恰好一个已准入定义：输入绑定成功，并记录定义指纹；
- 没有定义：`financial_field_missing`；
- 有定义但没有任何已准入定义：`financial_field_unverified`；
- 多个已准入原始字段映射到同一语义字段：`financial_field_ambiguous`。

歧义不会通过排序挑选一个，也不会跨 provider/source 借用定义。

## 市场字段绑定

调用方显式提供市场输入目录后，`market_field` 使用相同的精确唯一原则：

- 恰好一个日频 PIT 定义准入：绑定定义指纹；
- 当前 provider/source 没有该语义字段：`market_field_missing`；
- 有定义但价格基准、股本范围、时点、公司行动或证据不满足准入：`market_field_unverified`；
- 多个已准入原始字段映射到同一语义字段：`market_field_ambiguous`。

为保持调用兼容，如果调用方完全没有提供市场目录，仍返回 `market_input_contract_missing`，不会把空目录和“没有传契约”混成同一状态。审计本身保存市场目录指纹，使一次绑定结果可重放。

## 派生指标

`derived_metric` 必须存在于同一公式目录且自身已准入；它会递归复用同一财务目录、市场目录和 provider/source 上下文。底层市场输入阻断会使派生指标返回 blocked，不能在依赖层被绕过。

## 总体准入

审计同时保留：

- `formula_admitted`：公式规范自身是否完整且已验证；
- `all_inputs_admitted`：每个有序输入是否都完成唯一绑定；
- `overall_admissible`：前两者同时为真。

因此，完整字段不能挽救未验证公式，完整公式也不能绕过缺失、弱证据或歧义字段。

审计不读取数值、不执行公式，也不授予因子计算、绩效、风控、账户或订单权限。
