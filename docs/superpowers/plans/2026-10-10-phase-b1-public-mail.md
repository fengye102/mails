# B1 公开临时邮箱实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended). Steps use checkbox (`- [ ]`) syntax.

**Goal:** 交付与 A 私有后台隔离的公开临时邮箱创建、收件和纯文本阅读切片。

**Architecture:** 独立 `apps/public_api` 保存公共邮箱元数据和能力令牌哈希，通过配置的 Cloudflare Worker 读取；独立 `apps/public_web` 只调用 `/public-api/v1`，不导入 A 的包或数据库。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、Alembic、httpx、cryptography；React/TypeScript/Vite/Vitest；PostgreSQL 16、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-10-10-phase-b1-public-mail-design.md`

## Global Constraints

- B1 公共凭据、数据库、Cookie 和 API 路径必须与 A 隔离。
- 服务端不向浏览器返回 Cloudflare JWT、管理密钥或上游 HTML。
- 邮件详情只返回纯文本；固定错误代码不回显上游响应。
- 每 IP 每小时 3 次创建、每会话最多 5 个活跃邮箱、列表 30 次/分钟、详情 60 次/分钟。
- 所有时间使用 UTC ISO 8601；邮箱过期后拒绝读取。

### Task 1: Public API foundation and Cloudflare adapter

创建 `apps/public_api/pyproject.toml`、`src/public_mail_api/{app,settings,models,db,security,rate_limit}.py`、`providers/cloudflare_public.py`、Alembic 初始迁移和契约测试。实现独立表、令牌哈希、AES-GCM JWT、TTL 和上游创建/列表/详情。

### Task 2: Public routes and API tests

创建 `apps/public_api/src/public_mail_api/routes/{mailboxes,messages}.py` 及测试；完成创建、Cookie 鉴权、过期、跨邮箱拒绝、固定错误和限流。

### Task 3: Public Web

创建 `apps/public_web` React/Vite 应用和测试；实现创建地址、TTL、刷新列表、详情、复制验证码和过期状态。Web 只使用 `/public-api/v1`。

### Task 4: Deployment and verification

新增 B1 Dockerfile、Compose 环境变量样例和运行手册；执行 B1 测试、A 全量测试、前端 typecheck/build、ruff/diff check；无 Docker/PostgreSQL/真实 Cloudflare 时记录 skipped。
