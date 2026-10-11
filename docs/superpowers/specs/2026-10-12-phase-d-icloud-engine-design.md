# 阶段 D：iCloud 隐藏邮箱引擎（吸收 CYMail hme/srp/mail）设计规格

- 日期：2026-10-12
- 状态：实现中（D1 核心先行；需要真实 Apple 账号的部分标注为待接入）
- 上级决策：`docs/decisions/0001-platform-boundaries.md`
- 前序规格：`docs/superpowers/specs/2026-10-11-phase-c2-delivery-unification-design.md`
- 参考实现（只读）：CYMail `admin-server`（Go，`internal/hme`、`internal/srp`、`internal/mail`、`internal/account`、`internal/server`）

## 为什么要做

CYMail 是 iCloud 隐藏邮箱引擎（建/停/删别名、Apple 登录、收信、定时创建）。平台目前只有
**只读来源适配器**（cloudflare_temp_email / cymail / outlook_plus），没有「自己管邮箱」的能力：

- C 的库存只能靠运营手工登记，取件时无法刷新上游邮件（C2 规格把这条推给了本阶段）；
- A 读 iCloud 邮件必须经 CYMail 中转，CYMail 一旦下线读链路就断；
- 没有 iCloud 别名能力就没有可交付的库存来源。

本阶段把 CYMail 的引擎**全量移植**进平台（决策：不保留 Go 服务，避免长期双份维护），
按「算法、常量、端点路径、错误码一律照原实现」的原则逐包对应，不重新设计协议细节。

## 引擎归属与目录

| 归属 | 内容 | 理由 |
| --- | --- | --- |
| 新服务 `apps/icloud_api/` | 引擎全部：SRP 登录、Apple Account 会话、别名读写、IMAP 收信、限速、浏览器授权、定时创建 | 长驻状态多（每邮箱 IDLE、调度循环、授权会话、限速桶）；与 A/C 故障域隔离；与 CYMail 结构 1:1，移植成本最低 |
| 新前端 `apps/icloud_web/` | 账号接入、别名管理、浏览器授权、定时创建开关 | 与 `delivery_web` 同构 |
| A（`apps/api`） | 新增只读来源 `kind=icloud_hme`，经引擎机器接口读信 | 统一收件箱已有三来源模式，加一个来源即可 |
| C（`apps/delivery_api`） | 库存同步（7a）、取件时按邮箱节流刷新、邮箱池（阶段 E） | C 是库存与交付的唯一真相来源 |

引擎对外两类接口：

- **机器接口** `/icloud-api/v1/*`（`Authorization: Bearer <operator token>`）：给 A/C/注册机调用，只做数据交换。
- **管理接口** `/icloud-api/admin/*`（登录会话 + CSRF）：给 `icloud_web` 用，含凭据写入、授权发起、调度开关。

## 模块映射（Go → Python，逐文件）

| CYMail（Go） | 行数 | 新平台（Python） | 本阶段 |
| --- | --- | --- | --- |
| `internal/srp/{params,client,utils}.go` | 341 | `icloud_api/srp/{params,client,utils}.py` | D1 |
| `internal/hme/client.go`（iCloud web 接口） | 1073 | `icloud_api/aliases.py` | D1（读）/ D4（写） |
| `internal/hme/apple_srp_login.go` | 751 | `icloud_api/apple_login.py` | D1 |
| `internal/hme/apple_account.go`（Apple Account 新接口） | 1289 | `icloud_api/apple_account.py` | D4 |
| `internal/hme/auth.go`（旧登录回退） | 425 | `icloud_api/apple_auth_legacy.py` | D1 |
| `internal/mail/client.go`（IMAP） | 406 | `icloud_api/mail/imap.py` | D3 |
| `internal/mail/idle.go` | 463 | `icloud_api/mail/idle.py` | D3 |
| `internal/mail/date.go` | 97 | `icloud_api/mail/date.py` | D1 |
| `internal/mail/web_client.go`（iCloud web 收信） | 278 | `icloud_api/mail/web.py` | D3 |
| `internal/mail/netease_web_client.go` | 801 | **不移植**（163 网页取件已确认不做） | — |
| `internal/account/manager.go` | 1208 | `icloud_api/accounts.py` | D1 |
| `internal/server/alias_rate_limiter.go` | 68 | `icloud_api/rate_limit.py` | D1 |
| `internal/server/browser_auth.go` | — | `icloud_api/browser_auth.py` | D2 |
| `internal/server/scheduler.go` | 700+ | `icloud_api/scheduler.py` | D4 |
| `internal/server/server.go` 的 `createAliasPreferAppleAccount` | — | `icloud_api/alias_service.py` | D4 |
| `internal/fulfillment/*` | 809 | 已由阶段 C2 吸收，不重复移植 | 完成 |

