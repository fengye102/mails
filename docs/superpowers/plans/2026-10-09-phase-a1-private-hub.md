# A1 Private Mail Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付私有统一后台的第一个真实端到端切片：通过 OutlookEmailPlus 外部 API 索引邮件、阅读纯文本正文、提取验证码并显示同步状态。

**Architecture:** FastAPI 负责认证、规范化数据、同步及统一 API；React 只访问本项目 API。OutlookEmailPlus 作为独立上游，适配器通过 `X-API-Key` 读取，所有邮件按来源 ID 去重并存入 PostgreSQL。A2/A3 才加入另外两个来源，B/C 仍是后续独立阶段。

**Tech Stack:** Python 3.12+、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16+、httpx、cryptography、Argon2id；Node.js 22+、React、TypeScript、Vite、Vitest；Docker Compose。

**Spec:** `docs/superpowers/specs/2026-10-09-phase-a1-private-hub-design.md`

## Global Constraints

- 当前工作目录为 `D:\code\Program\Mails`，严格遵守 `Codex.md`；结构变更先改规则。
- A1 只接 OutlookEmailPlus；云临时邮箱、CYMail、公开用户、取件交付不得作为 A1 的“顺手功能”。A1 之后继续 A2→A3→A4→B→C，不把 A1 当作项目完成。
- Python 最低 3.12、Node.js 最低 22、PostgreSQL 最低 16；存储与 API 时间均为 UTC ISO 8601。
- 上游凭据只在服务端 AES-256-GCM 密文保存；日志、响应、URL 不包含密钥。生产仅 HTTPS，API 与数据库不发布公网端口。
- 所有 PowerShell 命令从仓库根执行；使用绝对文件路径或 `-LiteralPath` 处理本地文件。
- 当前 Windows 主机有 Python 3.14、`uv`、Node.js 24、npm、Git，但没有 Docker、`psql` 或本机 PostgreSQL。单元测试可本机运行；PostgreSQL/Compose 冒烟测试必须在有 Docker 的环境执行，未执行前不得声称它们通过。
- 上游接口基于固定提交 `ea531a1f40e7badd0769a46f906213f02bab7c82`；实现前后都运行模拟契约测试，真实实例验收须使用用户提供的私有测试环境。

## File Map

| 文件/目录 | 唯一职责 |
| --- | --- |
| `apps/api/pyproject.toml`、`uv.lock` | 后端依赖与锁定版本。 |
| `apps/api/src/mails_api/settings.py`、`db.py`、`models.py` | 环境配置、数据库连接、六张基础表。 |
| `apps/api/alembic/` | 生产数据库迁移。 |
| `apps/api/src/mails_api/app.py`、`cli.py` | ASGI 组装、首次 owner 创建命令。 |
| `apps/api/src/mails_api/security/` | 密码、服务端会话、CSRF、上游密钥加密。 |
| `apps/api/src/mails_api/providers/` | 规范化数据类型及 OutlookEmailPlus HTTP 适配器。 |
| `apps/api/src/mails_api/services/` | 同步编排与验证码提取。 |
| `apps/api/src/mails_api/routes/` | 本项目 session、connections、mailboxes、messages、sync-runs API。 |
| `apps/api/tests/` | 路由、服务、适配器单元测试及 SQLite 快速测试夹具。 |
| `apps/web/src/` | React 页面、共享 API 客户端和组件。 |
| `apps/web/src/**/*.test.tsx` | 前端交互测试。 |
| `tests/integration/` | PostgreSQL + 模拟上游的端到端测试。 |
| `infra/` | API/Web 镜像、Compose 和仅公开 Web 入口的反代配置。 |
| `README.md`、`docs/runbooks/` | 开发、部署、备份和故障处理。 |

## Shared Test Fixtures

