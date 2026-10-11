# 阶段 D 实施计划：iCloud 隐藏邮箱引擎

对应规格：`docs/superpowers/specs/2026-10-12-phase-d-icloud-engine-design.md`

## 施工顺序（每个子步骤独立可验收）

1. **D0 服务骨架**：`apps/icloud_api`（pyproject、settings、db、models、alembic 0001、`/health`、`/capabilities`）、
   `infra/icloud-api.Dockerfile`、`infra/icloud-compose.yaml`；`apps/icloud_web` 留到 D2 之后。
   验收：容器起得来、迁移建表、`/icloud-api/v1/health` 200。
2. **D1 引擎核心（不依赖真实账号）**：
   - `srp/`（params、client、utils）逐行对应 Go 实现；测试用同一组参数模拟服务端互验 M1/M2。
   - `rate_limit.py`（最小间隔 + 临时封禁取大值）；测试覆盖封禁优先、间隔到期放行。
   - `aliases.py` 的解析与错误映射：`parse_alias_list`、`parse_alias_created_at`、`new_random_label`、
     `operation_error`（409/429/信任会话）、`alias_retry_after`；测试用真实响应形状的样本。
   - `mail/date.py`（RFC2822 与常见变体解析、年份合法性）。
   - `accounts.py`：凭据 AES-GCM 加解密 + 脱敏；测试覆盖往返、密文不含明文、脱敏不含凭据。
   验收：`pytest apps/icloud_api/tests` 全绿 + `ruff check` 干净。
3. **D2 授权与会话**：`browser_auth.py`（发起返回 `iba_` 一次性码、回填校验、状态查询、TTL 过期）、
   `apple_login.py`（SRP 会话：authStart → authSRP → 2FA → 落 Cookie）、`apple_auth_legacy.py`（旧登录回退）。
   验收：授权码流程全绿；登录流程用假 idmsa 服务验证请求序列（真实登录待账号）。
4. **D3 收信**：`mail/imap.py`（连接、列信、按收件人查、取正文、HTML 转文本）、`mail/idle.py`（UID 游标、
   分批同步、退避）、`mail/web.py`（iCloud web 收信回退）。验收：本地假 IMAP 服务；真实链路标注 pending。
5. **D4 别名写操作与调度**：`alias_service.py`（Apple Account 优先、失败回退 iCloud web 并记录原因）、
   `apple_account.py`（Apple Account 会话与建别名）、`scheduler.py`（轮次创建、开关默认关、事件留痕）。
   验收：假上游验证请求形状与回退顺序；真实创建待账号。
6. **D5 平台接线**：
   - A：新增来源适配器 `kind=icloud_hme`（只读读信），走引擎机器接口；`MAILS_UPSTREAM_ORIGINS` 加引擎 origin。
   - C：库存同步（拉引擎别名 → `inventory_mailboxes` 幂等 upsert）+ 幽灵行清理 + 取件时按邮箱 15s 节流刷新。
   - 前端 `icloud_web`：账号接入、别名列表、浏览器授权、定时创建开关（默认关）。
   验收：A 的实时读信与 C 的取件刷新端到端（接入账号后）。
7. **D6 真实验收（需 fengye 提供 Apple 账号 + App Password）**：
   账号接入 → IMAP 读信 → 别名同步 → 建一个别名（显式确认）→ 取件页看到该别名邮件；结果写入本计划末尾。

## 测试与验收命令

```bash
# D1 起（引擎单元测试，无需真实账号）
cd /opt/mails/src
PYTHONPATH=apps/icloud_api/src apps/delivery_api/.venv/bin/python -m pytest apps/icloud_api/tests -q
apps/delivery_api/.venv/bin/ruff check --output-format=concise apps/icloud_api/
```

## 需要外部输入的点（阻塞项）

- 真实账号的 **App Password**（7a/7d 读信验收）——待 fengye 提供。
- 浏览器授权的 **人工登录**（Cookie 回填，D2 真实链路）——fengye 本人操作。
- 真实**建别名**授权（D4 真实验收）——需要 fengye 指定账号并确认。

## 进度台账

- 2026-10-12：规格与计划入库；D0/D1 施工中。
- 2026-10-12：D0 骨架、D1 核心（SRP/限速/日期/加解密/别名解析）完成并入库；D1 上游客户端
  （会话校验、别名与转发设置读取、建删方法）完成，全部用假上游测试覆盖。
- 2026-10-12：D2 浏览器授权完成（`iba_` 一次性码、TTL 5 分钟、单次消费、回填校验后落库、
  状态查询、账号增删；163 收件箱网页授权不移植）。管理接口暂用运营 token 守卫，
  登录会话 + CSRF 随 `icloud_web` 在 D5 落地——**这是目前唯一已知的临时鉴权**，D5 必须替换。
- 待施工：D2 余下部分（`apple_login.py` SRP 登录会话、`apple_auth_legacy.py` 旧登录回退）、
  D3 收信、D4 写操作与调度、D5 平台接线。
