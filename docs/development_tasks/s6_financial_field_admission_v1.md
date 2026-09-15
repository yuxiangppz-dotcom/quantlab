# S6-A v2 原始财务字段语义准入

- GitHub Issue：#161
- 父任务：#132（归属 #127）
- 前置：#159 / #160
- 起始 master：`e5d350829d2c5dafbdb0798837d524d85aea067e`
- 分支：`chatgpt/s6-financial-field-admission-v1`

## 目标

冻结财务原始字段进入 S6 后续公式层之前的 provider-neutral 语义和证据门槛。

## 包含

- 不可变字段定义和内容指纹；
- stock/flow、期间口径、合并范围、单位/币种、符号约定；
- verified、provider documented but unverified、unknown 三态证据；
- 确定性目录、证据计数和目录指纹；
- 财务版本所声明每个 raw field 的逐行审计；
- provider/source/statement 错配、缺失和弱证据的失败关闭；
- 财务版本证据与字段语义证据的联合准入；
- 纯合成测试和中文说明。

## 不包含

供应商调用、本地数据、Canonical 写入、财务数值、TTM、因子公式、阈值、模型、收益、组合、绩效、晋级、账户或订单。

## 验收

目录输入顺序不影响结果；构造器无法绕过排序、唯一性和计数不变量；所有声明字段均产生审计行；只有已验证且完整的字段与已验证版本组合才能整体准入；完整 CI 通过。