`apps/api/tests/conftest.py` defines `db` as a fresh SQLite in-memory SQLAlchemy session, `client` as `TestClient(create_app(test_settings, engine, upstream_client))`, and a default request `Origin` equal to `test_settings.MAILS_PUBLIC_ORIGIN` (`http://localhost:5173`). `owner` inserts username `owner` with Argon2id hash for password `secret`; `logged_in_owner` logs that owner in; `csrf_header` reads `GET /api/v1/session` and returns `{"X-CSRF-Token": token}`. `mock_outlook` is an `httpx.MockTransport` handling the four OutlookEmailPlus paths with success envelopes. `mailbox` inserts a connected mailbox; `seeded_messages` inserts two messages for filtering; `frozen_now` is a timezone-aware UTC datetime. `fake_adapter` implements the Task 4 methods with one fixed summary/detail and increments `detail_calls` each time its detail method is invoked. These fixtures are created in the first task that needs them, not all at repository setup.

```python
@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with OrmSession(engine) as session:
        yield session
    engine.dispose()
```

---

### Task 1: Backend foundation, schema, migration

**Files:** Create `apps/api/pyproject.toml`, `apps/api/src/mails_api/{__init__,settings,db,models,app}.py`, `apps/api/alembic.ini`, `apps/api/alembic/{env.py,versions/0001_initial.py}`, `apps/api/tests/{conftest,test_schema}.py`.

**Interfaces:** Produce `Settings`, `Base`, `get_db(request: Request)`, `create_app(settings: Settings | None = None, engine: Engine | None = None, upstream_client: httpx.AsyncClient | None = None)`, and SQLAlchemy models `User`, `Session`, `ProviderConnection`, `Mailbox`, `Message`, `SyncRun`. Later tasks import these names exactly.

- [ ] **Step 1: Write the failing schema test.**

```python
from sqlalchemy import create_engine, inspect
from mails_api.models import Base

def test_initial_schema_has_six_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) == {
        "users", "sessions", "provider_connections", "mailboxes", "messages", "sync_runs"
    }
```

- [ ] **Step 2: Run and observe failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_schema.py -q` → fails because `mails_api.models` does not exist.
- [ ] **Step 3: Add exact schema and migration.** Use `Uuid(as_uuid=True)` primary keys, `DateTime(timezone=True)` for timestamps, `LargeBinary` for `encrypted_secret`, unique `(connection_id, provider_mailbox_key)` and `(mailbox_id, provider_message_key)`, and foreign keys from sessions→users, mailboxes→connections, messages→mailboxes, sync_runs→connections/mailboxes. `db.py` exposes `get_db()` yielding a SQLAlchemy `Session`; `app.py` returns a FastAPI app with `GET /healthz` returning `{"status":"ok"}`. Migration `0001_initial.py` creates the same six tables and both unique constraints.

  `pyproject.toml` uses a `src/` package, a lockfile, and these direct dependencies; no floating runtime dependency is installed outside `uv.lock`:

```toml
[project]
name = "mails-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi", "uvicorn[standard]", "pydantic-settings", "sqlalchemy>=2,<3",
                "psycopg[binary]>=3,<4", "alembic", "httpx", "cryptography", "argon2-cffi"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "ruff"]
```

```python
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

  Apply the same mapped-column pattern to the remaining five tables with the fields in the spec; no implicit `create_all` on production startup. `create_app` stores settings, engine and optional upstream HTTP client in `app.state`; `get_db(request)` opens a SQLAlchemy session from that engine and closes it after the request. `Settings` reads `MAILS_DATABASE_URL`, `MAILS_ENCRYPTION_KEY`, `MAILS_PUBLIC_ORIGIN`, and `MAILS_UPSTREAM_ORIGINS` from the environment and fails startup when a required value is absent.
- [ ] **Step 4: Run focused checks.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_schema.py -q` and `uv run --project apps/api --python 3.12 python -m compileall apps/api/src` → pass. Use `alembic upgrade head` on PostgreSQL in Task 9; SQLite schema tests are not migration proof.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): add base schema and health endpoint"`.

### Task 2: Owner bootstrap and revocable session

