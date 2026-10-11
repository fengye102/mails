# 阶段 E 实施计划：邮箱池（供注册机）

对应规格：`docs/superpowers/specs/2026-10-12-phase-e-mailbox-pool-design.md`

## 施工顺序

1. **E1 池数据与 Key**：迁移 `0003_pool_api_keys.py`（`pool_api_keys`、`pool_claims`）、
   `pool.py` 的 Key 校验（摘要比对 + 项目/域名范围 + 限流）、Key 管理接口（运营 token）。
   验收：Key 明文只在创建响应出现；无效/停用 Key 401；超限 429。
2. **E2 领取与释放**：`claim-random` / `claim-domain`（原子领取 + 租约）、`claim-release`、
   租约到期回收任务；测试含并发争抢与租约过期。
3. **E3 回传与统计**：`claim-complete`（幂等 + 结果记录）、`stats`；测试含重复回传、参数不一致。
4. **E4 取码链路**：`messages` / `verification-code` / `verification-link` / `wait-message`
   复用 C 的邮件表与提取逻辑；`wait-message` 用「持续到超时」的轮询 + 新邮件判定，不引第三方队列。
5. **E5 接线与验收**：nginx `location /pool-api/` 直连 C；注册机侧用现有 OEP 契约做一次真实领取-取码-回传；
   结果写入本计划末尾。

## 测试与验收命令

```bash
cd /opt/mails/src
PYTHONPATH=apps/delivery_api/src apps/delivery_api/.venv/bin/python -m pytest apps/delivery_api/tests -q
apps/delivery_api/.venv/bin/ruff check --output-format=concise apps/delivery_api/
```

## 依赖

- 池里的邮箱要有可读来源：阶段 D 接通 `icloud_hme`（或先用现有 outlook_plus 来源）后才有意义。
- nginx 新增 `/pool-api/` 直连（保留真实 IP）——宝塔侧由运维执行。

## 进度台账

- 2026-10-12：规格与计划入库，未开工（等 D 的库存来源链路）。
