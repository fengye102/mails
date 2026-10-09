# A1 私有邮箱核心设计规格

- 日期：2026-10-09
- 状态：已确认（用户于 2026-10-09 确认按此规格继续）
- 上级决策：`docs/decisions/0001-platform-boundaries.md`
- 交付定义：A 阶段第一个可运行的端到端切片，不代表 A 阶段全部完成。

## 1. 目标与非目标

A1 提供一个仅内部访问的统一邮箱后台。管理员配置一个 OutlookEmailPlus 服务和至少一个已在上游授权的邮箱地址后，可以从本项目前端查看该邮箱的邮件、手动同步、读取邮件正文、提取验证码，并看到同步故障。统一核心保存规范化索引，为 A2、A3 的来源接入提供稳定接口。

A1 不承担 Outlook OAuth 授权、IMAP 密码登录或上游账号导入；这些操作仍在 OutlookEmailPlus 完成。A1 不提供公开注册、临时邮箱申请、邮箱池分配、发信、取件码或 iCloud 别名创建。A2、A3、A4 的交付边界见上级决策。

## 2. 技术与运行边界

- API：Python 3.12+、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16+、`httpx`。A1 固定运行一个 API worker，由该进程执行周期同步；不引入消息队列或多副本调度。
- Web：Node.js 22+、React、TypeScript、Vite。通过本项目 API 访问数据，不直接请求上游邮箱服务。
- 部署：Linux Docker Compose 启动 PostgreSQL、API、Web/反向代理；本机 Windows 使用 PowerShell 脚本和 Docker Compose 开发。生产入口只开放 HTTPS，API 和数据库不直连公网。
- 配置：`MAILS_DATABASE_URL`、`MAILS_ENCRYPTION_KEY` 等保存在部署环境；仓库只提供 `.env.example`。上游 API Key 通过后台写入数据库并以 AES-256-GCM 加密。会话采用数据库服务端状态，不使用长期有效的自包含登录令牌。
- 初始化：首次管理员只能通过本机 CLI 命令创建；不存在公开的“首次设置管理员”接口。

## 3. 模型与职责

| 实体 | 必要字段 | 约束 |
| --- | --- | --- |
| `users` | `id`、`username`、`password_hash`、`role`、`created_at` | A1 仅创建 `owner`；口令采用 Argon2id；用户名唯一。 |
| `sessions` | `id`、`user_id`、`token_hash`、`csrf_token`、`created_at`、`last_seen_at`、`expires_at`、`revoked_at` | 随机 32 字节会话令牌只进 Cookie，数据库只存其 SHA-256 摘要；独立随机 CSRF Token 存在服务端并通过会话查询返回。登出立即吊销；空闲 30 分钟或绝对 8 小时到期，由服务端判定。 |
| `provider_connections` | `id`、`kind`、`name`、`base_url`、`encrypted_secret`、`status`、`last_error`、`last_checked_at` | A1 的 `kind` 仅 `outlook_plus`；密钥不进入普通 API 响应。 |
| `mailboxes` | `id`、`connection_id`、`provider_mailbox_key`、`address`、`display_name`、`sync_state`、`last_synced_at` | 同一连接下上游邮箱键唯一；A1 由管理员显式登记地址。 |
| `messages` | `id`、`mailbox_id`、`provider_message_key`、`rfc_message_id`、`sender`、`recipient`、`subject`、`snippet`、`received_at`、`body_text`、`verification_code`、`created_at` | 同一邮箱下上游消息键唯一，重复同步执行 upsert；A1 仅存纯文本正文，不渲染上游 HTML。`rfc_message_id` 仅作为后续跨读取方式核对线索，不在 A1 中作为唯一键。 |
| `sync_runs` | `id`、`connection_id`、`started_at`、`finished_at`、`status`、`error_code`、`error_summary`、`new_message_count` | 记录每次手动/定时同步结果，不记录凭据或完整邮件正文。 |

所有时间以 UTC 存储。`provider_message_key` 由上游读取方式 `method` 与上游 `id` 共同构成，避免 Graph ID 与 IMAP UID 碰撞；核心把它作为不透明字符串处理，不根据其格式推断顺序。消息列表按 `received_at DESC, id DESC` 排序。邮箱地址用于展示和查询，但内部关联只用 ID，避免别名更名导致索引错配。

