# 阶段 C2 实施计划：取件交付统一

- 日期：2026-10-11
- 规格：`docs/superpowers/specs/2026-10-11-phase-c2-delivery-unification-design.md`
- 范围：`apps/delivery_api/`、`apps/delivery_web/`；不动 A/B1，不动上游容器。

## 步骤

1. 文档：Codex.md 的 C 段补 C2 约定；新增本规格与本计划。
2. 邮件层（先失败测试）：
   - `delivery_api/extract.py`：`extract_code`、`extract_link`（与 A 同语义，独立实现）。
   - `delivery_api/htmltext.py`：`html_to_text`（保留换行与 http(s) 链接）、`looks_like_html`。
   - `delivery_messages` 模型 + 幂等 upsert + 运营推送/查询接口。
   - 迁移 0002：建表、回填 JSON、删列、加 `pickup_credentials.key_hash`。
3. 签发层（先失败测试）：
   - `mode=code|key`、`reissue`、批量分配、`pickup_url` / `delivery_text`。
   - 取件 `verify` 支持密钥与地址匹配；双桶限速；凭证失败计数修复；公开路由安全头。
4. 取件页：URL fragment 自动取件（取后清理 fragment）、行动链接、入库状态、错误码文案。
5. 验收：全量 pytest、tsc/vitest、迁移在现网 C 库上执行、线上全流程验收脚本。
6. 交付：提交推送 GitHub、更新 skill、中文报告。

## 测试命令

- 后端：`apps/delivery_api/.venv/bin/python -m pytest apps/delivery_api/tests -q`
- 前端：`cd apps/delivery_web && npx vitest run`（node:22-alpine 容器内）
- 迁移：`docker compose --project-name mails-c ... exec delivery-api alembic upgrade head`
