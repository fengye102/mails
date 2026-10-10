# C 阶段库存与取件交付运行手册

C 是独立服务，不读取 A/B1 数据库，不复用 A/B1 会话或密钥。

## 本地源码验收

```powershell
$env:PYTHONPATH = 'apps/delivery_api/src'
& 'apps/api/.venv/Scripts/python.exe' -m pytest apps/delivery_api/tests -q
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/delivery_api/src apps/delivery_api/tests apps/delivery_api/alembic
```

## 部署（具备 Docker 时）

```powershell
Copy-Item .env.delivery.example .env.delivery
# 替换所有占位值；不要提交 .env.delivery
 docker compose --env-file .env.delivery -f infra/delivery-compose.yaml config --quiet
 docker compose --env-file .env.delivery -f infra/delivery-compose.yaml up -d --build
```

运营者通过 `Authorization: Bearer DELIVERY_OPERATOR_TOKEN` 登记库存、分配邮箱和签发取件码。明文取件码只在签发响应出现一次；数据库仅保存摘要。取件验证成功后返回短时会话，取件接口只返回地址及纯文本字段。

## 未执行项

当前环境缺少 Docker、PostgreSQL 和真实库存上游，因此不执行容器启动、真实数据库联调、HTTPS 和上游取件回归；这些必须在部署环境完成后再验收。
