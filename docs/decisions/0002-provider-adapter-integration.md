# 0002 来源适配器接入边界

- 日期：2026-10-09
- 状态：实施前决策
- 替代：无

## 决策

统一核心继续使用 `NormalizedMessage`、`MessageListResult` 和按 `(mailbox_id, provider_message_key)` 去重的同步流程。A2 接入 `cloudflare_temp_email` 的只读管理邮件 API；A3 接入 CYMail 的只读 `/api/inbox` API。两个来源都作为显式 Python 适配器实现，不复制上游 Worker、Go 管理端、前端或数据库。

每个来源连接的 `encrypted_secret` 保存该来源的机器凭据：Cloudflare 为管理员 API 密钥或地址 JWT（由部署者选择其可读范围）；CYMail 为内部 API Key。适配器只请求列举和详情所需接口，不实现创建地址、Apple 授权、库存、订单、发件或公开用户功能。

## Cloudflare 契约

- 认证：`Authorization: Bearer <secret>`。
- 列表：`GET /admin/mails?address=<address>&limit=<1..100>&offset=<n>`。
- 响应：`{"results":[{"id","message_id","source","address","raw","created_at"}],"count":n}`；若 `raw` 缺失或为空，条目视为来源契约错误。
- 详情：`GET /admin/mails/{id}`，返回同一邮件记录。适配器在本地解析 RFC822 纯文本、主题、发件人、收件人和 Message-ID；不向前端暴露 HTML。
- 连接探测：`GET /` 或 `/health_check` 只验证 HTTP 可达和非错误响应；鉴权失败映射 `UPSTREAM_AUTH`。

## CYMail 契约

- 认证：`X-API-Key: <secret>`（也兼容 Bearer，但核心固定使用 X-API-Key）。
- 列表：`GET /api/inbox?account_id=<account_id>&alias=<address>&limit=100&days=7`。
- 响应：`{"success":true,"data":{"messages":[{"id","from","to","subject","date","preview","body"?}],"method":"imap|web_api"}}`。
- 列表中的 `body` 存在时直接使用；否则详情端点无单封读取契约，适配器将 `preview` 作为纯文本正文并标记为摘要，避免猜测或调用管理端 Cookie 接口。
- `account_id` 在连接配置中保存，邮箱地址在统一核心的 mailbox 记录中保存。
- 认证、HTTP 5xx/超时和 `success:false` 映射为固定 ProviderError；不回传上游 `message` 字段。

## 连接配置

A2/A3 仍复用 `provider_connections`，但 `kind` 扩展为 `cloudflare_temp_email`、`cymail`；连接 `name`、`base_url`、加密 secret 不变。来源专有字段暂不增加数据库列：Cloudflare 无额外字段，CYMail 的 `account_id` 作为邮箱登记时的 `provider_mailbox_key` 前缀格式 `account_id\x00address` 存储，并单独在 mailbox `address` 保存可展示地址。

## 验收

- 三来源适配器均能把模拟响应转换为同一 `MessageSummary`/`NormalizedMessage`。
- 一个来源超时或契约错误时，其他来源同步仍可成功。
- 相同来源重复同步不重复落库；不同来源相同数字 ID 不碰撞。
- Cloudflare 原始 MIME 只在适配器内解析为纯文本；CYMail 的响应正文按纯文本处理。
- 适配器测试覆盖认证头、分页、坏响应、时间解析、HTML 不执行、详情缺失降级和固定错误码。
