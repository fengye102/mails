## C 阶段源码回归记录（2026-10-10）

已执行：

- C API：10 passed
- C ruff：通过
- C compileall：通过
- C Alembic SQLite upgrade/downgrade roundtrip：通过
- B1 API：12 passed
- B1 Web：6 passed，typecheck/build 通过
- A API：255 passed，1 skipped，13 个测试因当前 Windows 临时目录 ACL PermissionError 未能执行
- A ruff：通过

未执行（环境缺失）：

- Docker 镜像构建/启动
- PostgreSQL 真实联调
- Cloudflare/CYMail/Outlook 真实凭据联调
- HTTPS/Caddy 验收

A 的失败项是测试基础设施权限，不是代码断言失败：pytest 无法创建或清理 `C:\Users\longy\AppData\Local\Temp\pytest-of-longy`。

