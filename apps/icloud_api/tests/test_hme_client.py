"""上游客户端测试：用假上游（httpx MockTransport）验证协议细节，不需要真实 Apple 账号。

重点覆盖容易写错、又不好在真机上复现的部分：查询参数、Cookie 拼装、
401/403 不重试、421 换信任 token、限速/容量错误分类、建别名重试会重新校验会话。
"""

import json

import httpx
import pytest
from icloud_api.aliases import (
    AliasError,
    AliasLimitReached,
    AliasRateLimited,
    TrustSessionRequired,
)
from icloud_api.hme_client import (
    HmeClient,
    UpstreamHTTPError,
    is_apple_session_header,
    normalize_host,
    safe_apple_http_error,
    trim_outer_quotes,
    valid_trust_token,
)

SERVICE_URL = "https://p68-maildomainws.icloud.com/wm/maildomainws"
COOKIES = {
    "aosappleid": "user@example.com",
    "X-APPLE-WEBAUTH-TOKEN": '"webauth-value"',
    "X-APPLE-WEBAUTH-HSA-TRUST": '"trust-value"',
}


async def _noop_sleep(_seconds: float) -> None:
    return None


class Upstream:
    """记录请求并按键值返回响应的假上游。"""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for key, response in self.routes.items():
            method, _, path = key.partition(" ")
            if request.method == method and request.url.path.endswith(path):
                return response(request) if callable(response) else response  # type: ignore[return-value]
        # validate 默认成功，省得每个用例都写一遍。
        if request.url.path.endswith("/validate"):
            return httpx.Response(200, json=_validate_payload())
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _validate_payload(service_url: str = SERVICE_URL, **ds_info: object) -> dict:
    info = {"dsid": "123456", "primaryEmail": "user@example.com", "fullName": "Feng Ye"}
    info.update(ds_info)
    return {
        "webservices": {"premiummailsettings": {"url": service_url}},
        "dsInfo": info,
    }


def _client(upstream: Upstream, **overrides: object) -> HmeClient:
    transport = httpx.MockTransport(upstream)
    return HmeClient(
        cookies=dict(COOKIES),
        transport=transport,
        sleep=_noop_sleep,
        **overrides,  # type: ignore[arg-type]
    )


def _success(**extra: object) -> dict:
    payload: dict = {"success": True}
    payload.update(extra)
    return payload


# ---- 会话 ----


@pytest.mark.asyncio
async def test_validate_session_resolves_service_and_identity() -> None:
    upstream = Upstream({})
    client = _client(upstream)
    info = await client.validate_session()
    assert client.service_url == SERVICE_URL
    assert info.dsid == "123456"
    assert info.apple_id == "user@example.com"
    assert info.full_name == "Feng Ye"


@pytest.mark.asyncio
async def test_validate_session_strips_443_only_when_it_ends_the_url() -> None:
    # 原实现是先去掉尾部斜杠、再看是否以 :443 结尾，所以带路径的 URL 不会被动端口。
    upstream = Upstream(
        {"POST /validate": httpx.Response(200, json=_validate_payload("https://p68-maildomainws.icloud.com:443/wm/maildomainws/"))}
    )
    client = _client(upstream)
    await client.validate_session()
    assert client.service_url == "https://p68-maildomainws.icloud.com:443/wm/maildomainws"

    upstream = Upstream({"POST /validate": httpx.Response(200, json=_validate_payload("https://p68-maildomainws.icloud.com:443"))})
    client = _client(upstream)
    await client.validate_session()
    assert client.service_url == "https://p68-maildomainws.icloud.com"


@pytest.mark.asyncio
async def test_validate_session_falls_back_to_cookie_apple_id() -> None:
    payload = _validate_payload()
    payload["dsInfo"] = {}
    upstream = Upstream({"POST /validate": httpx.Response(200, json=payload)})
    client = _client(upstream)
    info = await client.validate_session()
    assert info.apple_id == "user@example.com"


@pytest.mark.asyncio
async def test_validate_session_requires_icloud_plus() -> None:
    upstream = Upstream({"POST /validate": httpx.Response(200, json={"webservices": {}})})
    client = _client(upstream)
    with pytest.raises(AliasError) as info:
        await client.validate_session()
    assert info.value.code == "SESSION_INVALID"
    assert "iCloud+" in info.value.message


# ---- 请求构造 ----


@pytest.mark.asyncio
async def test_maildomainws_uses_hme_build_number() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(200, json=_success(result={"hmeEmails": []}))})
    client = _client(upstream)
    await client.validate_session()
    await client.list_aliases()
    query = dict(httpx.URL(str(upstream.requests[-1].url)).params)
    assert query["clientBuildNumber"] == "2624Build22"
    assert query["clientMasteringNumber"] == "2624Build22"
    assert query["clientId"] == client.client_id
    assert query["dsid"] == "123456"