## 4. 上游契约与数据流

1. 管理员在 OutlookEmailPlus 内完成 Outlook 或 IMAP 邮箱授权，并创建启用的外部 API Key，将 `allowed_emails` 收窄到要纳入本项目的地址。若上游开启公网模式，还须把本项目 API 的出口 IP 纳入其白名单；邮箱池权限不是 A1 的前提。
2. 管理员在本项目登记上游 `base_url`、API Key 和待纳入统一后台的邮箱地址。API 使用 `X-API-Key` 调用 `/api/external/health` 和 `/api/external/capabilities`，要求 `message_list` 与 `message_detail` 能力可用，再用 `/api/external/account-status?email=...` 验证每个地址的 `exists` 与 `can_read`；失败时不激活连接或邮箱。上游接口不依赖其后台 Cookie 或 CSRF Token。
3. 手动同步和定时同步使用 `GET /api/external/messages?email=...&folder=inbox&skip=...&top=50` 按邮箱读取摘要；最多读取两页、100 封，不依赖当前实现恒为 `false` 的 `has_more`。只对尚未索引的消息使用 `GET /api/external/messages/{message_id}?email=...&folder=inbox` 获取正文。两种接口均检查上游的 `{success, code, data}` 包装层，不能仅凭 HTTP 200 判断成功。
4. 适配器把上游数据变为内部 `NormalizedMessage`：读取方式与上游 ID、邮箱 ID、发件人、收件人、主题、UTC 收件时间、纯文本正文和摘要。无上游 ID 的响应作为适配器错误，不通过正文哈希猜测身份。从上游 `raw_content` 解析到有效 `Message-ID` 时记录 `rfc_message_id`，否则留空。上游从 Graph 回退到 IMAP 时，同一封实体邮件可能获得新 ID；跨方式自动合并在 A4 完成，以免在证据不足时错误合并不同邮件。
5. 核心从纯文本正文和主题中用确定性规则提取 4–8 位数字验证码；有多个候选且无法明确判定时返回空值，不猜测。提取结果写入 `messages.verification_code`，可由用户在界面复制。
6. 每次同步在单个邮箱粒度提交；单封邮件解析失败记录错误并继续处理后续邮件，单个邮箱失败不阻断其他邮箱。同步记录准确反映部分成功与失败。

为避免无限制拉取，A1 每个邮箱首次同步只保存最近 7 天、最多 100 封；之后每次读取最近 100 封并依靠读取方式与上游消息 ID 去重。重复同步不重复拉取已索引邮件的正文。管理员可手动重试。历史补全和自定义保留期归入 A4。

