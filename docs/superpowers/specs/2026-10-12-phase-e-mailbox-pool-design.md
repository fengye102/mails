# 阶段 E：邮箱池（供注册机）设计规格

- 日期：2026-10-12
- 状态：规格已定，待阶段 D 的库存来源接通后实施
- 参考实现（只读）：OEP `outlook-email-plus`（`/api/external/pool/*`）

## 为什么要做

OEP 对外提供「注册机领号 → 取验证码/验证链接 → 回传结果」的邮箱池接口，注册机侧已按该契约写好。
批次 8 要下线 OEP，池能力必须由平台承接，且注册机**不应改代码**——所以新池接口沿用 OEP 的对外契约
（路径、字段、错误码），只把底座从 OEP 的账号表换成平台库存。

## 归属与目录

- 并入 C（`apps/delivery_api`），不新增服务：库存（`inventory_mailboxes`）已是唯一真相来源，
  池只是「带租约的分配」，复用 C 已有的邮箱、分配、邮件、密钥与审计设施。
- 新增 `apps/delivery_api/src/delivery_api/pool.py`、`routes/pool.py`、迁移 `0003_pool_api_keys.py`。
- 外部 Key 与项目隔离放 `pool_api_keys`（`key_hash`、`name`、`project`、`enabled`、`allowed_domains`、
  `rate_limit_per_minute`、`created_at`）；Key 只存摘要，明文只在创建响应出现一次。

## 数据模型（迁移 0003）

- `pool_api_keys`：见上。
- `pool_claims`：`id`、`mailbox_id`(FK)、`project`、`claim_token`、`status`
  (`claimed|completed|released|expired`)、`claimed_at`、`lease_expires_at`、`completed_at`、
  `result`(success|failed)、`result_detail`、`idempotency_key`。
  约束：同一邮箱同一时刻最多一条 `claimed`；`idempotency_key` 唯一（可空）。

## API（对外，沿用 OEP 契约）

| 接口 | 说明 |
| --- | --- |
| `POST /pool-api/v1/claim-random` | 随机领取，可按 `domain` / `project` 过滤 |
| `POST /pool-api/v1/claim-domain` | 指定域名领取 |
| `POST /pool-api/v1/claim-release` | 释放（回传参数必须与领取一致） |
| `POST /pool-api/v1/claim-complete` | 回传结果（success/failed + detail） |
| `GET  /pool-api/v1/stats` | 池状态（可用/占用/完成/失败） |
| `GET  /pool-api/v1/messages?email=`、`GET .../verification-code`、`GET .../verification-link`、`GET .../wait-message` | 取码/取链接/等新邮件 |

鉴权：`Authorization: Bearer <pool key>`；`/pool-api/` 走 nginx 直连 C（保留真实 IP 用于限流）。

## 行为不变量

1. 领取必须原子：并发领取同一邮箱只有一个成功（`SELECT ... FOR UPDATE SKIP LOCKED`）。
2. 租约默认 600 秒，到期未回传自动回收为可用；回收后该邮箱进入冷却（默认 86400 秒）不立即再发。
3. `claim-release` / `claim-complete` 的邮箱、token、项目必须与领取记录一致，否则 `409 CLAIM_MISMATCH`。
4. 回传幂等：同一 `claim_token` 重复回传只生效一次。
5. 池只发「有邮件来源可读」的邮箱：无可用来源的邮箱不进池。
6. 每 Key 限流（默认 60 次/分钟）与审计留痕；Key 明文不入库、不进日志。

## 验收

- 并发领取（20 线程争 1 个邮箱）只 1 个成功，其余 409/404。
- 租约到期自动回收 + 冷却期内不再发放。
- 回传幂等、参数不一致报错、`stats` 数字与实际记录一致。
- 取码/取链接/等新邮件与 C 的邮件表一致（同一封邮件在池接口与取件页字段相同）。
