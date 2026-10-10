# A4 统一后台硬化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended). Steps use checkbox (`- [ ]`) syntax.

**Goal:** 完成 A 阶段私有统一后台的权限、统一搜索、身份核对和运维硬化。

**Architecture:** 在现有服务端会话和统一 Message 模型上加最小边界，不引入权限框架、消息队列或自动跨来源合并。读取权限和写入权限分开依赖，搜索由 SQL 参数化查询完成，identity_status 仅为审计信号。

**Spec:** `docs/superpowers/specs/2026-10-10-phase-a4-hardening-design.md`

### Task 1: 权限与 owner CLI

- 新增 `require_member` 读依赖；保留 `require_owner` 写依赖。
- 增加 member 创建 CLI 和测试；成员不能写连接、邮箱或同步。

### Task 2: 统一搜索与身份状态

- 扩展 Message API query/source_kind/connection_id；响应增加 source_kind、identity_status。
- 新增 identity service/test：同邮箱相同有效 RFC Message-ID 标记 same_rfc_id，但 provider key 不合并。

### Task 3: 前端与运维

- Inbox 添加全文搜索和来源筛选，显示 source kind/identity status。
- sync-runs 默认最近 100 条；runbook 补充 A4 成员、备份恢复、轮换和验收步骤。

### Task 4: 全量验证与主工作区迁移

- 后端/前端/ruff/build/diff check；无 Docker/PostgreSQL/真实凭据时记录 skipped。
- 将 A4 分支合并回 `D:\code\Program\Mails`。
