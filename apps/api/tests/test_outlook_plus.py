from datetime import UTC, datetime

import httpx
import pytest

from mails_api.providers.base import ProviderError
from mails_api.providers.outlook_plus import OutlookPlusAdapter

BASE_URL = "https://mail.example"
KEY = "test-key"
EMAIL = "a@example.com"


def summary(**overrides):
    return {
        "id": "1",
        "method": "Graph API",
        "timestamp": 1760000000,
        "from_address": "sender@example.com",
        "subject": "code",
        "content_preview": "hello",
        **overrides,
    }


def ok(data):
    return httpx.Response(200, json={"success": True, "code": "OK", "data": data})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,skips", [(0, [0]), (49, [0]), (51, [0, 50]), (150, [0, 50])]
)
async def test_paging_uses_page_size_not_has_more(count, skips):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == KEY
        assert request.url.path == "/api/external/messages"
        assert request.url.params["email"] == EMAIL
        assert request.url.params["folder"] == "inbox"
        assert request.url.params["top"] == "50"
        assert request.extensions["timeout"]["connect"] == 5
        skip = int(request.url.params["skip"])
        return ok(
            {
                "emails": [
                    summary(id=str(i)) for i in range(skip, min(skip + 50, count))
                ],
                "has_more": count < 50,
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert len(result.summaries) == min(count, 100)
    assert [int(c.url.params["skip"]) for c in calls] == skips
    assert result.issues == []
    if result.summaries:
        assert result.summaries[0].received_at == datetime.fromtimestamp(
            1760000000, UTC
        )
        assert result.summaries[0].sender == "sender@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (200, {"success": False, "code": "ERROR", "message": KEY}, "UPSTREAM_CONTRACT"),
        (401, {"message": KEY}, "UPSTREAM_AUTH"),
        (403, {"message": KEY}, "UPSTREAM_AUTH"),
        (400, {"message": KEY}, "UPSTREAM_CONTRACT"),
        (302, {"message": KEY}, "UPSTREAM_CONTRACT"),
        (200, [], "UPSTREAM_CONTRACT"),
        (200, {"success": True, "data": {}}, "UPSTREAM_CONTRACT"),
    ],
)
async def test_invalid_responses_are_sanitized_and_not_retried(status, body, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json=body, headers={"Location": "https://other.example"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ProviderError) as exc:
            await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert exc.value.code == code
    assert KEY not in str(exc.value)
    assert EMAIL not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,recover,code",
    [
        ("timeout", True, None),
        ("timeout", False, "UPSTREAM_TIMEOUT"),
        ("5xx", True, None),
        ("5xx", False, "UPSTREAM_CONTRACT"),
    ],
)
async def test_transient_errors_retry_once(failure, recover, code):
    calls = []

    def handler(request):
        calls.append(request)
        if recover and len(calls) == 2:
            return ok({"emails": []})
        if failure == "timeout":
            raise httpx.ReadTimeout(KEY, request=request)
        return httpx.Response(503, text=KEY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OutlookPlusAdapter(client)
        if code:
            with pytest.raises(ProviderError) as exc:
                await adapter.list_messages(BASE_URL, KEY, EMAIL)
            assert exc.value.code == code
            assert KEY not in str(exc.value)
            assert exc.value.__cause__ is None
        else:
            result = await adapter.list_messages(BASE_URL, KEY, EMAIL)
            assert result.summaries == [] and result.issues == []
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        summary(id=""),
        summary(id=None),
        summary(timestamp="bad"),
        summary(timestamp=True),
        summary(timestamp=1e100),
        summary(method=""),
        summary(from_address=[]),
        None,
    ],
)
async def test_malformed_summary_is_contract_error(item):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: ok({"emails": [item]}))
    ) as client:
        result = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert result.summaries == []
    assert [(issue.absolute_index, issue.code) for issue in result.issues] == [
        (0, "UPSTREAM_CONTRACT")
    ]


