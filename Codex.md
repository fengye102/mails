# Mails 项目规则

## 目标与阶段

本项目是一个统一邮箱平台，采用「统一核心 + 来源适配器」架构，不把三个参考仓库的代码或界面直接拼接。

- A：私有统一后台。接入 Outlook/IMAP、Cloudflare 临时邮箱、iCloud 隐藏邮箱；统一索引、检索、阅读邮件和提取验证码。
- B：公开临时邮箱。开放注册、域名与地址配额、滥用控制；与 A 的私人凭据隔离。
- C：库存与取件交付。分配邮箱、签发取件码、设置有效期并留存操作记录。

当前只实施 A。B、C 的设计可以在文档中预留边界，不得提前实现其业务功能。

## 仓库结构

| 路径 | 内容 |
| --- | --- |
| `Codex.md` | 本文件；所有结构与工作流变更先改这里。 |
| `README.md` | 面向使用者的项目简介、开发与部署入口。 |
| `apps/api/` | Python API、数据库迁移、来源适配器及 API 单元测试。 |
| `apps/api/src/mails_api/providers/` | 纯 HTTP 来源适配器和规范化 DTO；按来源 snake_case 命名，共享类型放 base.py；不存持久数据，测试缓存按任务完成后清理。 |
| `apps/api/src/mails_api/services/` | 核心同步与验证码提取服务；snake_case 模块名；无持久运行文件，测试缓存任务后清理。 |
| `apps/web/` | TypeScript 前端及其测试。 |
| `tests/integration/` | 跨 API、数据库和来源模拟服务的集成测试。 |
| `infra/` | Docker Compose、容器构建和部署配置模板。 |
| `scripts/` | 可重复执行的开发、检查和清理脚本；Windows 使用 PowerShell。 |
| `docs/superpowers/specs/` | 设计规格，文件内标注待确认或已确认状态，按 `YYYY-MM-DD-topic-design.md` 命名。 |
| `docs/superpowers/plans/` | 与规格一一对应的实施计划，按 `YYYY-MM-DD-topic.md` 命名。 |
| `docs/decisions/` | 架构决策记录，按 `NNNN-topic.md` 命名。 |
| `docs/runbooks/` | 部署、备份、恢复与故障处理步骤。 |
| `.superpowers/sdd/<plan-basename>/` | 按计划隔离的子代理执行草稿、任务简报、测试报告与审查记录；忽略 Git，计划完成并完成终审后清理该计划目录，其他计划目录不得动。 |

不得创建没有归属说明的顶层目录。运行数据、缓存和密钥不进入源码目录；参考仓库不以 `vendor/` 方式复制进本项目。新增目录前，先更新本节，说明内容、命名和清理规则。

## 组件边界

- 统一核心拥有内部用户、来源连接引用、规范化邮箱与邮件索引、验证码结果、同步状态和统一 API。
- 来源适配器只负责上游鉴权接入、列出邮箱、获取邮件和报告错误；上游独有操作保留在对应适配器边界内，不塞入通用邮件接口。
- 来源服务保管其原有登录会话和凭据；核心仅保存连接标识及必要的加密配置，不记录 Cookie、令牌或邮件敏感内容到日志。
- 上游接口以实际代码和契约测试为准；README 中的能力描述不能代替接口验证。
- 一个来源失败不得阻断其他来源的同步或读取。所有同步必须可重试并按来源消息标识去重。
- B 阶段公开服务与 A 阶段私人来源在凭据、权限和部署入口上隔离。

## 技术与命名

- 后端：Python API；数据库：PostgreSQL；前端：TypeScript。具体框架与最低版本写入经确认的规格。
- 生产部署优先 Linux + Docker Compose；本机开发命令以 Windows PowerShell 为准，不直接照搬 Bash 命令。
- Python 文件、函数、变量使用 `snake_case`；类型与类使用 `PascalCase`。TypeScript 文件按组件 `PascalCase.tsx`、其他模块 `kebab-case.ts`；变量和函数使用 `camelCase`。
- 数据库表、列及 JSON 字段使用 `snake_case`；HTTP 路径使用小写复数名词和连字符。
- 时间在存储与 API 中统一为 UTC ISO 8601，界面按用户时区展示。
- 密钥与环境配置使用全大写 `SNAKE_CASE`，只提交 `.env.example`，不提交真实 `.env`。

## 变更与验收

1. 先改规则或规格，再改实现；设计变更需在对应文档说明原因。
2. 每个独立功能先写失败测试，再写最小实现，并保留可复现的测试命令。
3. API、适配器和前端分别测试；跨来源流程必须有集成测试，真实凭据测试只能在私有环境执行。
4. 未运行的测试不得标记为通过；对上游的假设和已验证行为必须明确区分。
5. 引入或更新第三方代码前，记录来源版本、许可和改动范围。未经核实的许可不作为可再分发依据。

## 清理与数据保留

