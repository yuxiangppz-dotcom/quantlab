# S5-B 内容寻址诊断交付包

`build_s5_base_diagnostic_bundle` 不访问文件系统。它接收 typed metrics、单次
run seal 和中文 review，在内存中生成三个固定文件：

- `s5b_metrics.json`：完整机器可读指标；
- `s5b_run_seal.json`：单次运行证据封印；
- `s5b_review.md`：中文只读审阅报告。

JSON 固定使用 UTF-8、键排序、紧凑分隔符、原始 Unicode、禁止 NaN，并且只保留
一个结尾换行。每个文件记录 media type、UTF-8 字节数与 SHA-256；bundle
fingerprint 绑定文件清单、逐文件哈希以及协议、输入、指标、seal、review 身份。

构建时会从 metrics 和 seal 独立重建 review，避免调用方换掉展示文本。验证函数会
重新检查文件顺序、media type、字节数、SHA-256、JSON 可解析性、规范化编码、
内嵌 fingerprint 与 Markdown content fingerprint。以后本地 runner 应当先验证
bundle，再用独立的原子写入流程保存三个文件。

SHA-256 与 fingerprint 在这里用于内容寻址和篡改检测，不表示数字签名、第三方见证
或真实历史收益认证。bundle 继续保持仅历史诊断、非执行 PnL、未冻结持有期、
无绩效结论、无晋级、无账户变更和无券商下单权限。