**Files:** Create `apps/api/src/mails_api/security/{passwords,sessions}.py`, `apps/api/src/mails_api/routes/session.py`, `apps/api/src/mails_api/cli.py`, `apps/api/tests/{test_passwords,test_session}.py`; modify `models.py`, `app.py` only for declared session fields/route registration.

**Interfaces:** Produce `hash_password(password: str) -> str`, `verify_password(hash: str, password: str) -> bool`, `create_session(db, user, now) -> tuple[str, Session]`, `get_current_session(request, db) -> Session`, and `python -m mails_api.cli create-owner USERNAME` (password from interactive prompt, never CLI argument).

- [ ] **Step 1: Write failing password and HTTP tests.**

```python
def test_password_hash_is_argon2id():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "correct horse battery staple")
    assert not verify_password(encoded, "wrong")

def test_logout_revokes_server_session(client, owner):
    assert client.post("/api/v1/session", json={"username": "owner", "password": "secret"}).status_code == 200
    assert client.get("/api/v1/session").status_code == 200
    csrf = client.get("/api/v1/session").json()["csrf_token"]
    assert client.delete("/api/v1/session", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/v1/session").status_code == 401
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_passwords.py apps/api/tests/test_session.py -q` → missing security/routes modules.
- [ ] **Step 3: Implement.** Argon2id via `argon2.PasswordHasher`; `create_session` uses `secrets.token_urlsafe(32)`, stores only `sha256(token).hexdigest()`, independently creates `csrf_token`, sets `expires_at=now+8h`, and returns raw token once for the HttpOnly Cookie. `get_current_session` rejects `revoked_at`, `expires_at <= now`, and `last_seen_at < now-30m`, then updates `last_seen_at`. `GET /session` returns `{user:{id,username,role},csrf_token}` with `Cache-Control: no-store`; `DELETE` marks `revoked_at` before clearing Cookie. Cookie is `SameSite=Lax`, `Path=/`, no Domain, `Secure` iff public origin is HTTPS. Owner CLI refuses a second owner and reads password with `getpass.getpass()` twice.

```python
raw_token = secrets.token_urlsafe(32)
session.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
session.csrf_token = secrets.token_urlsafe(32)
session.expires_at = now + timedelta(hours=8)
```

- [ ] **Step 4: Test expiration.** Add cases for idle 31 minutes, absolute 8 hours, wrong password, missing Cookie, second owner creation; run the same test command and require all pass.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): add owner bootstrap and revocable sessions"`.

### Task 3: CSRF, origin, and route protection

**Files:** Create `apps/api/src/mails_api/security/{csrf,login_limit}.py`, `apps/api/tests/{test_csrf,test_login_limit}.py`; modify `routes/session.py`, `app.py`.

**Interfaces:** Produce `require_csrf(request: Request, session: Session) -> None`, `require_owner(session: Session) -> User`, and `LoginLimiter.record_failure(username: str, peer_ip: str, now: datetime) -> None` / `is_limited(...) -> bool`. Task 5 uses owner/CSRF for writes.

- [ ] **Step 1: Write failing mutation tests.**

```python
def test_logout_rejects_missing_csrf(client, logged_in_owner):
    assert client.delete("/api/v1/session").status_code == 403
    assert client.get("/api/v1/session").status_code == 200

def test_login_rejects_foreign_origin(client, owner):
    response = client.post("/api/v1/session", headers={"Origin": "https://foreign.example"},
                           json={"username": "owner", "password": "secret"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_csrf.py -q` → missing CSRF/origin behavior.
- [ ] **Step 3: Implement.** `require_csrf` reads `X-CSRF-Token` and compares against the current server-side session token with `hmac.compare_digest`; missing/incorrect returns 403. Login requires `Origin`, else `Referer`, matching `MAILS_PUBLIC_ORIGIN` by parsed scheme/host/port, not string prefix; reject both absent in browser HTTP flow. Do not add permissive CORS middleware. Define owner dependency for all administrative write routes. `LoginLimiter` keeps failed attempt timestamps per `(username.casefold(), request.client.host)` in this single worker; after five failures within 15 minutes subsequent requests return 429 for that key, reset on successful login, and do not use `X-Forwarded-For` in A1.

