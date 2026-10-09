# A1 部署与验收

## 前置条件

Linux + Docker Engine/Compose，私有访问的服务器、PostgreSQL 命名卷持久存储、可用磁盘、可解析的域名。API/db 没有宿主映射，Web 仅 TCP 443；仍须在防火墙/VPN 层限制访问者，HTTPS 本身不是私网隔离。

Caddy 默认自动申请受信任证书，要求 DNS 指向服务器、443 可完成 ACME TLS-ALPN-01 验证、容器能出站访问 CA；不开放 80，也没有 HTTP 跳转。仅内网且不具备 ACME 入站条件时，部署前给 Caddyfile 站点配置 `tls internal` 并在受管设备安装内部 CA 根证书，或挂载组织签发证书与私钥并配置 `tls CERT KEY`；不要使用 `curl -k` 当作 TLS 验收。保留 caddy_data 卷避免丢失证书和 CA。

检查安装工具（Linux：`command -v docker`；Windows：`Get-Command docker,uv,npm`）；以下 Linux 命令只在 Docker 主机执行。当前开发机未运行 Docker 冒烟。

## 配置与启动

1. 复制 `.env.example` 到 `.env`，权限 0600。用密码管理器生成高熵 DB 密码（推荐十六进制以避免 URI/Compose 转义），填入 POSTGRES_PASSWORD 和 MAILS_DATABASE_URL 的同一密码；DB 主机保持 `db`。主密钥是随机 32 字节的标准 Base64，例如在有 Python 的私有终端运行 `python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"`，直接存入密码管理器及受控配置，不进聊天/日志/Git。
2. MAILS_PUBLIC_ORIGIN 必须精确等于浏览器 HTTPS origin（无路径/尾斜杠），MAILS_DOMAIN 是该主机名。MAILS_UPSTREAM_ORIGINS 是 JSON 字符串数组，显式列出 HTTPS 上游 origin；UI 输入与白名单精确匹配。不要使用通配符。
3. 上游先完成邮箱授权，启用外部 API Key，将 allowed_emails 限定两个验收邮箱；上游公网模式按需允许 API 出口 IP。上游密钥只经本后台 HTTPS 连接表单登记，不放 URL。
4. 执行：

```sh
docker compose --env-file .env -f infra/compose.yaml config --quiet
docker compose --env-file .env -f infra/compose.yaml up -d --build
docker compose --env-file .env -f infra/compose.yaml ps
docker compose --env-file .env -f infra/compose.yaml exec api python -m mails_api.cli create-owner owner
```

迁移在 API 启动前执行，失败则不启动服务。owner 密码交互输入；没有公网首次设置接口。只运行一个 API 容器、一个 worker；60 秒周期同步在同进程中执行。反代下失败登录按用户名及直连代理 IP 共用限速桶，不信任 forwarded headers。避免 `docker compose config`（不带 --quiet）、`docker inspect` 或调试日志输出实际凭据。

## Docker / PostgreSQL 模拟验收

不复用生产 DB；在已经启动的 Compose db 中创建一次性空库：

```sh
docker compose --env-file .env -f infra/compose.yaml exec db sh -c 'createdb -U "$POSTGRES_USER" mails_test_a1'
# 临时容器加入 Compose 网络；源码只读；不修改生产 API 容器。
docker compose --env-file .env -f infra/compose.yaml run --rm --no-deps --user root \
  -v "$PWD:/workspace:ro" api sh -c \
  'uv pip install --python /app/.venv/bin/python pytest && export TEST_DATABASE_URL="${MAILS_DATABASE_URL%/*}/mails_test_a1" && cd /workspace && python -m pytest tests/integration -q -rs -p no:cacheprovider'
```

测试必须报告 private_flow PASSED（不是 SKIPPED）。它以 httpx MockTransport 模拟 HTTP 契约，不替代真实网络/TLS/上游验收；生命周期内运行 PostgreSQL Alembic upgrade/downgrade。拒绝非 PostgreSQL、非 mails_test 前缀或非空库。失败后手工检查专用测试库状态，只有确认无业务数据才重建。Windows 独立 PostgreSQL 可设置 `$env:TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@127.0.0.1:5432/mails_test_a1'` 后运行 README 集成命令。

## 真正的部署与实例验收（必须逐项记录证据）

- Compose ps：db/api 健康，web 运行；容器 build、重启后健康；宿主只监听 443，无 8000/5432/80。
- 浏览器受信任 HTTPS 打开 Web；匿名 GET /api/v1/messages 返回 401；首次 owner 登录成功；登出或到期后失效；缺 CSRF 的写入为 403。
- 对用户提供的私有 OutlookEmailPlus 实例登记连接和两个已授权邮箱；每个地址可读性通过。发一封单一 6 位验证码邮件，连续同步两次，只有一条且可复制。多候选验证码不猜测。
- 过滤邮箱、发件人、主题、时间；阅读纯文本；HTML/script 不执行；Graph/IMAP 身份切换不承诺跨方式去重。
- 在测试上游制造超时、401/403、字段缺失；仅显示固定错误码，一个邮箱失败不破坏另一个。检查数据库密文及响应/日志无原密钥（不复制原值到工单）。
- 在真实浏览器 360px 和桌面宽度检查登录、表单、收件箱、长主题阅读、复制与滚动；jsdom 测试不证明视觉布局。
- 按备份手册在隔离环境恢复 DB+主密钥，验证解密和读取。

上述 Docker/PG/真实实例/视觉/恢复检查在当前开发机均未验；不得用单元测试替代。已知终审分流：非 ASCII API Key 输入、RFC raw Message-ID 边界验证、会话恢复错误提示、阅读栏长主题；Task 9 不修改这些功能，终审决定阻断还是归入 A4。
