# A2/A3 三来源适配器设计规格

- 日期：2026-10-10
- 状态：已确认（沿用用户已确认的“统一核心、来源适配器、分阶段交付”方案）
- 上级决策：`docs/decisions/0002-provider-adapter-integration.md`

## 目标

在 A1 OutlookEmailPlus 私有后台基础上接入 Cloudflare Temp Email 与 CYMail，只读同步三类来源的邮件到同一 PostgreSQL 索引，并通过现有统一收件箱检索、阅读和提取验证码。

## 范围

- 统一适配器协议：探测、邮箱可读性检查、邮件摘要列表、邮件详情。
- Cloudflare：`/admin/mails` + `/admin/mails/{id}`，Bearer 管理 API 密钥，RFC822 本地解析。
- CYMail：`/api/inbox`，X-API-Key，`account_id` 与别名地址作为邮箱上下文，使用 body 或 preview。
- 连接管理扩展三种 kind，来源工厂和同步工厂按 kind 选择适配器。
- 前端连接表单可选择来源并输入来源专有 account_id；统一收件箱显示来源。

不包含：复制任何上游仓库代码、Cloudflare/CYMail 的公开用户、地址创建、Apple 授权、库存、订单、发件或取件业务。

## 数据与 API

`provider_connections.kind` 支持 `outlook_plus`、`cloudflare_temp_email`、`cymail`。`mailboxes.provider_mailbox_key` 对 CYMail 使用 `account_id\x00address`，其他来源使用地址或上游邮箱键。新增连接字段 `config_json`（非秘密 JSON）保存来源专有配置；迁移默认 `{}`。现有响应增加 `kind` 和非敏感配置摘要，绝不返回密钥。

统一适配器协议必须能被同步服务调用：

```python
class MailProvider(Protocol):
    async def probe(self, base_url: str, secret: str, config: dict) -> None: ...
    async def check_mailbox(self, base_url: str, secret: str, address: str, config: dict) -> str: ...
    async def list_messages(self, base_url: str, secret: str, address: str, config: dict) -> MessageListResult: ...
    async def get_message(self, base_url: str, secret: str, address: str, summary: MessageSummary, config: dict) -> NormalizedMessage: ...
```

## 验收

- 每个适配器契约测试通过；认证头、分页、错误码、时间解析、HTML 不执行均有覆盖。
- 三来源可在同一数据库中登记、同步和搜索；同 ID 不碰撞。
- 重复同步保持幂等；来源失败只标记自己的 sync run。
- 前端可选择三来源，现有 Outlook 行为不回归。
- 后端、前端测试、类型检查、生产构建和 `git diff --check` 通过。
- PostgreSQL/Docker/真实上游联调仍按环境可用性单独记录，不以模拟测试代替。