- `.venv/`、`node_modules/`、构建产物、测试缓存和 `tmp/` 为可重建内容，不提交 Git；仅在相应任务结束后使用明确路径清理。
- `.superpowers/sdd/` 为可重建的本地执行记录，不提交 Git；执行中保留进度台账，终审完成后只清理当前计划对应子目录。
- 数据库、邮件、上传内容、备份和密钥属于持久数据；清理脚本不得自动删除，备份与保留期另见运行手册。
- 删除或移动目录前核实绝对路径位于本项目内；不得对计算出的未核实路径执行递归操作。
- 过期设计不删除，使用新决策记录标明替代关系；临时调查笔记在结论进入规格或决策记录后清理。

## Web 目录约定（A1）
- `apps/web/src/` 放应用入口、内存会话 API 客户端与样式；React 组件使用 PascalCase.tsx，交互测试同名 `.test.tsx`。
- `apps/web/src/components/` 放登录、连接、邮箱、收件箱视图；共享 API 类型保留在 api.ts，避免引入额外状态框架。
- `apps/web/` 放 Vite、TypeScript、npm 清单及锁文件；node_modules、dist 为任务后可重建产物，不提交。

## A1 部署与集成测试文件约定
- `infra/` 使用 compose.yaml、*.Dockerfile、Caddyfile；构建上下文仅允许清单和源码，生产数据只在命名卷，禁止提交密钥或镜像产物。
- `tests/integration/` 使用 conftest.py 与 test_*.py；仅销毁显式配置的 mails_test 前缀 PostgreSQL 数据库中的测试表，测试后清理连接与测试 schema。
- `docs/runbooks/` 使用小写 kebab-case.md；保留部署、恢复和验收步骤，过期步骤由更新替代；备份及密钥不得存于此目录。
- 根目录 `.dockerignore` 限制 Docker 构建上下文，`.env.example` 仅放占位值。

## B1 公开临时邮箱目录约定
- `apps/public_api/`：B1 独立 FastAPI 服务、独立数据库模型、公共 Cloudflare 来源适配器和测试；不导入 `apps/api` 的 A 私有模型或会话。
- `apps/public_web/`：B1 独立公开页面源码与测试；不复用 A 管理后台会话状态。
- `infra/public-api.Dockerfile`、`infra/public-web.Dockerfile`、`infra/public-web.Caddyfile`、`infra/public-compose.yaml`：B1 容器构建、静态服务和独立部署模板；运行数据只进独立 PostgreSQL 命名卷。
- `docs/superpowers/specs/`、`docs/superpowers/plans/`：B1 规格和计划；B1 结束后保留设计记录。B1 测试缓存和虚拟环境按现有清理规则处理。
- B1 不新增顶层运行数据目录；公共邮箱和能力令牌属于持久敏感数据，仅存 B1 数据库。

## C 库存与取件交付目录约定
- `apps/delivery_api/`：C 阶段独立库存、分配、取件码和审计服务；不得导入 A/B1 的应用包或数据库模型。
- `infra/delivery-*.Dockerfile`、`infra/delivery-compose.yaml`：C 独立容器与数据库部署；不把 C 运行数据并入 A/B1 卷。
- `docs/superpowers/specs/`、`docs/superpowers/plans/`：C 设计和实施记录；实现前必须先更新对应文档。
- `docs/runbooks/delivery-mail.md`：C 部署、密钥轮换、库存回收和取件验收；明文取件码不写入日志或文档。
- `infra/delivery-api.Dockerfile`、`infra/delivery-compose.yaml`：C 独立容器与 PostgreSQL 部署模板；不与 A/B1 卷、网络、密钥混用。
- `.env.delivery.example`：C 环境变量占位模板；真实 operator token、加密密钥和数据库密码不入库。


## C 取件交付页目录约定
- `apps/delivery_web/`：C 阶段公开取件页（React + Vite + TypeScript），只调用 `/delivery-api/v1` 的取件接口；取件码与会话令牌仅存在于内存，不写 localStorage、Cookie 或日志。
- `infra/delivery-web.Dockerfile`：先构建 `apps/delivery_web` 再交给 Caddy 静态服务；`infra/delivery-web.Caddyfile` 保留 `/delivery-api/*` 反代以便容器内自测，对外仍由宿主机反代直连 API 以保留真实来源 IP。
- 取件页只展示纯文本正文与验证码，不渲染 HTML；取件码输入框不使用自动填充，避免凭据被浏览器留存。
- `apps/delivery_web/src/` 内组件用 `PascalCase.tsx`，其余模块 `camelCase.ts`，测试与被测文件同名。

## C2 统一取件交付约定（吸收 CYMail fulfillment）

- C 是唯一的库存/取件交付实现；CYMail 的 `internal/fulfillment` 只作能力参照，不再新增功能。
  规格见 `docs/superpowers/specs/2026-10-11-phase-c2-delivery-unification-design.md`。
- 双取件凭证：`code`（一次性，验证即作废）与 `key`（`tok_` 前缀长密钥，可重复验证，重签才失效）。
  两者都只存 SHA-256 摘要，明文只在签发响应返回一次；密钥随取件链接放在 URL fragment，
  服务端与访问日志不得出现明文。