```python
provided = request.headers.get("x-csrf-token", "")
if not provided or not hmac.compare_digest(provided, session.csrf_token):
    raise HTTPException(status_code=403, detail="CSRF_TOKEN_INVALID")
```

- [ ] **Step 4: Test correct CSRF and same origin.** Add `Origin: http://localhost:5173` test with matching configured origin and exact-host negative case (`localhost:5173.evil.example`); rerun focused suite.
  Add five wrong-password requests followed by a 429 and show a different username/peer key remains unaffected; run `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_csrf.py apps/api/tests/test_login_limit.py -q`.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): enforce session CSRF and login origin"`.

### Task 4: OutlookEmailPlus contract adapter

**Files:** Create `apps/api/src/mails_api/providers/{base,outlook_plus}.py`, `apps/api/tests/test_outlook_plus.py`.

**Interfaces:** `OutlookPlusAdapter(client: httpx.AsyncClient)` implements `probe(base_url, api_key)`, `check_mailbox(base_url, api_key, email)`, `list_messages(base_url, api_key, email) -> MessageListResult`, `get_message(base_url, api_key, email, summary) -> NormalizedMessage`. `MessageSummary` contains `provider_id`, `method`, `sender`, `subject`, `snippet`, `received_at`. `NormalizedMessage` adds `recipient`, `body_text`, `rfc_message_id`. `MessageListResult` contains `summaries: list[MessageSummary]` and `issues: list[MessageListIssue]`; each issue contains only zero-based `absolute_index` (skip + position) and fixed safe `code`, with no source content, ID or credentials. Parse failures are isolated per item; envelope/page shape failures still raise `ProviderError`.

- [ ] **Step 1: Write a failing mocked HTTP test for paging and auth.**

```python
@pytest.mark.asyncio
async def test_list_fetches_second_page_even_when_has_more_false():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "test-key"
        skip = int(request.url.params["skip"])
        items = [{"id": str(i), "method": "Graph API", "timestamp": 1760000000,
                  "from_address": "sender@example.com", "subject": "code", "content_preview": "hello"}
                 for i in range(skip, min(skip + 50, 51))]
        return httpx.Response(200, json={"success": True, "code": "OK", "data":
                                          {"emails": items, "has_more": False}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OutlookPlusAdapter(client).list_messages("https://mail.example", "test-key", "a@example.com")
    assert len(result.summaries) == 51
    assert result.issues == []
    assert [int(c.url.params["skip"]) for c in calls] == [0, 50]
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_outlook_plus.py -q` → adapter not defined.
- [ ] **Step 3: Implement exact transport contract.** Set `follow_redirects=False`, 5 s connect/20 s total timeout, and `X-API-Key` header. `probe` calls health/capabilities and requires `message_list` + `message_detail`; `check_mailbox` requires `exists` and `can_read`; `list_messages` calls `top=50,skip=0` and `skip=50` only if first page contains 50 entries, never trusts `has_more`. `get_message` URL-encodes `provider_id` as a single path segment and passes `email` + `folder=inbox` query, uses detail `content` as plain text, parses `Message-ID` only when `raw_content` is a valid RFC message. Reject `{success:false}` even with HTTP 200. Map 401/403 to `UPSTREAM_AUTH`, timeout to `UPSTREAM_TIMEOUT`, malformed shape to `UPSTREAM_CONTRACT`; sanitize error summaries. Retry timeout and 5xx once after 200 ms; never retry 401/403, non-5xx client errors or malformed payloads.

```python
summaries, issues = [], []
for skip in (0, 50):
    data = await self._get_data(base_url, api_key, "/api/external/messages",
                                params={"email": email, "folder": "inbox", "skip": skip, "top": 50})
    page = data["emails"]
    for position, item in enumerate(page):
        try:
            summaries.append(self._parse_summary(item))
        except ProviderError as exc:
            issues.append(MessageListIssue(absolute_index=skip + position, code=exc.code))
    if len(page) < 50:
        break
return MessageListResult(summaries=summaries[:100], issues=issues)
```

