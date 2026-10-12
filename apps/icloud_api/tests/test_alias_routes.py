"""D4 写操作路由测试：建/停/启用/删除别名（假上游端到端）。

每条写路径都要验两件事：上游确实被调用了、本地库存被正确改动。
错误路径重点验 HTTP 语义（409 容量满 / 429 限速带 Retry-After / 401 会话失效），
这是调用方（C 站点、调度器）唯一能看到的东西。
"""

import base64
import os

import httpx
import pytest

from icloud_api.accounts import encrypt_json
from icloud_api.models import ICloudAccount, ICloudAlias

MANAGE_HOSTS = ("account.apple.com", "appleid.apple.com")
WEB_SERVICE_URL = "https://p68-maildomainws.icloud.com/wm/maildomainws"
COOKIES = {"aosappleid": "user@example.com", "X-APPLE-WEBAUTH-TOKEN": '"webauth-value"'}
HEADERS = {"Authorization": "Bearer operator-test-token"}


def _success(**result: object) -> dict:
    return {"success": True, "result": result}


class FakeUpstream:
    """假上游：apple.com 走新接口，其余走 iCloud Web 旧接口。

    `add_status/add_body` 控制新接口建别名第一步的返回，用来制造各种失败；
    `web_generate_status` 控制旧接口生成候选（用来让旧接口也失败）。
    """

    def __init__(
        self,
        *,
        add_status: int = 200,
        add_body: object | None = None,
        web_rate_limited: bool = False,
        web_capacity_reached: bool = False,
    ) -> None:
        self.add_status = add_status
        self.add_body = (
            add_body if add_body is not None else {"emailAddress": "new-apple@icloud.com"}
        )
        self.web_rate_limited = web_rate_limited
        self.web_capacity_reached = web_capacity_reached
        self.apple_requests: list[str] = []
        self.web_requests: list[str] = []
        # 列别名（`GET /v2/hme/list`）返回的条目；测试按需改。
        self.web_aliases: list[dict] = []

    def _manage(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.apple_requests.append(f"{request.method} {path}")
        if path == "/bootstrap/portal":
            return httpx.Response(200, json={"timeOutInterval": 30})
        if path == "/account/manage/gs/ws/token":
            return httpx.Response(200, headers={"scnt": "scnt-1"}, json={"timeOutInterval": 30})
        if path == "/account/manage":
            return httpx.Response(200, json={"apiKey": "api-key-1"})
        if path == "/account/manage/email/private/add":
            if self.add_status != 200:
                return httpx.Response(
                    self.add_status,
                    headers=(
                        {"Retry-After": "1800"} if self.add_status == 429 else None
                    ),
                    text="too many requests" if self.add_status == 429 else None,
                    json=None if self.add_status == 429 else self.add_body,
                )
            return httpx.Response(200, json=self.add_body)
        if path == "/account/manage/email/private/add/complete":
            return httpx.Response(
                200, json={"emailAddress": "new-apple@icloud.com", "label": "L", "id": "7"}
            )
        if path.endswith(".em"):
            return httpx.Response(200, json={"emailAddress": "new-apple@icloud.com", "label": "L"})
        return httpx.Response(200, json={})

    def _web(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.web_requests.append(f"{request.method} {path}")
        if path.endswith("/validate"):
            return httpx.Response(
                200,
                json={
                    "webservices": {"premiummailsettings": {"url": WEB_SERVICE_URL}},
                    "dsInfo": {"dsid": "1", "primaryEmail": "user@example.com"},
                },
            )
        if path.endswith("/v1/hme/generate"):
            # 旧接口的错误信号在 200 响应体里（success:false），不是 HTTP 状态码——
            # 这点 Go 和 Python 一致，别按 HTTP 429 造夹具。
            if self.web_rate_limited:
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "error": {"errorMessage": "rate limit", "retryAfter": 1800},
                    },
                )
            if self.web_capacity_reached:
                return httpx.Response(
                    200,
                    json={
                        "success": False,
                        "error": {"errorMessage": "total address limit reached"},
                    },
                )
            return httpx.Response(200, json=_success(hme="cand@icloud.com"))
        if path.endswith("/v1/hme/reserve"):
            return httpx.Response(200, json=_success(hme={"hme": "new-web@icloud.com"}))
        if path.endswith("/v1/hme/deactivate"):
            return httpx.Response(200, json=_success(hme="deactivated"))
        if path.endswith("/v1/hme/reactivate"):
            return httpx.Response(200, json=_success(hme="reactivated"))
        if path.endswith("/v2/hme/list"):
            # 列表的别名在 `result.hmeEmails` 下，解析器也认 `_find_first_dict_array` 兜底。
            return httpx.Response(200, json={"result": {"hmeEmails": self.web_aliases}})
        if path.endswith("/v1/hme/delete"):
            return httpx.Response(200, json=_success())
        raise AssertionError(f"unexpected web request: {request.method} {request.url}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host and request.url.host.endswith("apple.com"):
            return self._manage(request)
        return self._web(request)


@pytest.fixture
def fake_upstream():
    return FakeUpstream()


@pytest.fixture
def client_with_upstream(settings, engine, validator, fake_upstream):
    """带假上游 transport 的 client：所有外呼都被拦截。"""
    from fastapi.testclient import TestClient

    from icloud_api.app import create_app

    app = create_app(settings, engine, cookie_validator=validator)
    app.state.icloud_transport = httpx.MockTransport(fake_upstream)
    return TestClient(app)


@pytest.fixture
def data_key() -> bytes:
    return base64.b64decode(base64.b64encode(os.urandom(32)))


def _seed_account(session, data_key: bytes, name: str = "主号") -> ICloudAccount:
    account = ICloudAccount(name=name, real_email="user@example.com", host="icloud.com")
    account.cookies_enc = encrypt_json(data_key, COOKIES)
    account.apple_session_enc = encrypt_json(data_key, {"host": "icloud.com", "cookies": {"aasp": "s"}})
    session.add(account)
    session.flush()
    return account


def _seed_alias(session, account_id: str, address: str, anonymous_id: str) -> ICloudAlias:
    row = ICloudAlias(
        account_id=account_id, anonymous_id=anonymous_id, address=address, state="active"
    )
    session.add(row)
    session.flush()
    return row


# ---- 建别名 ----


def test_create_alias_uses_apple_account_and_records_inventory(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases",
        headers=HEADERS,
        json={"label": "我的标签"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "new-apple@icloud.com"
    assert body["interface"] == "apple_account"
    # 本地库存要补一条，否则 C 站点同步过去是空的
    with Session(engine) as session:
        rows = session.query(ICloudAlias).filter(ICloudAlias.account_id == account_id).all()
        assert [row.address for row in rows] == ["new-apple@icloud.com"]
        assert all(row.state == "active" for row in rows)


def test_create_alias_falls_back_to_icloud_web(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    from sqlalchemy.orm import Session

    fake_upstream.add_status = 401
    fake_upstream.add_body = {"error": {"message": "authentication failed"}}

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases", headers=HEADERS, json={}
    )

    assert response.status_code == 200, response.text
    assert response.json()["interface"] == "icloud_web"
    assert response.json()["email"] == "new-web@icloud.com"
    assert any(p.endswith("/v1/hme/reserve") for p in fake_upstream.web_requests)


def test_create_alias_capacity_reached_is_409(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """总容量已满 → 409，且不回退旧接口（回退也白试，还会耗掉旧接口配额）。"""
    from sqlalchemy.orm import Session

    fake_upstream.add_status = 403
    fake_upstream.add_body = {"error": {"message": "You have reached the maximum number of addresses"}}

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases", headers=HEADERS, json={}
    )

    assert response.status_code == 409
    assert fake_upstream.web_requests == []


def test_create_alias_rate_limited_is_429_with_retry_after(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """两个接口都被限速 → 429 + Retry-After，调用方才知道该等多久。

    只有新接口限速不算完：`alias_service` 会回退旧接口，而回退是能成功的
    （两接口配额独立）。要让错误真的透到路由层，必须让两条路都限速。
    """
    from sqlalchemy.orm import Session

    fake_upstream.add_status = 429
    fake_upstream.web_rate_limited = True

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases", headers=HEADERS, json={}
    )

    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "1800"


def test_create_alias_unknown_account_is_404(client_with_upstream) -> None:
    response = client_with_upstream.post(
        "/icloud-api/v1/accounts/none/aliases", headers=HEADERS, json={}
    )
    assert response.status_code == 404


def test_create_alias_requires_operator_token(client_with_upstream, engine, data_key) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(f"/icloud-api/v1/accounts/{account_id}/aliases", json={})
    assert response.status_code == 401


# ---- 停/启用/删除 ----


def test_deactivate_updates_local_state(client_with_upstream, engine, fake_upstream, data_key) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        _seed_alias(session, account.id, "a@icloud.com", "anon-1")
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases/anon-1/deactivate", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "inactive"
    assert any(p.endswith("/v1/hme/deactivate") for p in fake_upstream.web_requests)
    with Session(engine) as session:
        row = session.query(ICloudAlias).filter(ICloudAlias.anonymous_id == "anon-1").one()
        assert row.state == "inactive"


def test_reactivate_updates_local_state(client_with_upstream, engine, fake_upstream, data_key) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        _seed_alias(session, account.id, "a@icloud.com", "anon-1")
        session.commit()
        account_id = account.id

    response = client_with_upstream.post(
        f"/icloud-api/v1/accounts/{account_id}/aliases/anon-1/reactivate", headers=HEADERS
    )

    assert response.status_code == 200
    assert response.json()["state"] == "active"
    assert any(p.endswith("/v1/hme/reactivate") for p in fake_upstream.web_requests)


def test_delete_removes_local_row_to_avoid_ghost(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """删除后本地行必须一并清掉：留着就是幽灵行，下次同步会当新增加回来。"""
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        _seed_alias(session, account.id, "a@icloud.com", "anon-1")
        session.commit()
        account_id = account.id

    response = client_with_upstream.delete(
        f"/icloud-api/v1/accounts/{account_id}/aliases/anon-1", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["removed_local_rows"] == 1
    assert any(p.endswith("/v1/hme/delete") for p in fake_upstream.web_requests)
    with Session(engine) as session:
        assert session.query(ICloudAlias).filter(ICloudAlias.anonymous_id == "anon-1").count() == 0


def test_capabilities_now_report_write_features_ready(client_with_upstream) -> None:
    response = client_with_upstream.get("/icloud-api/v1/capabilities", headers=HEADERS)
    features = response.json()["features"]
    for name in ("aliases_write", "apple_account_session", "scheduler"):
        assert features[name] == {"shipped": True, "verified_live": False}, name
    # 没做的链路不能跟着变成 true
    for name in ("mail_web", "mail_netease_web"):
        assert features[name]["shipped"] is False, name


# ---- 列别名（C 站库存同步的唯一入口）----


def test_list_aliases_returns_snapshot_and_syncs_local(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """一次 GET 既返回上游列表，也把本地库存补齐——C 站只调这一个口。"""
    from sqlalchemy.orm import Session

    fake_upstream.web_aliases = [
        {"hme": "a@icloud.com", "anonymousId": "anon-a", "label": "A"},
        {"hme": "b@icloud.com", "anonymousId": "anon-b", "state": "inactive"},
    ]

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        _seed_alias(session, account.id, "a@icloud.com", "anon-a")
        session.commit()
        account_id = account.id

    response = client_with_upstream.get(
        f"/icloud-api/v1/accounts/{account_id}/aliases", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["address"] for item in body["aliases"]] == ["a@icloud.com", "b@icloud.com"]
    assert body["synced"] == {"added": 1, "updated": 1, "missing": 0}
    assert body["total"] == 2

    with Session(engine) as session:
        rows = {row.address: row for row in session.query(ICloudAlias).all()}
        assert rows["a@icloud.com"].label == "A"
        assert rows["b@icloud.com"].state == "inactive", "上游停用的别名本地要跟着停用"


def test_list_aliases_marks_ghost_but_keeps_row(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """上游已消失的本地行只标记 missing，行必须还在。"""
    from sqlalchemy.orm import Session

    fake_upstream.web_aliases = [{"hme": "keep@icloud.com", "anonymousId": "anon-keep"}]

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        _seed_alias(session, account.id, "keep@icloud.com", "anon-keep")
        _seed_alias(session, account.id, "gone@icloud.com", "anon-gone")
        session.commit()
        account_id = account.id

    response = client_with_upstream.get(
        f"/icloud-api/v1/accounts/{account_id}/aliases", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    assert response.json()["synced"]["missing"] == 1
    with Session(engine) as session:
        gone = session.query(ICloudAlias).filter(ICloudAlias.address == "gone@icloud.com").one()
        assert gone.state == "missing"


def test_ghost_detection_uses_full_snapshot_not_one_page(
    client_with_upstream, engine, fake_upstream, data_key
) -> None:
    """分页只能作用于响应：只取一页时，另外两页不能被误判成幽灵行。

    这条是防回归的关键——幽灵行判定如果跟着分页走，一次同步就能把整账号库存
    标成 missing，C 站那边一池子邮箱全部不可用。
    """
    from sqlalchemy.orm import Session

    fake_upstream.web_aliases = [
        {"hme": "one@icloud.com", "anonymousId": "anon-1"},
        {"hme": "two@icloud.com", "anonymousId": "anon-2"},
        {"hme": "three@icloud.com", "anonymousId": "anon-3"},
    ]

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.get(
        f"/icloud-api/v1/accounts/{account_id}/aliases?limit=1&offset=0", headers=HEADERS
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["aliases"]) == 1, "分页没生效"
    assert body["total"] == 3
    assert body["synced"] == {"added": 3, "updated": 0, "missing": 0}
    with Session(engine) as session:
        assert session.query(ICloudAlias).count() == 3


def test_list_aliases_unknown_account_is_404(client_with_upstream) -> None:
    response = client_with_upstream.get("/icloud-api/v1/accounts/none/aliases", headers=HEADERS)
    assert response.status_code == 404


def test_list_aliases_requires_operator_token(client_with_upstream, engine, data_key) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        account = _seed_account(session, data_key)
        session.commit()
        account_id = account.id

    response = client_with_upstream.get(f"/icloud-api/v1/accounts/{account_id}/aliases")
    assert response.status_code == 401