## 数据模型（`apps/icloud_api`，迁移 0001）

- `icloud_accounts`：`id`、`name`、`real_email`、`icloud_email`、`cookies_enc`（AES-GCM 密文）、
  `host`、`proxy`、`app_password_enc`、`apple_session_enc`、`forward_to_emails`、`selected_forward_to`、
  `status`、`alias_total`、`alias_active`、`last_validated`、`last_error`、`created_at`、`updated_at`。
  凭据列一律密文；`*_enc` 之外不得出现明文 Cookie/App Password/会话。
- `icloud_aliases`：`id`、`account_id`(FK)、`anonymous_id`、`address`、`label`、`note`、
  `state`(active/inactive)、`created_at`、`synced_at`；唯一 `(account_id, address)`。
- `icloud_messages`：`id`、`account_id`、`alias_address`、`provider_message_id`、`sender`、`subject`、
  `body_text`、`otp_code`、`link`、`received_at`、`created_at`；唯一 `(account_id, provider_message_id)`。
- `icloud_scheduler_jobs`：`id`、`account_id`、`enabled`(默认 false)、`target_count`、`interval_seconds`、
  `created_count`、`last_run_at`、`next_run_at`、`last_error`、`events`(JSON，最多 100 条)。
- `icloud_browser_auth`：`request_id`、`account_id`、`kind`、`token_hash`、`status`、`expires_at`（TTL 5 分钟）。

## API（机器接口）

- `GET /icloud-api/v1/accounts`：账号列表（凭据脱敏，只回 `has_cookies` / `has_app_password` 布尔）。
- `GET /icloud-api/v1/accounts/{id}/aliases`：别名库存（分页）；`POST .../aliases/sync`：从上游拉取并清理幽灵行。
- `GET /icloud-api/v1/accounts/{id}/mail?address=&limit=&days=`：按别名地址读信（IMAP 优先，Web 回退）。
- `POST /icloud-api/v1/accounts/{id}/aliases`：建别名（`label`、`note`、`count`）。
- `POST /icloud-api/v1/aliases/{alias_id}/{deactivate|reactivate}`、`DELETE /icloud-api/v1/aliases/{alias_id}`。
- `GET /icloud-api/v1/scheduler/jobs`、`POST .../jobs/{id}/{enable|disable}`：定时创建开关（默认关）。
- `GET /icloud-api/v1/health`、`GET /icloud-api/v1/capabilities`。

管理接口（`icloud_web` 用）：账号增删、App Password 写入、浏览器授权发起/状态、调度参数编辑。
授权回填走公开路由：`POST /icloud-api/public/browser-auth/complete`（一次性授权码，TTL 5 分钟）。

## 安全与行为不变量

1. Cookie / App Password / Apple 会话一律 AES-GCM 加密入库，密钥来自 `ICLOUD_DATA_KEY`（0600 环境文件）；
   明文只在「写入」与「出站请求」两处短暂出现，不进日志、不进响应体、不进审计详情。
2. 别名创建必须走限速器：按账号最小间隔 + Apple 返回的临时封禁（`429`）双约束，封禁期内不再尝试。
3. `409`（地址容量满）与 `429`（临时限速）是**业务错误**，如实映射为 `ALIAS_LIMIT_REACHED` / `ALIAS_RATE_LIMITED`，
   不得吞掉、不得重试成 500。
4. 删除别名不可逆：管理接口要求显式 `confirm=true`，机器接口不提供删除。
5. 定时创建默认关闭；开启后每轮记录事件（最多 100 条），失败原因如实保留。
6. 幽灵行清理只在同步时标记/删除「上游已不存在」的本地别名，不动有有效分配的别名。

## 验收

- D1：SRP 数学自检（客户端与服务端用同一组参数互相验证 M1/M2）、限速器行为、别名/邮件解析、
  消息时间解析，全部有单元测试且不依赖真实账号。
- D2：浏览器授权码流程（发起 → 回填 → 状态查询 → 过期 401）有测试。
- D3：IMAP 读信与 IDLE 监听用本地假 IMAP 服务验证；真实 iCloud IMAP 需接入账号后验收（标注 pending）。
- D4：建/停/激活/删别名的请求形状、错误映射、双接口回退顺序用假上游验证；真实创建需账号。
- 真实账号链路（建别名、读信）在接入账号后补做，未运行前不得标记通过。
