# 备份、恢复、轮换与回滚

数据库和 MAILS_ENCRYPTION_KEY 必须作为同一恢复点管理；缺少任一项，已保存的上游凭据不能恢复。数据库包含私人邮件、会话及凭据密文，备份必须加密、限制访问，主密钥另存密码管理器/离线密钥库；不要把备份或密钥写进仓库。记录版本、备份时间、主密钥版本，不记录密钥原值。保留期由运营者确定，至少保留一次已验证恢复点。

## Linux Docker 备份

先确认外部 BACKUP_DIR 是受控绝对路径（例如专用加密盘，不是源码目录），并限制权限。下列命令只在具备 Docker 的部署主机执行：

```sh
umask 077
BACKUP_DIR=/srv/private-mails-backups
mkdir -p "$BACKUP_DIR"
# 停 Web/API 防止备份与凭据写入竞态；DB 保持运行。
docker compose --env-file .env -f infra/compose.yaml stop web api
docker compose --env-file .env -f infra/compose.yaml exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP_DIR/mails.dump"
# 必须检查上一条退出码为 0、文件非空；备份失败则不进入升级。
# 将当前 MAILS_ENCRYPTION_KEY 存入独立受控密钥库，记录匹配的备份标识。
# 同时备份受控部署配置、镜像摘要/源码版本；不要在终端打印 .env。
docker compose --env-file .env -f infra/compose.yaml start api web
```

示例固定文件名需在实际执行时换成唯一 UTC 时间戳，避免覆盖最后可用副本。把 dump 在受控主机上加密后离线复制；PG 自定义 dump 本身不加密。Caddy 数据卷也应做受控备份，特别是 internal CA 模式需保留并保护 CA 私钥。磁盘卷快照不是 DB 一致性备份的替代品。

## 恢复演练 / 灾难恢复

1. 使用隔离机器或全新 Compose 项目与全新数据卷；先保留故障现场，不执行 `down -v`。拉取记录的源码/镜像版本，恢复部署配置及**与 dump 匹配的主密钥**。禁用对真实上游的出站访问直到检查结束。
2. 只启动 db；确认目标为空且不是生产库。将备份解密到受控外部目录后导入：

```sh
docker compose --env-file .env -f infra/compose.yaml up -d db
# 等待 db healthy；恢复到新的空库，不使用 --clean 破坏已有数据。
docker compose --env-file .env -f infra/compose.yaml exec -T db sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner' < "$BACKUP_DIR/mails.dump"
# 恢复的旧会话全部撤销，避免备份使已退出会话复活。
docker compose --env-file .env -f infra/compose.yaml exec -T db sh -c \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP;"'
docker compose --env-file .env -f infra/compose.yaml up -d api web
```

3. 核对迁移版本、连接/邮箱/邮件数量，owner 重新登录，确认历史纯文本读取。允许连接测试上游后验证凭据可解密并同步；在隔离环境完成恢复演练才算备份可用。主密钥不匹配时停止 API，恢复正确密钥，不生成新密钥试图修复旧密文。

## 轮换

- DB 密码：维护窗口停 API/Web，先备份；在受控 psql 会话使用交互 `\password` 更新角色密码，同步私有 .env 的 POSTGRES_PASSWORD 和 MAILS_DATABASE_URL，重建 API，再验证。已有 PG 卷不会因为修改 POSTGRES_PASSWORD 自动改密码。
- 上游 API Key：A1 没有编辑连接/密钥轮换接口。先在上游创建新受限 key，经后台建立新连接并重新登记邮箱验证，随后在维护窗口停止 API，在上游撤销旧 key 后重启。旧连接仍会产生鉴权失败状态；旧邮箱数据保留，新的连接属于独立去重范围，可能产生跨连接重复项。需要无重复/无旧连接告警的迁移时，先规划经审查的数据迁移，不临时修改密文。
- 加密主密钥：A1 没有在线重加密工具，禁止直接替换值。需要停服务并对全部 encrypted_secret 做原子重加密、回滚与恢复演练；作为独立受审任务处理。此任务完成前继续使用现有主密钥并保护备份。
- owner 密码：A1 CLI 仅用于首次创建，已有 owner 不可重复创建。密码重置工具尚未交付；账号泄露时先隔离入口、撤销所有 sessions 并安排受审重置流程，不把删除 owner 当作轮换。

## 安全回滚

部署前保存当前镜像摘要、源码版本、DB dump 与匹配主密钥。先在隔离环境验证旧镜像与当前 schema 是否兼容；兼容则停 API/Web 并回到已知镜像。不兼容时恢复完整备份到新卷并切换，明确恢复点后的邮件/配置变化可能丢失。不要对生产库盲目 `alembic downgrade base`，它会删除业务表。集成测试的 downgrade 仅针对空的 disposable mails_test 库。所有恢复/回滚必须记录实际命令、时间、影响及验证结果。