- 邮件落在 `delivery_messages` 表，唯一键 `(mailbox_id, provider_message_id)`，重复推送是幂等更新；
  `provider_message_id` 缺失时由内容指纹派生。**禁止**再把邮件写回邮箱行上的 JSON 列。
- 入库即规范化：HTML 正文用 `delivery_api/htmltext.py` 转纯文本（保留换行，http(s) 锚点保留为
  `文字 (URL)`），并用 `delivery_api/extract.py` 提取 `otp_code` 与 `link`；出库再经 `security.plain_text()`
  兜底。取件响应任何情况下不得含 HTML 标签。
- C 不做上游拉信（上游引擎属批次 7 范围）：邮件由运营/中枢通过幂等推送接口写入；取件响应里的
  `sync` 只如实回报入库总数与最近入库时间，不伪装成实时同步。
- 反枚举双桶限速：提交值摘要桶 + 来源 IP 桶，任一超限 429 `DELIVERY_PICKUP_RETRY_LATER`；
  已知凭证在分配失效时的失败必须累加该凭证的 `failed_attempts`。
- 公开取件路由统一附加 no-store / nosniff / no-referrer / 严格 CSP 响应头。


## 来源管理目录约定（A 的读写来源）
- `apps/api/src/mails_api/sources_admin/`：来源管理适配器，按 kind 一个模块，统一暴露 `build(client) -> SourceAdmin`；错误码只用 `base.py` 里的 `SOURCE_ERROR_CODES`；只做无状态上游 HTTP 调用，不落库、不新增迁移，读邮件链路不受影响。
- `apps/api/src/mails_api/routes/source_<kind>.py`：管理路由，前缀 `/api/v1/sources/<kind>`；读操作 `require_member`，写操作叠加 `require_owner` + `require_csrf`；凭据复用连接的加密 secret，密钥与口令不进响应体、不进日志。
- `apps/web/src/sources/<kind>/`：各来源的管理面板；统一入口是 `apps/web/src/sources/SourceManagementPage.tsx`，后台页签名「来源管理」。
- 新建来源沿用同一套协议与错误码；来源专有动作（刷新令牌、库存同步、导出等）在各自模块追加实现，并在路由层单独暴露。

## 上游来源的机器接口依赖
- `outlook_plus`（OEP v2.9.10）：账号管理接口只有「登录会话」鉴权，写操作还要会话级 `X-CSRFToken`
  （取自 `GET /api/csrf-token`；该端点会轮换会话 Cookie，必须把响应 Cookie 带回写请求）。本仓库的
  连接 `secret` 用**对外 API Key**：读链路走 `X-API-Key`（`/api/external/*`），管理链路拿它当登录口令
  换会话。因此 OEP 侧需挂本地补丁 `patches/crypto.py`（对外 API Key 可通过密码校验），否则管理链路 401。
  补丁生成脚本、应用步骤与验收清单随仓库存档在 `integrations/outlook-email-plus/`（OEP 下线后可整目录删除）。
- `outlook_plus` 分页：`page_size` 上限 100，**页码越界会被上游夹回最后一页**（不是返回空页），结束条件
  必须用 `pagination.total_count`；靠「返回不足一页」判断会无限翻页（账号数恰好是页大小整数倍时必踩）。
- 适配器只映射固定错误码；上游 message 原文、登录口令、会话 Cookie、导出内容都不得进日志或响应体。

## 统一收件箱的实时读信（三来源合一）

- 路由：`GET /api/v1/connections/{connection_id}/live-mail`（列表，`address` 必填，可选
  `query` / `limit` / `offset`）与 `GET .../live-mail/detail`（`address` + `key` 必填）。
  两者只读，统一要求登录成员（`require_member`），不写入任何状态、不改变上游邮箱。
- 读契约复用 `providers.*`：与定时同步同一套解析，所以「实时看到的」与「归档下来的」
  字段永远一致。列表项：`provider_key` / `source_kind` / `sender` / `subject` /
  `snippet` / `received_at` / `verification_code`；详情另加 `recipient` /
  `rfc_message_id` / `body_text` / `link`。上游单条解析失败以 `issues[].code` 回报，
  不隐藏、不猜测。
- CYMail 按「账号 + 别名」寻址：连接必须带 `config.account_id`，缺失即
  `422 CYMAIL_ACCOUNT_ID_REQUIRED`；它的上游只有 `preview`，因此详情正文等同摘要
  （不假装存在正文）。
- 正文一律纯文本：`services.htmltext` 在 provider 层把上游 HTML 转成文本，`<script>`、
  `<style>`、图片与 `mailto:` 链接一律丢弃。**超链接的 http(s) 目标以 `文字 (URL)`
  形式保留**——激活类邮件只给链接，丢掉目标等于把邮件变成废纸；`link` 字段返回正文里
  第一个 http(s) 链接（本地归档的详情同样返回），供前端点击或复制。
- 关键词过滤在服务端按「发件人 + 主题 + 摘要」做本地子串匹配：三家上游都没有通用搜索接口。
- 上游错误码原样映射为 502；连接凭据只用于向上游取数，绝不进响应体、日志与异常消息。
