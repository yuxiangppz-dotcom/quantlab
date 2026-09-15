# S6 公式输入语义绑定审计

状态：仅元数据的整合契约（v1）

## 作用

公式规范完整，不代表公式引用的字段已经可用。该审计把一条 S6 公式规范逐输入绑定到明确的 provider/source 财务字段目录，并将每种阻断原因单独保留。

## 财务字段绑定

对每个 `financial_field` 输入，审计在指定 provider/source 中按语义字段 ID 查找：

- 恰好一个已准入定义：输入绑定成功，并记录定义指纹；
- 没有定义：`financial_field_missing`；
- 有定义但没有任何已准入定义：`financial_field_unverified`；
- 多个已准入原始字段映射到同一语义字段：`financial_field_ambiguous`。

歧义不会通过排序挑选一个，也不会把供应商 A 的字段拿来满足供应商 B 的公式绑定。

## 派生指标与市场字段

`derived_metric` 必须存在于同一公式目录且自身已经准入，否则分别返回 missing 或 blocked。

`market_field` 当前统一返回 `market_input_contract_missing`。这是有意的阶段边界：EP、BM 等价值指标通常需要价格、股本或市值语义，在市场输入的价格基准、股本范围与 PIT 时间契约冻结之前，不允许猜测一个字段顶上。

## 总体准入

审计同时保留：

- `formula_admitted`：公式规范自身是否完整且已验证；
- `all_inputs_admitted`：每个有序输入是否都完成唯一绑定；
- `overall_admissible`：前两者同时为真。

因此，完整字段不能挽救未验证公式，完整公式也不能绕过缺失、弱证据或歧义字段。

审计不读取数值、不执行公式，也不授予因子计算、绩效、账户或订单权限。