@pytest.mark.asyncio
async def test_validate_uses_default_build_number() -> None:
    upstream = Upstream({})
    client = _client(upstream)
    await client.validate_session()
    query = dict(httpx.URL(str(upstream.requests[0].url)).params)
    assert query["clientBuildNumber"] == "2624Build13"
    assert query["clientMasteringNumber"] == "2624Build13"
    # validate 时还没拿到 dsid，不该带这个参数。
    assert "dsid" not in query


@pytest.mark.asyncio
async def test_cookie_header_quotes_values_and_mirrors_webauth_headers() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(200, json=_success(result={"hmeEmails": []}))})
    client = _client(upstream)
    await client.validate_session()
    await client.list_aliases()
    request = upstream.requests[-1]
    cookie = request.headers["cookie"]
    # 没引号的补引号，已带引号的保持原样。
    assert 'aosappleid="user@example.com"' in cookie
    assert 'X-APPLE-WEBAUTH-TOKEN="webauth-value"' in cookie
    # WebAuth 值同时作为请求头出现，且去掉外层引号。
    assert request.headers["x-apple-webauth-token"] == "webauth-value"
    assert "x-apple-webauth-token=" not in cookie
    assert request.headers["origin"] == "https://www.icloud.com"
    assert request.headers["content-type"] == "text/plain"  # maildomainws 用 text/plain


@pytest.mark.asyncio
async def test_generic_endpoint_uses_json_content_type() -> None:
    upstream = Upstream({})
    client = _client(upstream)
    await client.validate_session()
    assert upstream.requests[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("icloud.com", "icloud.com"),
        ("icloud.com.cn", "icloud.com.cn"),
        ("https://www.icloud.com.cn/setup", "icloud.com.cn"),
        ("setup.icloud.com.cn", "icloud.com.cn"),
        ("example.com", "icloud.com"),
        ("", "icloud.com"),
    ],
)
def test_normalize_host(host: str, expected: str) -> None:
    assert normalize_host(host) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("X-APPLE-WEBAUTH-TOKEN", True),
        ("x-apple-webauth-hsa-trust", True),
        ("X-Apple-Group", True),
        ("X-APPLE-DS-WEB-SESSION-TOKEN", True),
        ("aosappleid", False),
    ],
)
def test_is_apple_session_header(name: str, expected: bool) -> None:
    assert is_apple_session_header(name) is expected


def test_trim_outer_quotes_and_trust_token_rules() -> None:
    assert trim_outer_quotes('"abc"') == "abc"
    assert trim_outer_quotes("abc") == "abc"
    assert trim_outer_quotes('"') == '"'
    assert valid_trust_token("a" * 32) is True
    assert valid_trust_token("a" * 31) is False
    assert valid_trust_token("a+/_=-" + "b" * 32) is True
    assert valid_trust_token("a" * 32 + "!") is False


# ---- 错误与重试 ----


@pytest.mark.asyncio
async def test_unauthorized_is_not_retried() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(401, json={"error": {"errorMessage": "session expired"}})})
    client = _client(upstream)
    await client.validate_session()
    with pytest.raises(UpstreamHTTPError) as info:
        await client.list_aliases()
    assert info.value.status_code == 401
    assert len(upstream.requests) == 2  # validate + 一次列表，没有重试


@pytest.mark.asyncio
async def test_forbidden_is_not_retried() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(403, text="")})
    client = _client(upstream)
    await client.validate_session()
    with pytest.raises(UpstreamHTTPError) as info:
        await client.list_aliases()
    assert info.value.status_code == 403
    assert len(upstream.requests) == 2


@pytest.mark.asyncio
async def test_server_error_is_retried_then_raised() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(500, text="boom")})
    client = _client(upstream)
    await client.validate_session()
    with pytest.raises(UpstreamHTTPError):
        await client.list_aliases()
    assert len(upstream.requests) == 1 + 3  # validate + 三次尝试


@pytest.mark.asyncio
async def test_trust_challenge_refreshes_trust_cookie_and_retries() -> None:
    new_token = "T" * 40
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] == 1:
            return httpx.Response(421, json={"trustTokens": ["short", new_token]})
        return httpx.Response(200, json=_success(result={"hmeEmails": [{"hme": "a@icloud.com"}]}))

    upstream = Upstream({"GET /v2/hme/list": handler})
    client = _client(upstream)
    await client.validate_session()
    aliases = await client.list_aliases()
    assert [alias.email for alias in aliases] == ["a@icloud.com"]
    assert client.cookies["X-APPLE-WEBAUTH-HSA-TRUST"] == new_token
    assert state["count"] == 2


