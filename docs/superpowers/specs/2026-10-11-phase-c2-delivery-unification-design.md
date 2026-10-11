# 阶段 C2：取件交付统一（吸收 CYMail fulfillment）设计规格

- 日期：2026-10-11
- 状态：实现中；完成后补线上验收记录
- 上级决策：`docs/decisions/0001-platform-boundaries.md`
- 前序规格：`docs/superpowers/specs/2026-10-10-phase-c-mail-delivery-design.md`

## 为什么要做

CYMail（Go，`internal/fulfillment` + `internal/server/fulfillment.go`）与 C（`apps/delivery_api`）
各自实现了一套「库存 → 分配 → 取件凭证 → 取件看邮件」。CYMail 下线后，它的取件交付能力
必须由 C 承接，否则现有发货方式（地址 + 密钥 + 取件链接）会断。本阶段把两者能力**并集**收敛到 C 一处，
C 成为唯一交付实现；CYMail 侧不再新增功能。

## 能力对照与决策

| 能力 | CYMail | C（旧） | C2 决策 |
| --- | --- | --- | --- |
| 取件凭证 | `tok_` 长密钥（可重复使用，重签才失效） | 一次性取件码 → 会话 | **双模式**：`code`（一次性）与 `key`（可重复）；都用同一套会话读邮件 |
| 取件链接 | `/pickup#email=..&key=..`（fragment 不进服务端日志）+ 一行发货文本 | 无 | 照搬：签发返回 `pickup_url` 与 `delivery_text`（`地址---密钥---链接`） |
| 批量签发 | 1..750 个邮箱，去重、逐项失败、部分成功 | 仅单条 | 新增批量分配接口，语义照搬 |
| 重签密钥 | `ReissuePickupKey`（旧密钥立即失效） | 无 | 新增 `mode=key&reissue=true` |
| 邮件存储 | `messages` 表，(account, provider_message_id, mailbox) 唯一，幂等 upsert | 邮箱行上的 `messages` JSON 数组 | 新增 `delivery_messages` 表 + 幂等 upsert；JSON 列迁移后删除 |
| 验证码 | 入库时正则提取 `otp_code` | 登记时由调用方给出 | 入库时服务端提取（`otp_code`） |
| 激活链接 | 无（只有正文纯文本） | 无 | 新增 `link`：正文第一个 http(s)，取件页可点可复制 |
| HTML 正文 | 存 HTML 原文 | `plain_text()` 兜底去标签 | 入库即转纯文本（保留换行与链接），出库再过一次 `plain_text()` 兜底 |
| 取件时刷新 | 取件请求触发上游拉信（每邮箱 15s 节流） | 无（只读登记时的 JSON） | **本阶段不做上游拉取**（属批次 7 引擎范围）：改为运营/中枢幂等推送邮件，取件响应如实回报 `sync` 状态，不假装有实时刷新 |
| 反枚举 | 按提交值 + 来源 IP 双桶限速（内存） | 仅按来源 IP（落库） | 双桶落库：`pickup-value:<hash>` 与 `pickup-source:<ip>`，任一超限即 429 |
| 审计 | 无 | 不可变事件表 | 保留 C 的审计，新增事件类型 |
| 库存来源 | 上游 iCloud 别名同步 + 幽灵行清理 | 运营登记 | 保留登记；上游同步属批次 7 |

## 数据模型（迁移 0002）

- `delivery_messages`（新）：`id`、`mailbox_id`(FK)、`provider_message_id`、`sender`、`recipient`、
  `subject`、`body_text`、`otp_code`、`link`、`received_at`、`created_at`；
  唯一约束 `(mailbox_id, provider_message_id)`，索引 `(mailbox_id, received_at)`。
- `pickup_credentials.key_hash`（新列，唯一、可空）：仅存 SHA-256 摘要。
- `inventory_mailboxes.messages`：迁移时逐行回填进 `delivery_messages`，然后删列（不留双份真相）。

`provider_message_id` 缺失时用 `sha256(sender|subject|received_at)` 派生，保证同一封邮件重复推送仍是幂等更新。

## API

运营（`Authorization: Bearer <operator token>`）：

- `POST /delivery-api/v1/inventory/mailboxes`：登记邮箱；`messages` 兼容旧入参，写入 `delivery_messages`。
- `POST /delivery-api/v1/inventory/mailboxes/{mailbox_id}/messages`：幂等推送邮件，返回 `{inserted, updated, total}`。
- `GET /delivery-api/v1/inventory/mailboxes/{mailbox_id}/messages?limit=`：运营核对入库邮件。
- `POST /delivery-api/v1/inventory/allocations`：单条分配（兼容旧调用）。
- `POST /delivery-api/v1/inventory/allocations/batch`：`{mailbox_ids[1..750], external_ref, ttl_seconds, issue_key}`；
  去重、`external_ref` 多邮箱时追加 `-001` 序号、逐项失败数组、部分成功返回 201。
- `POST /delivery-api/v1/pickups`：`{allocation_id, mode: "code"|"key", reissue, ttl_seconds}`；
  明文只返回一次。
- 其余（撤销、停用、清理、审计）保持原有语义。

取件（公开）：

- `POST /delivery-api/v1/pickups/verify`：`{pickup_code}` 或 `{pickup_key}`（可带 `email` 做地址匹配校验）；
  成功返回短时取件会话；两者都不给 → `400 DELIVERY_PICKUP_CREDENTIAL_REQUIRED`。
- `GET /delivery-api/v1/pickups/{allocation_id}/mailbox`：返回 `{address, messages[], sync{}}`；
  `messages[]` 仍是纯文本字段（`id`/`sender`/`subject`/`received_at`/`body_text`/`verification_code`/`link`）。

公开路由统一附加安全响应头：`Cache-Control: no-store`、`Pragma: no-cache`、
`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
`Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'`。

## 事务与安全不变量（在旧规格基础上补充）

1. 一个库存邮箱同一时刻最多一个有效分配（不变）。
2. 取件码一次性；取件密钥可重复验证但受分配有效期、重签与限速约束。
3. 密钥与取件码一律只存摘要；明文只在签发响应中出现一次，不入库、不打日志、不进审计详情。
4. 取件响应只含纯文本：入库转文本 + 出库 `plain_text()` 兜底，任何情况下不返回 HTML。
5. 反枚举双桶：提交值桶 + 来源 IP 桶，任一超限返回 `429 DELIVERY_PICKUP_RETRY_LATER`。
6. 已知取件码/密钥在分配无效时的失败会计入该凭证的 `failed_attempts`，达到上限后该凭证作废
   （修复旧实现里「计数永不递增、上限检查形同虚设」的问题）。
7. 取件链接的密钥放在 URL fragment，服务端与访问日志都不应出现明文密钥。

## 验收

- 迁移后现网 3 个邮箱的邮件（含 JSON 里的 1+2 封）完整落到 `delivery_messages`，旧列删除。
- 幂等：同一 `provider_message_id` 推送两次只有一行且字段被更新。
- HTML 正文推送进来后取件只见纯文本，且 `link` / `验证码` 可读。
- 双模式取件：码与密钥都能换到会话；重签后旧密钥立即 401，新密钥可用。
- 反枚举：连续错误尝试触发 429，且审计留痕。
- 未运行的测试与缺失环境（真实上游）如实标注 skipped。