上游契约依据固定提交的[接口文档](https://github.com/ZeroPointSix/outlookEmailPlus/blob/ea531a1f40e7badd0769a46f906213f02bab7c82/%E6%B3%A8%E5%86%8C%E4%B8%8E%E9%82%AE%E7%AE%B1%E6%B1%A0%E6%8E%A5%E5%8F%A3%E6%96%87%E6%A1%A3.md)、[列表与详情实现](https://github.com/ZeroPointSix/outlookEmailPlus/blob/ea531a1f40e7badd0769a46f906213f02bab7c82/outlook_web/controllers/emails.py)及[读取服务实现](https://github.com/ZeroPointSix/outlookEmailPlus/blob/ea531a1f40e7badd0769a46f906213f02bab7c82/outlook_web/services/external_api.py)。运行时行为仍须用契约测试验证。

## 5. 本项目 API 与界面

A1 的 API 路径统一以 `/api/v1` 开头：

| 路径 | 用途 |
| --- | --- |
| `POST /api/v1/session`、`DELETE /api/v1/session`、`GET /api/v1/session` | 登录、退出、读取当前用户及其 CSRF Token；HttpOnly、SameSite=Lax Cookie，生产 HTTPS 设置 Secure，本地 HTTP 开发环境不设置 Secure。 |
| `POST /api/v1/connections`、`GET /api/v1/connections` | 新增和列出上游连接；不回显 API Key。 |
| `POST /api/v1/connections/{id}/mailboxes`、`GET /api/v1/mailboxes` | 登记并列出可读邮箱。 |
| `POST /api/v1/mailboxes/{id}/sync`、`GET /api/v1/sync-runs` | 手动同步和查看结果。 |
| `GET /api/v1/messages`、`GET /api/v1/messages/{id}` | 按邮箱、发件人、主题和时间检索；读取纯文本正文及验证码。 |

除登录外，所有写入接口要求有效会话和 `X-CSRF-Token`，服务端与当前会话的 Token 恒时比较。前端仅在内存持有 CSRF Token，页面刷新后经 `GET /api/v1/session` 重新读取；不放在 URL、Cookie 或持久 Web Storage。会话响应禁止缓存。登录请求也校验 `Origin` 或 `Referer` 与部署 origin 精确一致。未登录返回 401；上游鉴权失败记录 `UPSTREAM_AUTH`，超时记录 `UPSTREAM_TIMEOUT`，响应契约不匹配记录 `UPSTREAM_CONTRACT`，而非把上游错误文本原样展示给用户。

前端只有四个主要页面：登录、来源连接、邮箱列表、统一收件箱。收件箱默认显示最近邮件，可按邮箱、发件人、主题和时间过滤；详情以纯文本展示；验证码仅在确定提取成功时显示复制按钮。连接和邮箱展示最近同步时间、状态与可读的错误摘要。

## 6. 安全、错误与运行

- API Key 和加密主密钥不出现在 URL、页面响应、日志或错误摘要中；主密钥丢失时只能从备份恢复，不提供明文导出。
- 会话令牌不进入数据库明文或浏览器脚本；退出登录必须撤销服务端会话，不能只删除 Cookie。反向代理按真实直连来源对登录请求限速，API 不采信未经配置的转发 IP 头。
- 所有上游 HTTP 请求有连接超时和总超时；仅对超时和 5xx 作有限重试，401/403 与契约错误不自动重试。
- 上游目标采用部署配置中的显式 `MAILS_UPSTREAM_ORIGINS` 白名单：管理员填写的 `base_url` 必须与其中一个 origin 完全匹配。生产 origin 必须是 HTTPS；本机开发才可将 HTTP 本地 origin 列入白名单。适配器不跟随跨 origin 重定向，防止任意服务端请求。
- 同步任务同一邮箱不能并发执行；请求重复触发时返回当前运行状态，不排队叠加。
- 备份覆盖 PostgreSQL 数据和加密主密钥，恢复手册必须说明二者缺一不可；A1 不自动删除邮件。

## 7. 验收与测试

在模拟上游和真实 OutlookEmailPlus 测试实例上验收：

1. 首次管理员由 CLI 创建后可登录；匿名请求无法查看邮箱或消息。
   登录后会话在服务端到期或登出时立即失效；缺少或错误的 CSRF Token 不能修改连接和邮箱。
2. 登记一个上游连接和两个邮箱；健康探测与地址可读性校验通过后显示为可同步。
3. 同一上游读取方式下，一封新邮件同步后只出现一次；连续执行同一同步，邮件数不变。Graph/IMAP 方式切换导致的新 ID 记录在 A4 处理，不把 A1 的去重范围表述成全局保证。
4. 包含单个明确 6 位验证码的邮件能显示并复制；包含多个无明确上下文数字的邮件不输出猜测值。
5. 能按邮箱、发件人、主题和时间过滤并阅读纯文本正文；HTML 内容不会执行。
6. 上游超时、401/403、响应字段缺失分别产生明确状态；一个邮箱失败不影响另一邮箱的已有邮件与同步。
7. API Key 在数据库中为密文，所有 HTTP 响应及应用日志中均不包含原值。
8. `pytest`、前端单元测试、类型检查、生产构建和 Docker Compose 冒烟测试通过，运行命令写入 README。

## 8. 后续兼容边界

A2 的 Cloudflare 和 A3 的 CYMail 适配器复用 `NormalizedMessage` 与同步流程，但各自实现鉴权、邮箱发现和分页。A1 不创建通用插件加载器；适配器以代码模块和显式工厂注册，避免在只有一个来源时引入动态插件系统。A4 再补团队成员权限、跨来源性能与运维完善。B 的公开用户和 C 的取件客户均不复用 A1 的管理会话。

会话与 CSRF 的设计参考 [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html) 和 [OWASP CSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)。
