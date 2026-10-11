# C 阶段库存与取件交付运行手册

C 是独立服务，不读取 A/B1 数据库，不复用 A/B1 会话或密钥。

## 本地源码验收

```powershell
$env:PYTHONPATH = 'apps/delivery_api/src'
& 'apps/api/.venv/Scripts/python.exe' -m pytest apps/delivery_api/tests -q
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/delivery_api/src apps/delivery_api/tests apps/delivery_api/alembic
```

## 部署（具备 Docker 时）

C Compose 包含独立 PostgreSQL、API 和 Caddy HTTPS 入口；API 启动时先执行 Alembic，运行期间每隔 `DELIVERY_CLEANUP_INTERVAL_SECONDS` 秒回收过期分配。

```powershell
Copy-Item .env.delivery.example .env.delivery
# 替换所有占位值；不要提交 .env.delivery
 docker compose --env-file .env.delivery -f infra/delivery-compose.yaml config --quiet
 docker compose --env-file .env.delivery -f infra/delivery-compose.yaml up -d --build
```

运营者通过 `Authorization: Bearer DELIVERY_OPERATOR_TOKEN` 登记库存、分配邮箱和签发取件码。明文取件码只在签发响应出现一次；数据库仅保存摘要。取件验证成功后返回短时会话，取件接口只返回地址及纯文本字段。

## 未执行项

本轮已在隔离 Compose 项目中完成 A、B1、C 的镜像构建与容器启动检查，并验证了 API healthz 和 Alembic 迁移；测试容器、网络和命名卷已清理。C 的 HTTPS 入口源码已接入 Compose，但没有用真实 DNS/公网证书完成外部 HTTPS 验收。

仍未执行：真实上游库存/邮箱联调、生产域名及 ACME 证书验收、生产数据和密钥配置下的部署。