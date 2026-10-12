from datetime import UTC

import httpx
import pytest

from mails_api.providers.base import ProviderError
from mails_api.providers.cymail import CYMailAdapter, mailbox_key

BASE_URL = "https://cymail.example"
KEY = "internal-key"
ACCOUNT = "acc_1"
EMAIL = "alias@example.com"
MAILBOX_KEY = mailbox_key(ACCOUNT, EMAIL)


def envelope(data):
    return httpx.Response(200, json={"success": True, "data": data})


def message(**overrides):
    return {
        "id": "42",
        "from": "Sender <sender@example.com>",
        "to": EMAIL,
        "subject": "Verification code",
        "date": "2026-10-10T10:20:30+08:00",
        "preview": "Your code is 123456",
        **overrides,
    }


@pytest.mark.asyncio
async def test_list_normalizes_cymail_inbox_and_preserves_body():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/inbox"
        assert request.headers["x-api-key"] == KEY
        assert dict(request.url.params) == {
            "account_id": ACCOUNT,
            "alias": EMAIL,
            "limit": "100",
            "days": "7",
        }
        return envelope(
            {
                "account_id": ACCOUNT,
                "alias": EMAIL,
                "method": "imap",
                "messages": [message(body="Full plain text")],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CYMailAdapter(client)
        result = await adapter.list_messages(BASE_URL, KEY, MAILBOX_KEY)

    assert len(calls) == 1
    assert result.issues == []
    assert result.summaries[0].provider_id == "42"
    assert result.summaries[0].method == "imap"
    assert result.summaries[0].provider_message_key == "imap:42"
    assert result.summaries[0].received_at.tzinfo == UTC

    detail = await adapter.get_message(BASE_URL, KEY, MAILBOX_KEY, result.summaries[0])
    assert detail.body_text == "Your code is 123456"
    assert detail.recipient == EMAIL
    assert detail.rfc_message_id is None


@pytest.mark.asyncio
async def test_missing_body_uses_preview_and_cached_detail_does_not_refetch():
    calls = []

    def handler(request):
        calls.append(request)
        return envelope(
            {
                "account_id": ACCOUNT,
                "alias": EMAIL,
                "method": "web_api",
                "messages": [message(id="web-1", body=None, preview="preview only")],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CYMailAdapter(client)
        result = await adapter.list_messages(BASE_URL, KEY, MAILBOX_KEY)
        detail = await adapter.get_message(BASE_URL, KEY, MAILBOX_KEY, result.summaries[0])

    assert len(calls) == 1
    assert result.summaries[0].method == "web_api"
    assert detail.body_text == "preview only"
    assert detail.snippet == "preview only"


@pytest.mark.asyncio
async def test_bad_item_isolated_with_absolute_issue_index():
    def handler(request):
        return envelope(
            {
                "account_id": ACCOUNT,
                "alias": EMAIL,
                "method": "imap",
                "messages": [message(id="ok"), message(id=None), message(id="ok-2")],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CYMailAdapter(client).list_messages(BASE_URL, KEY, MAILBOX_KEY)

    assert [item.provider_id for item in result.summaries] == ["ok", "ok-2"]
    assert [(item.absolute_index, item.code) for item in result.issues] == [
        (1, "UPSTREAM_CONTRACT")
    ]


@pytest.mark.asyncio
async def test_invalid_envelope_method_is_not_downgraded_to_item_issues():
    def handler(request):
        return envelope(
            {
                "account_id": ACCOUNT,
                "alias": EMAIL,
                "method": "unexpected",
                "messages": [message()],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as raised:
            await CYMailAdapter(client).list_messages(BASE_URL, KEY, MAILBOX_KEY)

    assert raised.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.asyncio
async def test_malformed_alias_item_is_contract_error():
    def handler(request):
        return envelope(
            {
                "account_id": ACCOUNT,
                "aliases": [{"email": 123, "active": True}],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as raised:
            await CYMailAdapter(client).check_mailbox(BASE_URL, KEY, MAILBOX_KEY)

    assert raised.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"message": KEY}, "UPSTREAM_AUTH"),
        (403, {"message": KEY}, "UPSTREAM_AUTH"),
        (200, {"success": False, "message": KEY}, "UPSTREAM_CONTRACT"),
        (200, {"success": True, "data": []}, "UPSTREAM_CONTRACT"),
    ],
)
async def test_errors_are_fixed_and_do_not_leak_credentials(status, body, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as raised:
            await CYMailAdapter(client).list_messages(BASE_URL, KEY, MAILBOX_KEY)

    assert raised.value.code == code
    assert KEY not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_retries_once_then_recovers():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return envelope(
            {"account_id": ACCOUNT, "alias": EMAIL, "method": "imap", "messages": []}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await CYMailAdapter(client).list_messages(BASE_URL, KEY, MAILBOX_KEY)

    assert result.summaries == []
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_probe_accounts_and_check_mailbox_use_fixed_contract():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/accounts":
            return envelope([])
        if request.url.path == "/api/aliases":
            assert request.url.params["account_id"] == ACCOUNT
            return envelope(
                {"account_id": ACCOUNT, "aliases": [{"email": EMAIL, "active": True}]}
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CYMailAdapter(client)
        await adapter.probe(BASE_URL, KEY)
        await adapter.check_mailbox(BASE_URL, KEY, MAILBOX_KEY)

    assert paths == ["/api/accounts", "/api/aliases"]


@pytest.mark.asyncio
async def test_get_message_without_cached_list_finds_matching_message():
    def handler(request):
        return envelope(
            {
                "account_id": ACCOUNT,
                "alias": EMAIL,
                "method": "imap",
                "messages": [message(id="fresh", body="fresh body")],
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = CYMailAdapter(client)
        summary = (await adapter.list_messages(BASE_URL, KEY, MAILBOX_KEY)).summaries[0]
        other = CYMailAdapter(client)
        detail = await other.get_message(BASE_URL, KEY, MAILBOX_KEY, summary)

    assert detail.body_text == "Your code is 123456"


def test_mailbox_key_rejects_ambiguous_values():
    with pytest.raises(ValueError):
        mailbox_key("", EMAIL)
    with pytest.raises(ValueError):
        mailbox_key(ACCOUNT, "")