- [ ] **Step 4: Expand tests.** Add success-false HTTP 200, 401 without retry, timeout then success, repeated timeout failure, missing ID, malformed timestamp, details with HTML plus plain `content`, and a Graph/IMAP equal numeric ID producing different `provider_message_key`; rerun adapter suite. Add malformed-summary followed by valid-summary and full-page malformed-summary followed by page two regressions; assert valid summaries survive, issue indices are absolute, and page-size decisions use raw entries.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): add OutlookEmailPlus read adapter"`.

### Task 5: Encrypted connection and mailbox management

**Files:** Create `apps/api/src/mails_api/security/crypto.py`, `apps/api/src/mails_api/routes/{connections,mailboxes}.py`, `apps/api/tests/{test_crypto,test_connections}.py`; modify `app.py`.

**Interfaces:** `encrypt_secret(key: bytes, value: str) -> bytes` stores `nonce || ciphertext`; `decrypt_secret(key: bytes, blob: bytes) -> str`. `POST /api/v1/connections` accepts `{kind,name,base_url,api_key}`; `POST /api/v1/connections/{id}/mailboxes` accepts `{address,display_name}`. Later sync task reads records via these models.

- [ ] **Step 1: Write failing tests.**

```python
def test_secret_roundtrip_and_nonce_changes():
    key = bytes(range(32))
    first = encrypt_secret(key, "test-key")
    second = encrypt_secret(key, "test-key")
    assert first != second
    assert decrypt_secret(key, first) == "test-key"

def test_connection_never_echoes_secret(client, logged_in_owner, csrf_header, mock_outlook):
    response = client.post("/api/v1/connections", headers=csrf_header,
        json={"kind":"outlook_plus","name":"Primary","base_url":"https://mail.example","api_key":"test-key"})
    assert response.status_code == 201
    assert "test-key" not in response.text
    assert "test-key" not in client.get("/api/v1/connections").text
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_crypto.py apps/api/tests/test_connections.py -q` → missing functions/routes.
- [ ] **Step 3: Implement.** Decode `MAILS_ENCRYPTION_KEY` as 32-byte base64; AES-GCM encrypt with a fresh 12-byte nonce for each write. Canonicalize `base_url` to an origin and require exact membership in `MAILS_UPSTREAM_ORIGINS`; reject userinfo, path, fragment, redirects and non-HTTPS production origins. Probe before insert. Register mailbox only after upstream `account-status` passes; use lowercase address as `provider_mailbox_key` and unique constraint to make duplicate registration return 409. Responses include IDs/status/last check, not encrypted bytes.

```python
nonce = os.urandom(12)
return nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)
```

- [ ] **Step 4: Test allowlist, duplicate, upstream denial.** Include `https://mail.example.attacker.test`, missing CSRF, API-key-scope denial, duplicate mailbox and DB raw value not containing `test-key`; run suite.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): manage encrypted provider connections and mailboxes"`.

### Task 6: Sync coordinator and verification extraction

**Files:** Create `apps/api/src/mails_api/services/{extract,sync}.py`, `apps/api/src/mails_api/routes/sync_runs.py`, `apps/api/tests/{test_extract,test_sync}.py`; modify `app.py` for one 60-second periodic sync loop.

**Interfaces:** `extract_code(subject: str, body_text: str) -> str | None`; `async sync_mailbox(db, mailbox_id, adapter, now) -> SyncRun`; `POST /api/v1/mailboxes/{id}/sync`, `GET /api/v1/sync-runs` with an optional `mailbox_id` query. Task 7 consumes persisted messages. Consume Task 4 `MessageListResult`: record its safe issues as partial and process every valid summary; fake_adapter returns the same structured result.

- [ ] **Step 1: Write failing extraction and dedupe tests.**

