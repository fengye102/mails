# 0001：统一核心与来源边界

- 日期：2026-10-09
- 状态：已接受
- 决策范围：A → B → C 三阶段产品架构

## 决策

建立新的统一核心和统一界面，通过 HTTP 适配器接入现有邮箱系统。现有仓库各自负责其上游授权、邮箱操作和原始邮件获取；统一核心负责本项目用户、来源连接、规范化邮箱和邮件索引、验证码提取、同步状态及统一 API。

三个参考系统不作为本项目内部模块直接复制：

| 来源 | A 阶段集成方式 | 经代码/文档确认的接口线索 |
| --- | --- | --- |
| OutlookEmailPlus | 使用 `X-API-Key` 对外接口；管理员显式配置可读邮箱地址，不依赖其后台会话做账号发现。 | `/api/external/health`、`/api/external/account-status`、`/api/external/messages`、`/api/external/messages/{message_id}` |
| cloudflare_temp_email | 使用独立部署的 Worker 管理接口；连接凭据由统一核心加密保存。 | `x-admin-auth` 请求头、`/admin/address`、`/admin/mails`、`/admin/mails/:id` |
| CYMail | 使用其服务端 API；iCloud 登录与浏览器扩展继续由 CYMail 管理。 | `/api/accounts`、`/api/aliases`、`/api/messages`、`/api/inbox` |

接口线索只表示仓库中存在相应路由，不表示响应契约已经通过运行验证。每个适配器落地前必须先用固定版本的上游和契约测试确认鉴权、分页、消息 ID、时间格式及错误语义。

已核实的接入限制：

- OutlookEmailPlus 外部接口按邮箱地址读取，不提供用外部 API Key 枚举所有账号；A1 由管理员显式登记地址。单页 `top` 上限为 50，当前列表响应的 `has_more` 恒为 `false`，所以适配器按 `skip` 和实际返回条数分页，而非信任该字段。
- cloudflare_temp_email 的 `/open_api/admin_login` 只校验口令并返回成功标志，不产生供管理接口使用的 JWT。管理接口实际使用 `x-admin-auth` 请求头，且可能配置 `ADMIN_API_IP_WHITELIST`；适配器仅由服务端发送该密钥。
- CYMail 的 `/api/messages` 读取已采集到其 PostgreSQL 的统一邮件，要求配置 `DATABASE_URL`；它不是直接读取 Apple 收件箱的接口。A3 须验证采集任务与 `/api/mail/collect` 后再依赖此列表。

对应固定版本源码：[OutlookEmailPlus 邮件读取](https://github.com/ZeroPointSix/outlookEmailPlus/blob/ea531a1f40e7badd0769a46f906213f02bab7c82/outlook_web/services/external_api.py)、[Cloudflare Worker 管理鉴权](https://github.com/dreamhunter2333/cloudflare_temp_email/blob/be7ba25a574edbf567f57d1f97b21cb200ee84b9/worker/src/worker.ts)、[CYMail 统一邮件接口](https://github.com/fengye102/cymail/blob/b186781bdfcc32dbad458c9d9c81a306148edab9/admin-server/internal/server/fulfillment.go)。

## 内部交付顺序

- A1：私有核心 + OutlookEmailPlus 单来源端到端闭环，形成可运行的统一界面和规范化模型。
- A2：接入 cloudflare_temp_email，验证多来源同步、去重与故障隔离。
- A3：接入 CYMail，完成 iCloud 隐藏邮箱读取与索引。
- A4：完善团队权限、跨读取方式的消息身份核对、统一搜索、运维和整体验收。A4 完成才视为 A 阶段交付。
- B：单独设计公开临时邮箱入口与反滥用；公开流量不接触 A 的私人来源凭据。
- C：单独设计库存、分配和取件交付；不在 A 中提前暴露取件端点。

## 选择理由与备选方案

1. 三个应用并排部署、只做反向代理：快，但用户、邮件和任务没有统一模型，不满足“一个完整项目”。
2. 以 OutlookEmailPlus 为主分支，把另两套功能直接并入：前期复用多，但私人管理、公开临时邮箱和 iCloud 交付混入同一数据库与授权边界，后续演进成本高。
3. 新核心 + 适配器：初期接口工作较多，但可按来源独立验证和替换，故采用。

## 参考版本与许可记录

本次设计依据 2026-10-09 读取的 `main`：

- `fengye102/cymail`：`b186781bdfcc32dbad458c9d9c81a306148edab9`；GitHub 仓库元数据未提供 SPDX 许可证，仓库顶层未见 `LICENSE` 文件。因此本项目不复制或再分发其源码。
- `ZeroPointSix/outlookEmailPlus`：`ea531a1f40e7badd0769a46f906213f02bab7c82`；Apache-2.0。
- `dreamhunter2333/cloudflare_temp_email`：`be7ba25a574edbf567f57d1f97b21cb200ee84b9`；MIT。

## 影响

- 统一界面可以显示来源健康状态；来源故障不导致其他来源不可用。
- 上游升级必须通过适配器契约测试，不能根据 README 文案推断兼容。
- A1 仅有一个真实来源，结构必须允许后续增加来源，但不得为 B、C 编写未使用的抽象或业务代码。
- 若未来确认拥有 CYMail 的再分发许可，可另写决策记录调整打包方式；当前按独立外部服务接入。
