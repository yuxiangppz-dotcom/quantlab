# Scout 免费云端操作说明

## 架构与费用

私有 GitHub Actions 的标准 Linux runner 运行固定 Scout 应用 SHA 和已验收 `9fe617e3cc9aa11ba3605e713814bd048661b9d6` 引擎；Cloudflare Workers Free/SQLite Durable Object 保存手机简洁报告、来源/模型请求归档、日任务及微信发送意图；Cloudflare Access 只允许本人登录；Server酱 Turbo 沿用已实收通道。电脑可以关机。

GitHub Free 私库标准 runner 2000分钟/月由账号共享。免费 Workers 每日100000请求，SQLite DO账号合计5GB；此程序另设逻辑归档1GB上限、单快照256MB/5000文件和单次远端8000请求上限。容量不足停止，保留旧档案，不自动购买、扩容或清理。逻辑字节统计不包括SQLite/索引及快照清单开销，应同时看平台实际占用。模型 API 仍收费，数据及微信额度按既有账号权益。

定时设为北京时间08:03/08:23/08:43。启动时先远端检查，再决定是否构建运行；同日只领取一次，领取/归档失败和中断未知均不重做付费任务。GitHub cron可能延迟或丢任务，不承诺08:00准点；09:00后不补启动。没有触发的任务也没有本流程发送的失败通知。免费workers.dev在大陆微信内的连通性须用户手机实测。

## 版本与权限

公开源码仓库不保存真实运行资料或凭据。创建本人拥有的**私有**运行仓库，只放 `runtime-workflow.yml`。GitHub变量 `SCOUT_APP_COMMIT` 是已提交、通过CI的完整40位应用 SHA，`SCOUT_PUBLIC_URL` 是部署后的 HTTPS origin。Secrets 是 `TUSHARE_TOKEN`、`DEEPSEEK_API_KEY`、`SCOUT_API_TOKEN`；没有模型密钥进入手机页面。

Cloudflare配置 `APP_COMMIT` 必须与GitHub变量一致、`SCHEDULE_ENABLED=false` 起步。Cloudflare secret `SCOUT_API_TOKEN` 与GitHub同名随机令牌一致；`SERVERCHAN_SENDKEY` 只在Cloudflare，GitHub runner不持有。Access配置 `ACCESS_ISSUER`（本人团队cloudflareaccess.com HTTPS origin）、`ACCESS_AUD`、`OWNER_EMAIL`。Worker自行核验Access JWT签名、issuer、audience、有效期和本人邮箱，缺任何配置时拒绝所有手机报告访问。

Access边缘策略：手机路径只允许本人邮箱，API路径可使用独立Service Auth策略；若整站边缘Access保护，runner须另有相应Service Token头配置，不能用绕过本人登录的公开名单。当前runner只使用Worker自身API bearer，部署时应给 `/api/*` 配置边缘Bypass（只略过边缘，Worker bearer仍强制），手机路径为本人Allow。不创建全站Bypass，不公开原始归档。

首次部署需要本人Cloudflare账号的最小范围Workers脚本部署权限及Access应用/策略配置权限。账号条款、验证码、新密码和付费套餐选择由本人处理。禁止Global API Key，禁止将令牌贴入聊天或命令参数/日志。部署令牌只保存在本机受限文件，不进入GitHub日常任务。

## 部署与验收

1. `deploy/scout_free` 执行 `npm ci`、`npm test`、`npm run build`，然后在本人Free账号部署 `wrangler.jsonc`。实际 `workers.dev` 子域名由账号确定，不能猜测。未启用预览链接，未开启请求内容日志。
2. 通过环境/CLI stdin设置上述Workers变量/Secrets，创建手机与API路径分开的Access策略，记录AUD和团队issuer；未完成前保持调度关闭。
3. 将固定私库workflow放在默认分支 `.github/workflows/scout.yml`。手动 `mode=check` 仅核对远端连接，无数据/模型/微信请求。
4. 用已验收成功run经 `cloud_artifacts.publish` 派生的 `metadata.json/report.html` 调 `/api/publish`，`import_only=true`。只导入手机简洁报告，不上传旧完整模型回复、旧canonical或凭据。该步骤保持调度关闭；旧报告仍以原行情/目标日显示。
5. 在真实公网匿名访问确认拦截；本人手机登录后查看候选和理由；核对版本、免费额度和微信接收通道后启用Workers `SCHEDULE_ENABLED=true`。首个交易日真实云端运行检查数据、生成、归档、微信实收、手机打开及重复触发不重复付费。节假日/保存报告回放不能代替真实交易日模型验收。

GitHub job最长65分钟，行情更新100请求/15分钟，模型最长30分钟/4请求/一次定向纠错及原token预算不变。每30秒追加归档检查点，最终归档完成后才发布、结束领取并发送微信。VM突然消失可能丢最后30秒内尚未传出的字节；远端领取/已有检查点保留为未知并阻止后续自动预测，不能假装保存了未收到的响应。

## 停止、故障与恢复

停止新预测：把Workers `SCHEDULE_ENABLED` 改为false，或禁用私库workflow。不能删除DO、日领取、outbox或清空归档来重跑。更新版本必须独立开发、通过全仓检查与真实运行门槛，再同时更新两个固定SHA；旧任务运行时不改代码或配置。

如果`previous_job_unresolved`、`archive_delivery_unknown_no_retry`或`archive_unresolved_claim_retained`，先检查私有Actions作业、DO已有领取和最新归档，不自动重新请求模型。当前没有自动解锁、删除或重试接口。确需恢复时，明确审计已有原响应、费用及完整报告后在独立开发/运维流程处理；不通过提高预算或抹去失败恢复。

现有本机服务、canonical、所有旧run和前瞻保持原位。云端选股仍是研究候选，收益/胜率待成熟前瞻，未声称盈利。

官方依据：[GitHub免费额度](https://docs.github.com/en/billing/concepts/product-billing/github-actions)、[定时限制](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[Workers价格](https://developers.cloudflare.com/workers/platform/pricing/)、[DO限制](https://developers.cloudflare.com/durable-objects/platform/limits/)、[Access JWT校验](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。