```python
@pytest.mark.parametrize("subject,body,expected", [
    ("Your code", "Verification code: 123456", "123456"),
    ("Invoice 123456", "Order 654321 was paid", None),
    ("Code 123456", "Code 654321", None),
])
def test_extract_code(subject, body, expected):
    assert extract_code(subject, body) == expected

@pytest.mark.asyncio
async def test_repeat_sync_is_idempotent(db, mailbox, fake_adapter, frozen_now):
    await sync_mailbox(db, mailbox.id, fake_adapter, frozen_now)
    await sync_mailbox(db, mailbox.id, fake_adapter, frozen_now)
    assert db.scalar(select(func.count(Message.id))) == 1
    assert fake_adapter.detail_calls == 1
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_extract.py apps/api/tests/test_sync.py -q` → missing services.
- [ ] **Step 3: Implement.** Extraction accepts one 4–8 digit candidate adjacent to `code|验证码|verification|verify`; if conflicting candidates exist return `None`. Sync creates `SyncRun(status="running")`, acquires per-mailbox in-process `asyncio.Lock`, fetches a structured result for at most 100 raw entries, records `result.issues` as partial without discarding `result.summaries`, skips entries older than seven days only on first sync, queries existing `(mailbox_id, method:id)` keys in one statement, fetches detail only for unseen entries, inserts each valid message, records individual parse failures, commits per mailbox, and marks status `success|partial|failed` with safe error code/summary. On a second simultaneous trigger return current run without scheduling another. Background loop invokes the same service every 60 seconds and is cancelled cleanly at shutdown.

```python
key = f"{summary.method}:{summary.provider_id}"
if key in existing_keys:
    continue
detail = await adapter.get_message(base_url, api_key, mailbox.address, summary)
db.add(Message(mailbox_id=mailbox.id, provider_message_key=key,
               body_text=detail.body_text,
               verification_code=extract_code(detail.subject, detail.body_text)))
```

- [ ] **Step 4: Test failures and concurrency.** Add one malformed message followed by one good message (`partial`), one mailbox timeout with another mailbox unaffected, conflicting OTP, upstream auth error, two simultaneous triggers, and no raw credentials/body in `sync_runs`; run focused suite.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): sync mailboxes and extract verification codes"`.

### Task 7: Message listing and detail API

**Files:** Create `apps/api/src/mails_api/routes/messages.py`, `apps/api/tests/test_messages_api.py`; modify `app.py`.

**Interfaces:** `GET /api/v1/messages?mailbox_id=&sender=&subject=&from=&to=&limit=&offset=` returns newest-first summaries; `GET /api/v1/messages/{id}` returns plain text detail and optional code. No HTML response field.

- [ ] **Step 1: Write failing API tests.**

```python
def test_message_filter_and_plain_text_detail(client, logged_in_owner, seeded_messages):
    items = client.get("/api/v1/messages", params={"subject":"verify","limit":20}).json()["items"]
    assert len(items) == 1
    detail = client.get(f"/api/v1/messages/{items[0]['id']}").json()
    assert detail["body_text"] == "Verification code: 123456"
    assert "html_content" not in detail

def test_anonymous_message_list_is_private(client):
    assert client.get("/api/v1/messages").status_code == 401
```

- [ ] **Step 2: Run failure.** `uv run --project apps/api --python 3.12 pytest apps/api/tests/test_messages_api.py -q` → route missing.
- [ ] **Step 3: Implement.** Parameterize SQLAlchemy filters; case-insensitive substring only for sender/subject, mailbox ID exact, UTC inclusive start/exclusive end. Limit 1–100 (default 50), offset 0–10000. Return total count and item list, omit `body_text` from list but include it in detail; escape/serialize text as JSON, never render upstream HTML. Require valid session for both endpoints. Define `escape_like` to escape backslash, `%` and `_` in that order.

```python
def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

query = select(Message).where(Message.subject.ilike(f"%{escape_like(subject)}%", escape="\\"))
query = query.order_by(Message.received_at.desc(), Message.id.desc()).offset(offset).limit(limit)
```

