# Scout 免费云端操作说明

## 架构与费用

私有 GitHub Actions 的标准 Linux runner 运行固定 Scout 应用 SHA 和已验收 `9fe617e3cc9aa11ba3605e713814bd048661b9d6` 引擎；Cloudflare Workers Free/SQLite Durable Object 保存手机简洁报告、来源/模型请求归档、日任务及微信发送意图；Scout 独立查看密码保护报告（无需开通 Access 的绑卡流程）；Server酱 Turbo 沿用已实收通道。电脑可以关机。

GitHub Free 私库标准 runner 2000分钟/月由账号共享。免费 Workers 每日100000请求，SQLite DO账号合计5GB；此程序另设逻辑归档1GB上限、单快照256MB/5000文件和单次远端8000请求上限。容量不足停止，保留旧档案，不自动购买、扩容或清理。逻辑字节统计不包括SQLite/索引及快照清单开销，应同时看平台实际占用。模型 API 仍收费，数据及微信额度按既有账号权益。

定时设为北京时间08:03/08:23/08:43。启动时先远端检查，再决定是否构建运行；同日只领取一次，领取/归档失败和中断未知均不重做付费任务。GitHub cron可能延迟或丢任务，不承诺08:00准点；09:00后不补启动。没有触发的任务也没有本流程发送的失败通知。免费workers.dev在大陆微信内的连通性须用户手机实测。

## 手机实际访问与微信正文

用户已实收云端微信通知，但手机网络无法访问当前workers.dev。日常微信消息详情现在直接附完整简洁Markdown：目标日、行情截至日、选中股票及理由/风险/失效条件。手机可在Server酱消息详情阅读正文，网站继续提供密码保护历史报告。免费通道卡片可能只显示标题，应点开消息详情；平台正文保留时长按账号权益，历史仍以云端私密档案为准。不能把平台接受等同于手机可读，需真实手机核对。

`/api/publish`可附`markdown`和`markdown_sha256`：最多16000字符/60000字节，程序计算哈希，DO独立不可覆盖伴随记录保存，原HTML记录不改。微信发送必须在报告保存后，原未知发送不重发。`/api/push-test`仅调度关闭且无活跃任务时每封版一次；`/api/push-health`仅独立API认证可访问的固定无key只读探针。

## 版本与权限

公开源码仓库不保存真实运行资料或凭据。创建本人拥有的**私有**运行仓库，只放 `runtime-workflow.yml`。GitHub变量 `SCOUT_APP_COMMIT` 是已提交、通过CI的完整40位应用 SHA，`SCOUT_PUBLIC_URL` 是部署后的 HTTPS origin。Secrets 是 `TUSHARE_TOKEN`、`DEEPSEEK_API_KEY`、`SCOUT_API_TOKEN`；没有模型密钥进入手机页面。

Cloudflare配置 `APP_COMMIT` 必须与GitHub变量一致、`SCHEDULE_ENABLED=false` 起步、`AUTH_MODE=password`。Cloudflare secret `SCOUT_API_TOKEN` 与GitHub同名随机令牌一致；`SERVERCHAN_SENDKEY` 只在Cloudflare，GitHub runner不持有。

手机查看密码随机生成，仅在本机受限交付文件保存明文。Cloudflare只保存 `VIEWER_PASSWORD_HASH`（PBKDF2 SHA256、100000轮、16字节随机盐）与独立 `SESSION_SECRET`（至少48字符）。慢密码校验在DO中执行；每IP每15分钟最多5次尝试。手机使用12小时签名Cookie，Secure/HttpOnly/SameSite=Lax；登录POST核对同源Origin，输入有界，跳转仅限站内报告。API必须独立bearer，手机Cookie不能启动研究任务或读取原始档案。

用户本人选择独立密码方案：实际Access Free开通页要求银行卡、地址及超额扣费授权，未提交开通。可选Access认证代码仍保留，但本次部署不使用Access，不需要相关边缘Bypass策略。首次部署仅需本人账户Workers脚本权限；部署令牌只保存在本机受限文件，不进入GitHub日常任务，禁止Global API Key。

## 部署与验收

1. `deploy/scout_free` 执行 `npm ci`、`npm test`、`npm run build`，然后在本人Free账号部署 `wrangler.jsonc`。实际 `workers.dev` 子域名由账号确定，不能猜测。未启用预览链接，未开启请求内容日志。
2. 通过环境/CLI stdin设置上述Workers变量/Secrets；密码/会话密钥与运行API令牌分别配置；未完成前保持调度关闭。
3. 将固定私库workflow放在默认分支 `.github/workflows/scout.yml`。手动 `mode=check` 仅核对远端连接，无数据/模型/微信请求。
4. 用已验收成功run经 `cloud_artifacts.publish` 派生的 `metadata.json/report.html` 调 `/api/publish`，`import_only=true`。只导入手机简洁报告，不上传旧完整模型回复、旧canonical或凭据。该步骤保持调度关闭；旧报告仍以原行情/目标日显示。
5. 调 `/api/notify-import` 发送一次已保存报告的上线链接，持久去重且明确标注不是新预测；仅在调度关闭且无活跃任务时允许。平台接受不等于手机实收。
6. 在真实公网匿名访问确认跳转登录、无API令牌时拒绝API；本人登录后核对报告、候选和理由；核对版本、免费额度和微信接收通道后启用Workers `SCHEDULE_ENABLED=true`。首个交易日真实云端运行检查数据、生成、归档、微信实收、手机打开及重复触发不重复付费。节假日/保存报告回放不能代替真实交易日模型验收。

GitHub job最长65分钟，行情更新100请求/15分钟，模型最长30分钟/4请求/一次定向纠错及原token预算不变。每30秒追加归档检查点，最终归档完成后才发布、结束领取并发送微信。VM突然消失可能丢最后30秒内尚未传出的字节；远端领取/已有检查点保留为未知并阻止后续自动预测，不能假装保存了未收到的响应。

## 停止、故障与恢复

停止新预测：把Workers `SCHEDULE_ENABLED` 改为false，或禁用私库workflow。不能删除DO、日领取、outbox或清空归档来重跑。更新版本必须独立开发、通过全仓检查与真实运行门槛，再同时更新两个固定SHA；旧任务运行时不改代码或配置。

如果`previous_job_unresolved`、`archive_delivery_unknown_no_retry`或`archive_unresolved_claim_retained`，先检查私有Actions作业、DO已有领取和最新归档，不自动重新请求模型。当前没有自动解锁、删除或重试接口。确需恢复时，明确审计已有原响应、费用及完整报告后在独立开发/运维流程处理；不通过提高预算或抹去失败恢复。

现有本机服务、canonical、所有旧run和前瞻保持原位。云端选股仍是研究候选，收益/胜率待成熟前瞻，未声称盈利。

官方依据：[GitHub免费额度](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[定时限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[Workers价格](https://developers.cloudflare.com/workers/platform/pricing/)、[DO限制](https://developers.cloudflare.com/durable-objects/platform/limits/)、[Access JWT校验](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。
