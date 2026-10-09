from datetime import timezone

import httpx
import pytest
from mails_api.providers.base import ProviderError
from mails_api.providers.cloudflare_temp_email import CloudflareTempEmailAdapter

BASE_URL = "https://cf-mail.example"
KEY = "admin-key"
EMAIL = "alias@example.com"


RAW = (
    "From: Sender <sender@example.com>\r\n"
    "To: alias@example.com\r\n"
    "Subject: Verification\r\n"
    "Message-ID: <cf-1@example.com>\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Your code is 123456"
)


def row(**overrides):
    return {
        "id": 42,
        "message_id": "db-message-42",
        "source": "smtp",
        "address": EMAIL,
        "raw": RAW,
        "created_at": "2026-10-10T10:20:30+08:00",
        **overrides,
    }


def list_response(results):
    return httpx.Response(200, json={"results": results, "count": len(results)})


@pytest.mark.asyncio
async def test_list_parses_raw_mime_and_uses_admin_auth():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/admin/mails"
        assert request.headers["x-admin-auth"] == KEY
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.url.params["address"] == EMAIL
        assert request.url.params["limit"] == "100"
        assert request.url.params["offset"] == "0"
        return list_response([row()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CloudflareTempEmailAdapter(client).list_messages(
            BASE_URL, KEY, EMAIL
        )

    assert len(calls) == 1
    assert result.issues == []
    assert result.summaries[0].provider_id == "42"
    assert result.summaries[0].method == "cloudflare"
    assert result.summaries[0].sender == "sender@example.com"
    assert result.summaries[0].subject == "Verification"
    assert result.summaries[0].snippet == "Your code is 123456"
    assert result.summaries[0].received_at.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_detail_parses_plain_text_and_never_exposes_html():
    item = row(
        id="id/with?reserved",
        raw=(
            "From: sender@example.com\r\n"
            "To: alias@example.com\r\n"
            "Subject: Hi\r\n"
            "Content-Type: multipart/alternative; boundary=part\r\n"
            "\r\n"
            "--part\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "plain body\r\n"
            "--part\r\n"
            "Content-Type: text/html; charset=utf-8\r\n\r\n"
            "<script>bad()</script><b>html body</b>\r\n"
            "--part--\r\n"
        ),
    )
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/admin/mails":
            return list_response([item])
        assert request.url.raw_path.split(b"?")[0] == b"/admin/mails/id%2Fwith%3Freserved"
        return httpx.Response(200, json=item)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CloudflareTempEmailAdapter(client)
        listed = await adapter.list_messages(BASE_URL, KEY, EMAIL)
        detail = await adapter.get_message(
            BASE_URL, KEY, EMAIL, listed.summaries[0]
        )

    assert len(calls) == 2
    assert detail.body_text == "plain body"
    assert "<script>" not in detail.body_text
    assert detail.recipient == EMAIL
    assert detail.rfc_message_id is None


@pytest.mark.asyncio
async def test_missing_raw_isolated_and_following_item_survives():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: list_response([row(raw=None), row(id=43)])
        )
    ) as client:
        result = await CloudflareTempEmailAdapter(client).list_messages(
            BASE_URL, KEY, EMAIL
        )

    assert [item.provider_id for item in result.summaries] == ["43"]
    assert [(issue.absolute_index, issue.code) for issue in result.issues] == [
        (0, "UPSTREAM_CONTRACT")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"message": KEY}, "UPSTREAM_AUTH"),
        (403, {"message": KEY}, "UPSTREAM_AUTH"),
        (500, {"message": KEY}, "UPSTREAM_UNAVAILABLE"),
        (200, {"results": {}, "count": 0}, "UPSTREAM_CONTRACT"),
    ],
)
async def test_errors_are_fixed_and_credentials_are_not_leaked(status, body, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as raised:
            await CloudflareTempEmailAdapter(client).list_messages(
                BASE_URL, KEY, EMAIL
            )

    assert raised.value.code == code
    assert KEY not in str(raised.value)
    assert len(calls) == (2 if status == 500 else 1)


@pytest.mark.asyncio
async def test_probe_and_mailbox_check_use_health_and_mail_list():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/health_check":
            return httpx.Response(200, text="OK")
        return list_response([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CloudflareTempEmailAdapter(client)
        await adapter.probe(BASE_URL, KEY)
        await adapter.check_mailbox(BASE_URL, KEY, EMAIL)

    assert paths == ["/health_check", "/admin/mails"]
