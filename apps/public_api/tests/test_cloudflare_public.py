import httpx
import pytest
from public_mail_api.providers.cloudflare_public import (
    CloudflarePublicAdapter,
    UpstreamError,
)

BASE_URL = "https://worker.example"
KEY = "admin-secret"


def response(payload, status_code=200):
    return httpx.Response(status_code, json=payload)


@pytest.mark.asyncio
async def test_create_address_sends_admin_auth_and_normalizes_contract():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/admin/new_address"
        assert request.headers["x-admin-auth"] == KEY
        return response({"address": "a@example.com", "jwt": "address-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CloudflarePublicAdapter(client).create_address(BASE_URL, KEY)

    assert result.address == "a@example.com"
    assert result.jwt == "address-jwt"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_create_address_maps_unauthorized_and_contract_errors():
    for status, code in [(401, "UPSTREAM_AUTH"), (502, "UPSTREAM_UNAVAILABLE")]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _, status=status, code=code: response({"secret": KEY}, status))
        ) as client:
            with pytest.raises(UpstreamError) as raised:
                await CloudflarePublicAdapter(client).create_address(BASE_URL, KEY)
        assert raised.value.code == code
        assert KEY not in str(raised.value)


@pytest.mark.asyncio
async def test_list_and_detail_use_address_jwt_without_leaking_it():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer address-jwt"
        assert "address-jwt" not in str(request.url)
        if request.url.path.endswith("/42"):
            return response({"id": "42", "raw": "From: a@example.com\\n\\ncode 1234", "created_at": "2026-10-10T00:00:00Z"})
        return response({"results": [{"id": "42", "raw": "From: a@example.com\\n\\ncode 1234", "created_at": "2026-10-10T00:00:00Z"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CloudflarePublicAdapter(client)
        listed = await adapter.list_messages(BASE_URL, "address-jwt", "a@example.com")
        detail = await adapter.get_message(BASE_URL, "address-jwt", "a@example.com", "42")

    assert listed[0]["id"] == "42"
    assert detail["id"] == "42"
    assert len(calls) == 2

@pytest.mark.asyncio
async def test_html_only_mail_is_returned_as_plain_text_without_script_content():
    raw = (
        "From: sender@example.com\n"
        "To: receiver@example.com\n"
        "Subject: Verify\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/html; charset=utf-8\n\n"
        "<p>Code <b>482901</b></p><script>alert('secret')</script>"
    )

    async def handler(request):
        return response({"id": "html-1", "raw": raw, "created_at": "2026-10-10T00:00:00Z"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        message = await CloudflarePublicAdapter(client).get_message(
            BASE_URL, "address-jwt", "receiver@example.com", "html-1"
        )

    assert message["body_text"] == "Code 482901"
    assert "<script>" not in message["body_text"]
    assert "secret" not in message["body_text"]
