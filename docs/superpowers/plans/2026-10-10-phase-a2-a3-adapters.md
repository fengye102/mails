# A2/A3 来源适配器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 接入 Cloudflare Temp Email 与 CYMail，使统一后台能在同一收件箱聚合三来源邮件。

**Architecture:** 保留现有核心模型与同步编排，抽象来源协议并用显式工厂按连接 kind 选择适配器。两个新适配器仅读取上游邮件契约，敏感凭据仍由统一连接加密保存。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、Alembic、httpx、email Parser、React/TypeScript/Vite/Vitest、PostgreSQL。

**Spec:** `docs/superpowers/specs/2026-10-10-phase-a2-a3-adapters-design.md`

## Global Constraints

- 工作区是 `D:\code\Program\Mails` 的隔离 worktree，遵守 `Codex.md`。
- 不复制三个上游仓库的代码、前端或数据库；只实现 HTTP 适配器。
- 所有凭据只存 AES-256-GCM 密文；日志和响应不回显密钥、Cookie、原始邮件正文。
- 统一消息只存纯文本；不执行或向前端渲染上游 HTML。
- 上游错误只跨边界传播固定 `ProviderError` code。
- 三来源失败互相隔离；同步按来源消息 key 幂等。

### Task 1: Cloudflare adapter

**Files:** `apps/api/src/mails_api/providers/cloudflare_temp_email.py`, `apps/api/tests/test_cloudflare_temp_email.py`。实现 RFC822 解析、Bearer 认证、分页、详情和固定错误。

### Task 2: CYMail adapter

**Files:** `apps/api/src/mails_api/providers/cymail.py`, `apps/api/tests/test_cymail.py`。实现 X-API-Key、account_id 配置、/api/inbox、body/preview 规范化和错误映射。

### Task 3: Core provider protocol and persistence

**Files:** `providers/base.py`, `models.py`, Alembic migration, `routes/connections.py`, `routes/mailboxes.py`, provider factory。扩展 kind/config_json，保留 Outlook 回归。

### Task 4: Sync and API integration

**Files:** `services/sync.py`, `routes/sync_runs.py`, app wiring, integration tests。同步服务改用统一协议和工厂，三来源独立失败。

### Task 5: Web source selection

**Files:** `apps/web/src/api.ts`, `Connections.tsx`, tests/styles。连接创建可选择三来源和 account_id，敏感字段不回显。

### Task 6: Full verification and docs

**Files:** README、runbooks、integration tests。执行后端/前端全套验证，记录 PostgreSQL/Docker 未具备时的明确 skipped 状态。
