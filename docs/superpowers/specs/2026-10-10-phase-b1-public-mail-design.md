# B1 公开临时邮箱设计规格

- 日期：2026-10-10
- 状态：已确认并进入实施
- 上级决策：`docs/decisions/0001-platform-boundaries.md`

## 目标

交付公开临时邮箱最小可运行切片：匿名访客创建临时地址，凭能力 Cookie 查看邮箱和纯文本邮件，邮箱按 TTL 过期并执行基础反滥用限制。B1 与 A 私有统一后台隔离数据库、密钥、会话、凭据和 API 路径。

## 架构与隔离

B1 使用 `apps/public_api` 独立 Python/FastAPI 服务和独立 PostgreSQL 数据库。B1 不导入 A 的 `mails_api` 包，不读取 A 的 users、sessions、provider_connections、mailboxes 或 messages。B1 API 前缀为 `/public-api/v1`，A 仍使用 `/api/v1`。

B1 只连接已部署的 Cloudflare Temp Email Worker；上游管理凭据由 B1 服务环境配置 `PUBLIC_MAIL_UPSTREAM_URL`、`PUBLIC_MAIL_ADMIN_KEY` 和 `PUBLIC_MAIL_ALLOWED_ORIGINS` 提供，不写入浏览器或 A 数据库。地址 JWT 以每个邮箱 AES-256-GCM 密文保存，主密钥为 `PUBLIC_MAIL_ENCRYPTION_KEY`。

## 数据与能力令牌

`public_mailboxes` 包含 `id`、`address`、`address_jwt_ciphertext`、`token_hash`、`created_at`、`expires_at`、`last_accessed_at`、`status`。能力令牌只保存 SHA-256 摘要；Cookie 为 HttpOnly、SameSite=Lax，生产 HTTPS 设置 Secure。状态为 `active|expired|upstream_error`，过期邮箱拒绝读取。

## API

- `POST /public-api/v1/mailboxes`：调用 Cloudflare `/admin/new_address` 创建地址，设置能力 Cookie，返回邮箱地址、ID、expires_at，不返回上游 JWT。
- `GET /public-api/v1/mailboxes/{id}`：能力 Cookie 鉴权，返回地址、状态、expires_at 和剩余秒数。
- `GET /public-api/v1/mailboxes/{id}/messages`：能力 Cookie 鉴权，调用上游地址邮件 API，服务端解析 RFC822，只返回纯文本摘要和确定验证码。
- `GET /public-api/v1/mailboxes/{id}/messages/{message_id}`：能力 Cookie 鉴权，按上游 ID 获取单封详情，只返回纯文本正文和验证码。
- 错误使用固定代码，不回显上游响应、凭据或完整原文；过期返回 `PUBLIC_MAILBOX_EXPIRED`。

## 反滥用

单 IP 每小时最多创建 3 个邮箱；单能力会话最多 5 个活跃邮箱；列表每邮箱每分钟最多 30 次，详情每分钟最多 60 次。创建不支持自定义域名、批量创建或发信。Turnstile 可选，配置缺失时关闭。

## Web 与验收

`apps/public_web` 提供独立页面：创建邮箱、复制地址、显示 TTL、刷新收件箱、纯文本阅读和验证码复制。Web 不使用 localStorage，不请求 A `/api/v1`。测试覆盖令牌哈希、TTL、限流、来源响应解析、HTML 不执行、跨邮箱拒绝和固定错误。B1 Docker/PostgreSQL/真实 Cloudflare 联调在相应环境执行，缺失时明确 skipped。