@pytest.mark.asyncio
async def test_trust_challenge_without_new_token_fails() -> None:
    upstream = Upstream({"GET /v2/hme/list": httpx.Response(421, json={"trustTokens": []})})
    client = _client(upstream)
    await client.validate_session()
    with pytest.raises(TrustSessionRequired) as info:
        await client.list_aliases()
    assert info.value.code == "TRUST_SESSION_REQUIRED"
    assert len(upstream.requests) == 2


@pytest.mark.asyncio
async def test_response_set_cookie_is_absorbed() -> None:
    upstream = Upstream(
        {
            "GET /v2/hme/list": httpx.Response(
                200,
                json=_success(result={"hmeEmails": []}),
                headers={"set-cookie": "X-APPLE-WEBAUTH-TOKEN=refreshed; Path=/"},
            )
        }
    )
    client = _client(upstream)
    await client.validate_session()
    await client.list_aliases()
    assert client.cookies["X-APPLE-WEBAUTH-TOKEN"] == "refreshed"


@pytest.mark.asyncio
async def test_connection_error_is_retried_then_surfaced() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    upstream = Upstream({"GET /v2/hme/list": boom})
    client = _client(upstream)
    await client.validate_session()
    with pytest.raises(AliasError) as info:
        await client.list_aliases()
    assert info.value.code == "UPSTREAM_UNREACHABLE"
    assert len(upstream.requests) == 1 + 3


def test_safe_apple_http_error_sanitizes_and_truncates() -> None:
    body = json.dumps({"error": {"errorMessage": "line1\r\nline2"}})
    error = safe_apple_http_error(500, body)
    assert error.raw_message == "line1  line2"
    long_error = safe_apple_http_error(500, json.dumps({"message": "x" * 500}))
    assert len(long_error.raw_message) == 200
    # 非 JSON 体退化到状态短语。
    assert "Bad Gateway" in safe_apple_http_error(502, "<html>").message


# ---- 别名操作 ----


@pytest.mark.asyncio
async def test_generate_reads_nested_hme_object() -> None:
    upstream = Upstream({"POST /v1/hme/generate": httpx.Response(200, json=_success(result={"hme": {"hme": "cand@icloud.com"}}))})
    client = _client(upstream)
    assert await client.generate() == "cand@icloud.com"


@pytest.mark.asyncio
async def test_generate_reads_email_key_fallback() -> None:
    upstream = Upstream({"POST /v1/hme/generate": httpx.Response(200, json=_success(result={"hme": {"email": "cand2@icloud.com"}}))})
    client = _client(upstream)
    assert await client.generate() == "cand2@icloud.com"


@pytest.mark.asyncio
async def test_reserve_default_label_uses_local_time() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_success(result={"hme": {"hme": "reserved@icloud.com"}}))

    upstream = Upstream({"POST /v1/hme/reserve": handler})
    client = _client(upstream)
    email = await client.reserve("cand@icloud.com")
    assert email == "reserved@icloud.com"
    assert captured["hme"] == "cand@icloud.com"
    assert captured["note"] == "Created by icloud_hme tool"
    assert captured["label"].startswith("Created ")


@pytest.mark.asyncio
async def test_reserve_returns_input_when_result_is_not_a_nested_object() -> None:
    # 原实现只在 result.hme 是对象时才取里面的 hme，字符串形态视为没给结果。
    for payload in (_success(), _success(result={"hme": "ignored@icloud.com"})):
        upstream = Upstream({"POST /v1/hme/reserve": httpx.Response(200, json=payload)})
        client = _client(upstream)
        assert await client.reserve("cand@icloud.com", label="x") == "cand@icloud.com"


@pytest.mark.asyncio
async def test_create_alias_uses_generate_then_reserve() -> None:
    upstream = Upstream(
        {
            "POST /v1/hme/generate": httpx.Response(200, json=_success(result={"hme": "cand@icloud.com"})),
            "POST /v1/hme/reserve": httpx.Response(200, json=_success(result={"hme": {"hme": "final@icloud.com"}})),
        }
    )
    client = _client(upstream)
    result = await client.create_alias("标签")
    assert result.email == "final@icloud.com"
    assert result.label == "标签"
    assert result.created_at.endswith("Z")


@pytest.mark.asyncio
async def test_create_alias_does_not_retry_on_rate_limit() -> None:
    upstream = Upstream(
        {"POST /v1/hme/generate": httpx.Response(200, json={"success": False, "error": {"errorMessage": "rate limit", "retryAfter": 60}})}
    )
    client = _client(upstream)
    with pytest.raises(AliasError) as info:
        await client.create_alias("x", max_retries=5)
    assert isinstance(info.value.__cause__, AliasRateLimited)
    assert len(upstream.requests) == 2  # validate + 一次 generate