- [ ] **Step 4: Add edge cases.** SQL wildcard `%` treated literally, invalid UTC timestamp returns 422, missing message returns 404, inaccessible session returns 401; run focused suite and `uv run --project apps/api --python 3.12 pytest apps/api/tests -q`.
- [ ] **Step 5: Commit.** `git add apps/api && git commit -m "feat(api): expose private unified message search"`.

### Task 8: Web application and interaction tests

**Files:** Create `apps/web/package.json`, `apps/web/package-lock.json`, `apps/web/tsconfig.json`, `apps/web/vite.config.ts`, `apps/web/index.html`, `apps/web/src/main.tsx`, `apps/web/src/App.tsx`, `apps/web/src/api.ts`, `apps/web/src/components/{Login,Connections,Mailboxes,Inbox}.tsx`, corresponding `*.test.tsx`, and `apps/web/src/styles.css`.

**Interfaces:** Web uses only `/api/v1/*`, `credentials: "same-origin"`, and in-memory CSRF Token from `/session`. It never stores API Key in localStorage or logs.

- [ ] **Step 1: Write failing interaction tests.**

```tsx
it('shows code only for an unambiguous message', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ items: [
    { id: 'm1', subject: 'Verify', verification_code: '123456' },
    { id: 'm2', subject: 'Invoice', verification_code: null },
  ], total: 2 }), { status: 200, headers: { 'Content-Type': 'application/json' } })));
  render(<Inbox />);
  expect(await screen.findByText('123456')).toBeVisible();
  expect(screen.getAllByRole('button', { name: '复制验证码' })).toHaveLength(1);
});
```

- [ ] **Step 2: Run failure.** `npm --prefix apps/web test -- --run` → missing package/component.
- [ ] **Step 3: Implement four views.** From the repo root, run `npm --prefix apps/web install react react-dom` and `npm --prefix apps/web install --save-dev vite typescript vitest @vitejs/plugin-react @testing-library/react @testing-library/jest-dom @types/react @types/react-dom`; commit the resulting `package-lock.json`. `vite.config.ts` proxies `/api` to `http://127.0.0.1:8000` in development. `Login` posts credentials with same-origin request; `App` restores session via `GET /session`; `Connections` adds base URL, API Key and displays status without echoing secret; `Mailboxes` adds address and triggers manual sync; `Inbox` filters by mailbox/sender/subject/time, opens text detail and copies code only when non-null. Shared `api.ts` sends `X-CSRF-Token` on mutations and treats 401 as logout. Use semantic labels, keyboard-accessible controls, loading/empty/error states, responsive two-column inbox layout and no `dangerouslySetInnerHTML`.

```ts
export async function apiFetch(path: string, init: RequestInit = {}, csrfToken?: string) {
  const headers = new Headers(init.headers);
  if (csrfToken && init.method && init.method !== 'GET') headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'same-origin' });
  if (!response.ok) throw new Error(`HTTP_${response.status}`);
  return response.status === 204 ? null : response.json();
}
```

- [ ] **Step 4: Test auth, form, sync and inbox.** Add login failure, connection secret not echoed, duplicate mailbox error, sync partial/error state, filter, detail, copy and mobile viewport tests. Run `npm --prefix apps/web test -- --run`, `npm --prefix apps/web run typecheck`, `npm --prefix apps/web run build`.
- [ ] **Step 5: Commit.** `git add apps/web && git commit -m "feat(web): add private mail management UI"`.

### Task 9: Deploy, runbooks, and end-to-end verification

**Files:** Create `.env.example`, `infra/{compose.yaml,api.Dockerfile,web.Dockerfile,Caddyfile}`, `tests/integration/{conftest,test_private_hub}.py`, `README.md`, `docs/runbooks/{deploy,backup-restore}.md`; modify `Codex.md` first if the exact directory structure changes.

**Interfaces:** `docker compose --env-file .env -f infra/compose.yaml up -d --build` starts Web, API, PostgreSQL with only HTTPS Web port exposed. `python -m mails_api.cli create-owner owner` runs inside the API container after migration.

