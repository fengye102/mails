# A4 统一后台硬化与整体验收设计规格

- 日期：2026-10-10
- 状态：实施中前置规格
- 上级决策：`docs/decisions/0001-platform-boundaries.md`

## 目标

在 A1/A2/A3 三来源聚合代码基础上完成 A 阶段交付所需的硬化：owner/member 只读权限边界、统一来源搜索与来源标识、跨读取方式消息身份的保守核对、同步历史运维限制和备份恢复验收记录。

## 权限

`owner` 保留连接、邮箱登记和手动同步写权限；`member` 可登录、读取来源/邮箱/同步状态/统一邮件，不可修改连接、登记邮箱、触发同步或登出其他会话。现有 `require_owner` 写依赖不变，新增 `require_member` 读依赖。CLI 增加 `create-member`，仅 owner 首次创建后可由受控本机命令创建成员。公开注册、B/C 用户体系不在 A4。

## 统一搜索

邮件列表增加 `source_kind`、`connection_id`、`query` 参数：`query` 对 sender、recipient、subject、snippet、body_text 做字面量大小写不敏感匹配，仍转义 `%`、`_`、反斜杠；响应列表和前端显示来源类型。默认排序和分页不变。

## 身份核对

不把 RFC Message-ID 直接变成唯一键。新增只读 `identity_status`：当同一 mailbox 下多个 provider key 拥有相同有效 RFC Message-ID 时标记 `same_rfc_id`；缺失或冲突标记 `unverified`，不自动合并。同步仍按 provider key 去重，避免误合并。

## 运维

同步运行历史 API 默认最近 100 条，可按 mailbox 过滤；不删除邮件。运行手册补充成员创建、来源故障隔离、密钥轮换前的连接替换、备份恢复后的会话撤销和 A4 验收清单。Docker/PostgreSQL/真实上游仍以实际环境证据为准。

## 验收

- member 登录并读取三来源聚合邮件；所有写入和手动同步被 403 拒绝。
- query/source_kind/connection_id 搜索测试通过，SQL 通配符按字面量处理，来源类型在 API/UI 显示。
- 相同 RFC Message-ID 仅作为核对状态展示，不改变去重数量。
- sync-runs 默认只返回最近 100 条。
- 后端、前端、ruff、构建和 `git diff --check` 通过；PostgreSQL、Docker、真实实例未具备时明确记录 skipped。
