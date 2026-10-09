# Mails — A1 私有统一邮箱后台

统一核心 + 来源适配器；A1 接入已配置的 OutlookEmailPlus，支持连接、邮箱登记、同步、纯文本阅读及验证码提取。不提供上游授权、公开注册或发信。一个 API worker，PostgreSQL 16，Python 3.12，Node.js 22。

- [项目规则](Codex.md)
- [已确认 A1 规格](docs/superpowers/specs/2026-10-09-phase-a1-private-hub-design.md)
- [部署与验收](docs/runbooks/deploy.md)
- [备份、恢复与轮换](docs/runbooks/backup-restore.md)

## Windows 开发 / 检查

在仓库根目录 PowerShell 执行；先安装 uv、Node.js 22；开发运行还需要 PostgreSQL 16。使用独立开发数据库，不连接生产库。

```powershell
uv sync --project apps/api --python 3.12 --frozen
uv run --project apps/api --python 3.12 pytest apps/api/tests -q
uv run --project apps/api --python 3.12 pytest tests/integration -q -rs
Push-Location apps/web
npm ci
npm test -- --run
npm run typecheck
npm run build
Pop-Location
git diff --check
```

集成测试要求 `TEST_DATABASE_URL` 指向**空的、名称以 mails_test 开头**的专用 PostgreSQL 数据库；测试会 upgrade、创建 owner、模拟上游、验证双邮箱和重复同步、downgrade 并删除迁移版本表。缺失时明确输出 `SKIPPED: TEST_DATABASE_URL missing`，这不是集成验收通过。静态部署断言通过也不代表 Docker 可用。

开发环境变量（当前 PowerShell 会话；不要提交实际值）：

```powershell
$env:MAILS_DATABASE_URL = 'postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/mails_dev'
$env:MAILS_ENCRYPTION_KEY = 'BASE64_32_RANDOM_BYTES'
$env:MAILS_PUBLIC_ORIGIN = 'http://localhost:5173'
$env:MAILS_UPSTREAM_ORIGINS = '["https://mail.example.com"]'
Push-Location apps/api
uv run alembic upgrade head
uv run python -m mails_api.cli create-owner owner
uv run uvicorn mails_api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
Pop-Location
# 另一个终端进入 apps/web，执行 npm run dev，并访问 http://localhost:5173
```

## Linux 部署

```sh
cp .env.example .env
# 编辑所有占位值；MAILS_DOMAIN 与 MAILS_PUBLIC_ORIGIN 的主机名保持一致
chmod 600 .env
docker compose --env-file .env -f infra/compose.yaml up -d --build
docker compose --env-file .env -f infra/compose.yaml exec api python -m mails_api.cli create-owner owner
```

唯一宿主端口是 TCP 443。详见运行手册的 TLS 前置条件、私网准入、备份及真实实例验收表。A1 尚不等于生产验收完成：当前 Windows 执行环境缺 Docker/PostgreSQL/真实上游实例，相关检查待部署主机执行。