@pytest.mark.asyncio
async def test_create_alias_retry_revalidates_session() -> None:
    attempts = {"generate": 0}

    def generate(request: httpx.Request) -> httpx.Response:
        attempts["generate"] += 1
        if attempts["generate"] == 1:
            return httpx.Response(200, json={"success": False, "error": {"errorMessage": "internal"}})
        return httpx.Response(200, json=_success(result={"hme": "cand@icloud.com"}))

    upstream = Upstream(
        {
            "POST /v1/hme/generate": generate,
            "POST /v1/hme/reserve": httpx.Response(200, json=_success(result={"hme": {"hme": "final@icloud.com"}})),
        }
    )
    client = _client(upstream)
    result = await client.create_alias("x", max_retries=3)
    assert result.email == "final@icloud.com"
    # 第一次 generate 失败后 serviceURL 被清空，重试前必须重新 validate。
    assert [path for path in upstream.paths if path.endswith("/validate")] == [
        "/setup/ws/1/validate",
        "/setup/ws/1/validate",
    ]


@pytest.mark.asyncio
async def test_create_alias_gives_up_after_max_retries() -> None:
    upstream = Upstream(
        {"POST /v1/hme/generate": httpx.Response(200, json={"success": False, "error": {"errorMessage": "internal"}})}
    )
    client = _client(upstream)
    with pytest.raises(AliasError) as info:
        await client.create_alias("x", max_retries=2)
    assert info.value.code == "ALIAS_CREATE_FAILED"
    assert "创建别名失败" in info.value.message


@pytest.mark.asyncio
async def test_deactivate_maps_capacity_error() -> None:
    upstream = Upstream(
        {"POST /v1/hme/deactivate": httpx.Response(200, json={"success": False, "error": {"errorMessage": "total address limit"}})}
    )
    client = _client(upstream)
    with pytest.raises(AliasLimitReached):
        await client.deactivate("anon-1")


@pytest.mark.asyncio
async def test_reactivate_sends_anonymous_id() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_success())

    upstream = Upstream({"POST /v1/hme/reactivate": handler})
    client = _client(upstream)
    assert await client.reactivate("anon-7") is True
    assert captured == {"anonymousId": "anon-7"}


@pytest.mark.asyncio
async def test_delete_deactivates_first_when_direct_delete_fails() -> None:
    calls = {"delete": 0}
    order: list[str] = []

    def delete(request: httpx.Request) -> httpx.Response:
        calls["delete"] += 1
        order.append("delete")
        if calls["delete"] == 1:
            return httpx.Response(200, json={"success": False, "error": {"errorMessage": "cannot delete active"}})
        return httpx.Response(200, json=_success())

    def deactivate(request: httpx.Request) -> httpx.Response:
        order.append("deactivate")
        return httpx.Response(200, json=_success())

    upstream = Upstream({"POST /v1/hme/delete": delete, "POST /v1/hme/deactivate": deactivate})
    client = _client(upstream)
    await client.delete("anon-9")
    assert order == ["delete", "deactivate", "delete"]


@pytest.mark.asyncio
async def test_delete_reports_operation_failure_after_retry() -> None:
    upstream = Upstream(
        {
            "POST /v1/hme/delete": httpx.Response(200, json={"success": False, "error": {"errorMessage": "cannot delete"}}),
            "POST /v1/hme/deactivate": httpx.Response(200, json={"success": False, "error": {"errorMessage": "nope"}}),
        }
    )
    client = _client(upstream)
    with pytest.raises(AliasError) as info:
        await client.delete("anon-9")
    assert "deactivate before delete" in info.value.message


@pytest.mark.asyncio
async def test_update_forward_to_rejects_invalid_without_request() -> None:
    upstream = Upstream({})
    client = _client(upstream)
    await client.validate_session()
    before = len(upstream.requests)
    with pytest.raises(AliasError) as info:
        await client.update_forward_to("not-an-email")
    assert info.value.code == "INVALID_FORWARD_TO"
    assert len(upstream.requests) == before


@pytest.mark.asyncio
async def test_update_forward_to_lowercases_target() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_success())

    upstream = Upstream({"POST /v1/hme/updateForwardTo": handler})
    client = _client(upstream)
    await client.update_forward_to("  Main@Example.COM ")
    assert captured == {"forwardToEmail": "main@example.com"}


@pytest.mark.asyncio
async def test_list_forwarding_settings_returns_forwarding_snapshot() -> None:
    upstream = Upstream(
        {
            "GET /v2/hme/list": httpx.Response(
                200,
                json=_success(
                    result={
                        "selectedForwardTo": "main@example.com",
                        "forwardToEmails": ["main@example.com"],
                        "hmeEmails": [{"hme": "a@icloud.com", "anonymousId": "a1", "state": "active"}],
                    }
                ),
            )
        }
    )
    client = _client(upstream)
    settings = await client.list_forwarding_settings()
    assert settings.selected_forward_to == "main@example.com"
    assert [alias.email for alias in settings.aliases] == ["a@icloud.com"]
