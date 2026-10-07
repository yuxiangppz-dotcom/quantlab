# Scout 云端运行、微信推送和手机报告

## 交付范围

电脑可以关机，云服务器独立运行。默认每个交易日北京时间 **08:00** 开始，以最近已完成的交易日行情生成当天盘前研究报告；09:00 后不补启动预测。非交易日不调用模型。一次模型流程仍最多四请求、一次纠错及原固定 token/超时预算，未增加策略。

手机打开 HTTPS 网站，输入查看密码后查看历史报告和个股卡。报告仍只有候选、理由、风险、失效条件和日期。手机网页没有预测执行接口。旧本机入口、报告、冻结和前瞻产物保留。

推送默认使用 **Server酱 Turbo 的微信通道**：通知只包含报告目标日、行情日和登录后的报告链接，不向推送平台发送完整股票分析、API Key 或查看密码。需要先在 Server酱账户配置并绑定微信接收通道。Server酱³使用自己的 App，不能把它的 sctp Key 当作 Turbo 的 SCT Key。

“通知平台接受”与“手机确实收到”分别验收；程序不会把 code=0 称作微信送达成功。网络超时或服务端异常可能已发送，保留 delivery_unknown，不自动重发。

生成失败也发送一次“未发布新候选”通知，附已保存报告首页链接。该提醒不携带错误原文、旧名单或伪造的新报告。

## 部署所需

- 可安装 Docker Engine/Compose 的 Linux 云服务器，建议从 2 核/4GB 内存的配置验收；实际容量仍需观测。
- 一个解析到服务器的域名及可访问的 80/443 端口。Caddy 自动申请/续期 TLS；8080 不向公网开放。
- 服务器上的 TuShare/DeepSeek 凭据，以及 Server酱 Turbo SendKey 和已绑定的微信通道。
- 服务器时间正确。调度始终用 Asia/Shanghai，不依赖服务器系统时区。

凭据只在服务器上的受限环境文件中，不能通过聊天、Git、Docker 构建参数或镜像保存。当前代码仓库是公开仓库，真实数据和凭据尤其不能提交。

## 生成部署包（开发机）

先提交并推送完整代码，保持工作树清洁，然后：

```bash
uv run python scripts/scout_cloud_bundle.py --output /tmp/scout-cloud-release
```

WSL 管理工作树如 Git 指针是 Windows 路径，可增加 `--git-dir` 指向正确的 WSL Git 工作树元数据目录。安装器只从 Git 归档打包，不带 .venv、原行情、模型原档或私密凭据。包内 `bundle.json` 固定应用 SHA、已验收引擎 `9fe617e3cc9aa11ba3605e713814bd048661b9d6` 及逐文件哈希。不得手改 app/engine 或切换默认配置；修复须重新提交、打包、验证。

把这个部署包上传到用户明确指定的服务器目录。上传旧行情、模型原档或本机凭据须另行明确指定范围；本任务不自动上传它们。

## 服务器安装

以下命令在部署包根目录执行。Docker、域名解析和防火墙先按所在云平台完成；本脚本不购买资源、开账户或改防火墙。

```bash
cp cloud.env.example cloud.env
chmod 600 cloud.env
# 编辑 cloud.env：设置 SCOUT_DOMAIN、独立镜像名、两个 API Key、SCT SendKey。
# SCOUT_SCHEDULE_ENABLED 首次保持 false。
docker build -t quantlab-scout:cloud-v1 .

# 创建手机密码和独立会话签名密钥；交互输入，不把密码放进命令行。
docker run --rm -it --user 0 --volume "$PWD:/setup" \
  --entrypoint /app/.venv/bin/python quantlab-scout:cloud-v1 \
  -m quantlab.scout.cloud_web --create-web-secrets /setup/web-secrets.env
chmod 600 web-secrets.env

mkdir -p data/scout
sudo chown 1000:1000 data/scout
chmod 700 data/scout
docker compose --env-file cloud.env config --quiet
docker compose --env-file cloud.env up -d
```

以上镜像名须与 cloud.env 中实际 `SCOUT_IMAGE` 一致；建议使用带本次应用 SHA 的唯一标签，并记录实际镜像 ID。先构建镜像和生成 web-secrets.env，再启动 Compose。生成器独占创建文件，不覆盖原密码。本机也可直接使用 `uv run python -m quantlab.scout.cloud_web --create-web-secrets web-secrets.env`，之后只上传该明确指定文件到自己的服务器。

初始化成功后手机打开 `https://你的域名/`，登录确认空报告页或已导入报告。确认模型、来源、预算、域名、微信接收账户后，把 cloud.env 的 `SCOUT_SCHEDULE_ENABLED` 改为 `true`，重建 scheduler 容器配置：

```bash
docker compose --env-file cloud.env up -d --force-recreate scheduler
```