- [ ] **Step 1: Write failing end-to-end test.** The integration test starts with a mock OutlookEmailPlus server and a fresh PostgreSQL database, creates owner, connects one source, registers two mailboxes, syncs one code-bearing mail twice, then asserts one message, extracted code, and anonymous 401. Do not skip the test silently when `TEST_DATABASE_URL` is absent; report a distinct missing-prerequisite result in the test command/runbook.

```python
def test_private_flow(api_client, session_headers):
    connection = api_client.post("/api/v1/connections",
        json={"kind":"outlook_plus","name":"Test","base_url":"https://mail.example","api_key":"test-key"},
        headers=session_headers).json()
    mailbox = api_client.post(f"/api/v1/connections/{connection['id']}/mailboxes",
                              json={"address":"a@example.com"}, headers=session_headers).json()
    for _ in range(2):
        api_client.post(f"/api/v1/mailboxes/{mailbox['id']}/sync", headers=session_headers)
    assert len(api_client.get("/api/v1/messages").json()["items"]) == 1
```

  `tests/integration/conftest.py` defines `api_client` using `TEST_DATABASE_URL` and a `MockTransport` upstream; it refuses destructive setup unless the database name begins with `mails_test`. It creates schema in that disposable PostgreSQL database, seeds the owner, and closes all clients. `session_headers` logs in and returns both same-origin `Origin` and `X-CSRF-Token`. If `TEST_DATABASE_URL` is absent, the integration suite reports `SKIPPED: TEST_DATABASE_URL missing`, never `PASSED`.

- [ ] **Step 2: Run failure on a Docker-capable host.** `docker compose --env-file .env -f infra/compose.yaml up -d --build` and `uv run --project apps/api --python 3.12 pytest tests/integration -q` → fails before infra/test fixtures exist. On this Windows host, record Docker unavailable rather than calling this pass.
- [ ] **Step 3: Implement deployment.** Compose has `db` with persistent volume, `api` with no host port and migration-before-start command, `web` with Caddy HTTPS entrypoint, and one API worker. `.env.example` names all required variables but contains no real secret. Runbooks provide PowerShell development commands, Linux deployment commands, owner bootstrap, upstream allowed-origin setup, database + encryption-key backup, restore, credential rotation, and safe rollback. README links spec, setup and test commands.

```yaml
services:
  db:
    image: postgres:16
    volumes: ["mails_pgdata:/var/lib/postgresql/data"]
  api:
    build: { context: .., dockerfile: infra/api.Dockerfile }
    depends_on: [db]
    env_file: [../.env]
  web:
    build: { context: .., dockerfile: infra/web.Dockerfile }
    ports: ["80:80", "443:443"]
    depends_on: [api]
volumes:
  mails_pgdata:
```

- [ ] **Step 4: Verify scope.** Run backend tests, frontend tests/typecheck/build, `git diff --check`, PostgreSQL migration upgrade/downgrade on disposable database, Compose health, integration test, and real OutlookEmailPlus instance acceptance. Record each command and its result; mark Docker/real-instance checks unverified if prerequisites are not available.
- [ ] **Step 5: Commit.** `git add .env.example infra tests/integration README.md docs/runbooks Codex.md && git commit -m "ops: package and verify A1 private mail hub"`.

## Plan Self-Review

- Spec sections 1–8 map to Tasks 1–9: model/stack (1), owner/session (2–3), upstream contract (4), connection (5), sync/OTP (6), private read API (7), four Web views (8), deployment/backup/end-to-end (9).
- Names used across tasks are consistent: `OutlookPlusAdapter`, `MessageSummary`, `NormalizedMessage`, `sync_mailbox`, `extract_code`, and `/api/v1/*`.
- A1 is not the end of the user objective. On completion, create A2 Cloudflare, A3 CYMail, A4 hardening, B public service and C fulfillment specs/plans in order under `Codex.md`.
- Current host lacks Docker and live provider credentials; neither integration nor production verification may be reported as complete from unit tests alone.
