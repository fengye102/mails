# OutlookMail Plus（OEP）集成胶水

**这一目录只在「OEP 还没被 mails 取代」期间需要；批次 8 下线 OEP 后可以整目录删除。**

mails 的 `outlook_plus` 来源要管理 OEP 的账号池，但 OEP v2.9.10 的现实是：

| 能力 | 官方接口 | 鉴权 |
|---|---|---|
| 读邮件、号池领取 | `/api/external/*` | `X-API-Key` |
| 账号增删改、刷新令牌、导出 | `/api/accounts*`、`/api/export/*` | **只有登录会话**（Flask session），不接受 `X-API-Key` |

所以 `mails` 侧一条连接同时要用两种鉴权。落地办法是给 OEP 打一个**本地补丁**，让同一把
「对外 API Key」也能通过密码校验 —— 这样连接里只存一把 Key：读走 `X-API-Key`，管理拿它当
登录口令换会话。

## 补丁内容

`make_crypto_patch.py` 从容器里导出原始 `outlook_web/security/crypto.py`，在
`verify_password()` 里追加兜底：bcrypt 不匹配时，把传入值当作对外 API Key 去比对
`settings.external_api_key` 与 `external_api_keys` 多 Key 表（`secrets.compare_digest`）。

选这一个文件的理由：全仓库只有 2 处调用 `verify_password`（`controllers/pages.py` 的 `/login`、
`controllers/accounts.py` 的导出二次验证），正好是需要放行的两处；给 30 个账号路由逐个加
`api_key_required` 装饰器改动面大得多，升级时冲突也多。

## 应用步骤（服务器上）

```bash
cd /opt/outlook-email-plus
mkdir -p patches/backup-v2.9.10
docker cp outlook-email-plus:/app/outlook_web/security/crypto.py patches/backup-v2.9.10/crypto.py
cp <本目录>/make_crypto_patch.py .            # 或直接在 OEP 目录里跑本目录的脚本
python3 make_crypto_patch.py
```

`docker-compose.yml` 的 `volumes:` 里加一行（和已有的 `patches/graph.py` 同款做法）：

```yaml
      - ./patches/crypto.py:/app/outlook_web/security/crypto.py:ro
```

然后 `docker compose up -d`，并用 `curl -s http://127.0.0.1:15001/healthz` 确认起得来。

## 配置对外 API Key

**是 `PUT /api/settings`，不是 POST**（POST 会 405）。需要登录态 + `X-CSRFToken`：

```bash
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36'
# 用登录密码换会话
curl -s -c jar -A "$UA" -H 'Content-Type: application/json' -d '{"password":"<登录密码>"}' \
  http://127.0.0.1:15001/login
CSRF=$(curl -s -b jar -c jar -A "$UA" http://127.0.0.1:15001/api/csrf-token \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["csrf_token"])')
curl -s -b jar -A "$UA" -X PUT -H 'Content-Type: application/json' -H "X-CSRFToken: $CSRF" \
  -d '{"external_api_key":"oep_<随机串>"}' http://127.0.0.1:15001/api/settings
```

现网这把 Key 在 `/root/.hermes/oep_machine_api_key.txt`（0600），配置与验证脚本：
`/root/.hermes/workspace/oep-machine-api/oep_machine_key_setup.py`。

## 验收（四条都要过）

```bash
# 1) Key 走机器接口
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:15001/api/external/health          # 200
# 2) Key 当登录口令
curl -s -H 'Content-Type: application/json' -d "{\"password\":\"$KEY\"}" http://127.0.0.1:15001/login   # success:true
# 3) 登录密码仍然能登（没搞坏 UI）
curl -s -H 'Content-Type: application/json' -d '{"password":"<登录密码>"}' http://127.0.0.1:15001/login   # success:true
# 4) 错密码仍然被拒（补丁不是万能钥匙）
curl -s -H 'Content-Type: application/json' -d '{"password":"nope"}' http://127.0.0.1:15001/login        # 401
```

## 安全边界

- 这把 Key 等于机器管理员凭据：**也能登 OEP 网页 UI**。不要贴进前端、仓库或聊天记录。
- 补丁只加兜底分支，不改变原有 bcrypt 校验路径；Key 未配置时行为与上游一致。
- 升级 OEP 镜像后必须重跑 `make_crypto_patch.py`（上游若改了 `crypto.py` 结构，脚本的锚点
  断言会直接报错，不会静默产出一个坏文件）。

## 对接时的两个必踩坑（已在 mails 侧修掉，别的客户端也会踩）

| 现象 | 真因 | 解法 |
|---|---|---|
| 写操作 400 `CSRF_TOKEN_INVALID`（The CSRF token is missing.） | OEP 的 POST/DELETE 要会话级 `X-CSRFToken`，GET 不要 | 先 `GET /api/csrf-token`，写请求带 `X-CSRFToken` |
| 带了 token 仍 400（**The CSRF session token is missing.**） | 该端点会把 token 写进服务端会话并 `Set-Cookie` **轮换会话 Cookie**；自己拼 Cookie 头又丢掉响应 Cookie，会话里就没有 token | 取 token 的响应 Cookie 必须合并回会话，且要在构造 Cookie 头**之前**取 |
| 列账号卡 ~110 秒后报契约错 | `page_size` 上限 100，且**页码越界被夹回最后一页**（不是返回空页）；「不足一页即结束」在账号数正好是页大小整数倍时无限翻页 | 用 `pagination.total_count` 收尾 |