08:00–09:00 内进程启动可补执行本交易日尚未登记的任务；当日已登记、失败或送达未知均不重新执行。首次运行自动获取 SSE 日历、股票主表及必要的最近 21 日行情/复权因子，之后只补缺失分区与当前日主表；本股事实单位按官方接口转换。接口分页最多三页，忽略分页/重复页、日期串分区、非有限值、缺因子或缺交易日历都拦截。行情更新最多 100 个新请求、15 分钟，公开异常内容不写日志；原始返回保存于私有持久卷。

云端 TuShare 传输只把锁定 SDK 的已知官方 HTTP 地址升为同主机 HTTPS，拒绝未知地址，不回退明文。真实账号/部署地区的 HTTPS 可用性必须在首次云端验收核对；网络不通时停止，不能绕过证书或私自换代理。

## 持久化、查看与停止

`data/scout` 是独立、带标记的数据卷，不能指向原 canonical。卷内 market 是隔离行情；runs 是原实验报告；runtime 保存引擎任务与请求日志；reports 保存简洁手机视图；source_updates 保存来源原始返回；schedule/outbox 保存预测和通知幂等记录。手机只读 reports，不能浏览模型日志或任意文件。完整实验审计仍在 runs。

```bash
docker compose --env-file cloud.env ps
docker compose --env-file cloud.env logs --tail=50 scheduler
# 暂停后续日常预测：先把 cloud.env 的 SCOUT_SCHEDULE_ENABLED 改为 false，再重建 scheduler。
docker compose --env-file cloud.env up -d --force-recreate scheduler
```

不要运行 `down -v`、删除 schedule/runtime/outbox 或清空数据卷来重试。升级前若已有活跃任务，先等待完成；终止中断状态必须人工核对，不能补发未知模型请求。报告和 outbox 不因重启消失。只备份受限数据卷与密钥文件到用户明确授权的位置。

若完整预测已保存，但进程在发布之前中断，核对该 run 的 manifest、完成状态和请求记录后，可只补发布已有报告。下面示例路径必须替换成已核验的真实 run；不会调用模型，也不删除失败记录：

```bash
docker compose --env-file cloud.env exec scheduler /app/.venv/bin/python \
  -m quantlab.scout.cloud_runner --publish-run /data/scout/runs/已核验run_id
# 需要推送同一份已有报告时使用 --notify-run；仍受持久 outbox 去重。
```

## 验收与当前限制

单元/集成测试覆盖登录、签名会话、跨站 POST、无付费网页接口、数据隔离、来源单位与日期、分页重复、预测幂等、假日/迟到跳过、通知送达未知。CI 构建真实部署镜像，并在只读镜像中验证固定引擎及关闭的调度，零真实模型/供应商调用。

本机可使用已经保存的成功真实报告验证手机展示；这不等于新云端预测或微信送达。完整云端验收必须在指定服务器完成：HTTPS 登录、孤立数据首次更新、固定引擎一次真实运行、重启/重复执行不重复付费、微信手机实际接收并打开报告。没有服务器/域名/SendKey 时，这几项明确为待配置/待验收。选股盈利能力仍待前瞻观察。

官方接口依据：[Server酱官方 SDK](https://github.com/easychen/serverchan-sdk)、[Server酱通道设置](https://sct.ftqq.com/forward/)、[产品区别](https://sc3.ft07.com/doc)、[TuShare 日线单位](https://tushare.pro/document/2?doc_id=27)、[每日指标单位](https://tushare.pro/document/2?doc_id=32)、[涨跌停价格与上限](https://tushare.pro/document/2?doc_id=183)、[官方分页说明](https://github.com/waditu/tushare/issues/1256)、[Caddy HTTPS](https://caddyserver.com/docs/automatic-https)、[uv Docker](https://docs.astral.sh/uv/guides/integration/docker/)。

## 本轮实际验证（2026-10-05）

- 全仓 274 项测试通过；固定引擎和预算、模型/策略均未变。
- 本机真实 TuShare HTTPS 日历探针成功，读取一条交易日历、一次供应商请求，零新增模型调用。尚未替代云服务器首次全量增量更新验收。
- 成功预测 `20261005T180149-c75a6a8c` 的五个原文件保持不变，仅在新隔离目录复制并派生手机视图。目标 10/8、行情 9/30、3 个重点与 4 个观察，结果待观察。
- 实际浏览器从受保护报告链接进入、登录并自动返回报告；390px 下七个候选完整、没有横向溢出或内部诊断。首轮发现 no-referrer 令表单 Origin 为 null 导致拒绝，改为标准 strict-origin-when-cross-origin；外站及 null Origin 仍拒绝。依据 [MDN Origin](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Origin)。
- 微信真实送达、域名 HTTPS、云服务器付费模型链路仍待接入后验收；本机没有 Docker，引入 CI 的真实镜像构建/封版验证记录随交付摘要提供。