@pytest.mark.asyncio
async def test_reading_methods_keep_numeric_ids_distinct():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: ok(
                {"emails": [summary(id="42"), summary(id="42", method="IMAP (New)")]}
            )
        )
    ) as client:
        messages = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert (
        messages.summaries[0].provider_message_key
        != messages.summaries[1].provider_message_key
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "features,valid",
    [
        (["message_list", "message_detail"], True),
        (["message_list"], False),
        ({"message_list": True}, False),
    ],
)
async def test_probe_checks_health_and_required_features(features, valid):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["x-api-key"] == KEY
        return ok(
            {"status": "ok", "database": "ok"}
            if request.url.path.endswith("health")
            else {"features": features}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if valid:
            assert await OutlookPlusAdapter(client).probe(BASE_URL, KEY) is None
        else:
            with pytest.raises(ProviderError):
                await OutlookPlusAdapter(client).probe(BASE_URL, KEY)
    assert calls == ["/api/external/health", "/api/external/capabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,valid",
    [
        ({"exists": True, "can_read": True}, True),
        ({"exists": False, "can_read": True}, False),
        ({"exists": True, "can_read": False}, False),
        ({"exists": True}, False),
        ({"exists": 1, "can_read": "true"}, False),
    ],
)
async def test_check_mailbox_requires_exists_and_readable(data, valid):
    def handler(request):
        assert request.url.path == "/api/external/account-status"
        assert request.url.params["email"] == EMAIL
        return ok(data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if valid:
            assert (
                await OutlookPlusAdapter(client).check_mailbox(BASE_URL, KEY, EMAIL)
                is None
            )
        else:
            with pytest.raises(ProviderError):
                await OutlookPlusAdapter(client).check_mailbox(BASE_URL, KEY, EMAIL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "From: sender@example.com\r\nTo: a@example.com\r\nMessage-ID: <valid@example.com>\r\n\r\nBody",
            "<valid@example.com>",
        ),
        ("<html>Message-ID: <fake@example.com></html>", None),
        ("Message-ID: not-valid\n\nBody", None),
        ("Message-ID: <one@example.com>\nMessage-ID: <two@example.com>\n\nBody", None),
        ("Broken header\nMessage-ID: <fake@example.com>\n\nBody", None),
        ("", None),
    ],
)
async def test_details_encode_single_segment_and_use_plain_content(raw, expected):
    item = summary(id="a/b+?=%#")

    def handler(request):
        if request.url.path == "/api/external/messages":
            return ok({"emails": [item]})
        assert (
            request.url.raw_path.split(b"?")[0]
            == b"/api/external/messages/a%2Fb%2B%3F%3D%25%23"
        )
        assert request.url.params["email"] == EMAIL
        assert request.url.params["folder"] == "inbox"
        return ok(
            {
                **item,
                "content": "plain body",
                "html_content": "<script>bad()</script>",
                "raw_content": raw,
                "to_address": EMAIL,
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OutlookPlusAdapter(client)
        summaries = await adapter.list_messages(BASE_URL, KEY, EMAIL)
        result = await adapter.get_message(BASE_URL, KEY, EMAIL, summaries.summaries[0])
    assert result.body_text == "plain body"
    assert result.recipient == EMAIL
    assert result.snippet == "hello"
    assert result.rfc_message_id == expected


@pytest.mark.asyncio
async def test_huge_integer_timestamp_is_sanitized_contract_error():
    item = summary(timestamp=10**1000)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: ok({"emails": [item]}))
    ) as client:
        result = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert result.summaries == []
    assert [(issue.absolute_index, issue.code) for issue in result.issues] == [
        (0, "UPSTREAM_CONTRACT")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id,segment", [(".", b"%2E"), ("..", b"%2E%2E")])
async def test_dot_ids_remain_single_path_segment(provider_id, segment):
    item = summary(id=provider_id)

    def handler(request):
        if request.url.path == "/api/external/messages":
            return ok({"emails": [item]})
        assert (
            request.url.raw_path.split(b"?")[0] == b"/api/external/messages/" + segment
        )
        return ok({**item, "to_address": EMAIL, "content": "body", "raw_content": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OutlookPlusAdapter(client)
        result = await adapter.list_messages(BASE_URL, KEY, EMAIL)
        await adapter.get_message(BASE_URL, KEY, EMAIL, result.summaries[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "health",
    [{}, {"status": "ok", "database": "error"}, {"status": "error", "database": "ok"}],
)
async def test_probe_rejects_unhealthy_or_malformed_health(health):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: ok(
                health
                if request.url.path.endswith("health")
                else {"features": ["message_list", "message_detail"]}
            )
        )
    ) as client:
        with pytest.raises(ProviderError) as exc:
            await OutlookPlusAdapter(client).probe(BASE_URL, KEY)
    assert exc.value.code == ("UPSTREAM_UNAVAILABLE" if health else "UPSTREAM_CONTRACT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"id": "other"},
        {"method": "IMAP (New)"},
        {"content": None},
        {"to_address": []},
        {"raw_content": []},
        {"timestamp": "bad"},
    ],
)
async def test_malformed_detail_never_becomes_a_normalized_message(changes):
    item = summary()

    def handler(request):
        if request.url.path == "/api/external/messages":
            return ok({"emails": [item]})
        return ok(
            {
                **item,
                "content": "plain",
                "raw_content": "",
                "to_address": EMAIL,
                **changes,
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OutlookPlusAdapter(client)
        messages = await adapter.list_messages(BASE_URL, KEY, EMAIL)
        with pytest.raises(ProviderError) as exc:
            await adapter.get_message(BASE_URL, KEY, EMAIL, messages.summaries[0])
    assert exc.value.code == "UPSTREAM_CONTRACT"


@pytest.mark.asyncio
async def test_retry_waits_200ms(monkeypatch):
    waits = []
    calls = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr("mails_api.providers.outlook_plus.asyncio.sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else ok({"emails": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
        ).summaries == []
    assert waits == [0.2]


@pytest.mark.asyncio
async def test_non_json_payload_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=KEY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert exc.value.code == "UPSTREAM_CONTRACT"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_total_deadline_maps_to_timeout_and_retries_once(monkeypatch):
    import asyncio

    original_timeout = asyncio.timeout
    deadlines = []
    calls = []

    def shortened_timeout(delay):
        deadlines.append(delay)
        return original_timeout(0.001)

    monkeypatch.setattr(
        "mails_api.providers.outlook_plus.asyncio.timeout", shortened_timeout
    )

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return ok({"emails": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert exc.value.code == "UPSTREAM_TIMEOUT"
    assert deadlines == [20, 20]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_bad_summary_preserves_following_valid_summary_and_safe_issue():
    from dataclasses import asdict

    bad = summary(id="", subject=KEY, content_preview=EMAIL)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: ok({"emails": [bad, summary(id="good")]})
        )
    ) as client:
        result = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert [item.provider_id for item in result.summaries] == ["good"]
    assert [asdict(issue) for issue in result.issues] == [
        {"absolute_index": 0, "code": "UPSTREAM_CONTRACT"}
    ]
    assert KEY not in repr(result.issues)
    assert EMAIL not in repr(result.issues)


@pytest.mark.asyncio
async def test_full_page_with_bad_summary_still_fetches_second_page():
    calls = []

    def handler(request):
        skip = int(request.url.params["skip"])
        calls.append(skip)
        if skip == 0:
            page = [summary(id="")] + [summary(id=str(i)) for i in range(1, 50)]
        else:
            page = [summary(id="50"), summary(timestamp="bad"), summary(id="52")]
        return ok({"emails": page, "has_more": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert calls == [0, 50]
    assert [item.provider_id for item in result.summaries] == [
        str(i) for i in range(1, 51)
    ] + ["52"]
    assert [(issue.absolute_index, issue.code) for issue in result.issues] == [
        (0, "UPSTREAM_CONTRACT"),
        (51, "UPSTREAM_CONTRACT"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [None, {}, "bad", [summary()] * 51])
async def test_page_shape_failure_still_raises_provider_error(page):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: ok({"emails": page}))
    ) as client:
        with pytest.raises(ProviderError) as exc:
            await OutlookPlusAdapter(client).list_messages(BASE_URL, KEY, EMAIL)
    assert exc.value.code == "UPSTREAM_CONTRACT"
