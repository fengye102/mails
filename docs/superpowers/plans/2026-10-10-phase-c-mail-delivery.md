# 阶段 C 邮箱库存与取件交付实施计划

> 本计划在 B1 验收后执行；设计边界先于实现。

**目标：** 交付独立库存、分配、一次性取件码和纯文本取件 API。

**架构：** `apps/delivery_api` 使用独立 PostgreSQL、独立 operator token 和取件会话；不读取 A/B1 数据库。

## 任务

- [ ] 建立独立模型与 Alembic 初始迁移：库存邮箱、分配、取件凭据、审计事件。
- [ ] 实现事务性库存分配、撤销和过期回收；增加并发锁测试。
- [ ] 实现取件码生成、摘要校验、失败退避、一次性消费和短时会话。
- [ ] 实现仅纯文本邮件摘要的取件读取接口与固定错误码。
- [ ] 增加独立 Docker Compose、环境变量模板和 C 运行手册。
- [ ] 完成 API、迁移、ruff、前端/集成环境验收；真实 PostgreSQL 与上游联调缺失时标记 skipped。

## 验证命令

```powershell
$env:PYTHONPATH = 'apps/delivery_api/src'
& 'apps/api/.venv/Scripts/python.exe' -m pytest apps/delivery_api/tests -q
& 'apps/api/.venv/Scripts/python.exe' -m ruff check apps/delivery_api/src apps/delivery_api/tests
```