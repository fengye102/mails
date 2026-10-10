"""Read-only adapter for cloudflare_temp_email's admin mail endpoints."""

import asyncio
import base64
import binascii
import re
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import Parser
from email.utils import getaddresses
from urllib.parse import quote

import httpx
from mails_api.providers.base import (
    MessageListIssue,
    MessageListResult,
    MessageSummary,
    NormalizedMessage,
    ProviderError,
)


def _contract_error() -> ProviderError:
    return ProviderError("UPSTREAM_CONTRACT")


def _text(value: object, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise _contract_error()
    if "\x00" in value:
        raise _contract_error()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _contract_error() from None
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _contract_error()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise _contract_error() from None
    # D1's `datetime('now')` values are UTC but omit an offset.
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _rfc_message_id(message: Message) -> str | None:
    values = message.get_all("Message-ID", [])
    if len(values) != 1 or getattr(values[0], "defects", ()):
        return None
    value = str(values[0]).strip()
    return value if re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", value) else None


def _address_header(value: object) -> str:
    text = _text(value)
    parsed = getaddresses([text])
    if len(parsed) == 1 and parsed[0][1]:
        return parsed[0][1]
    return text


def _parse_raw(raw: str) -> tuple[str, str, str, str, str | None]:
    try:
        message = Parser(policy=policy.default).parsestr(raw)
    except (TypeError, ValueError):
        raise _contract_error() from None
    if message.defects:
        raise _contract_error()
    sender = _address_header(message.get("From", ""))
    recipient = _address_header(message.get("To", ""))
    subject = _text(message.get("Subject", ""))
    try:
        if message.is_multipart():
            parts = list(message.walk())
            plain = next(
                (
                    part.get_content()
                    for part in parts
                    if part.get_content_type() == "text/plain"
                    and not part.is_multipart()
                ),
            )
            body = _text(plain if isinstance(plain, str) else "")
        else:
            body = _text(message.get_content())
    except (LookupError, TypeError, ValueError, UnicodeError):
        raise _contract_error() from None
    return sender, recipient, subject, body, _rfc_message_id(message)


def _decode_raw(value: object) -> str:
    if isinstance(value, str):
        return _text(value, required=True)
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return _text(bytes(value).decode("utf-8"), required=True)
        except UnicodeError:
            raise _contract_error() from None
    # D1 can serialize a BLOB as an array of octets in JSON.
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        try:
            return _text(bytes(value).decode("utf-8"), required=True)
        except UnicodeError:
            raise _contract_error() from None
    if isinstance(value, dict) and set(value) == {"base64"} and isinstance(value["base64"], str):
        try:
            return _text(base64.b64decode(value["base64"], validate=True).decode("utf-8"), required=True)
        except (binascii.Error, UnicodeError):
            raise _contract_error() from None
    raise _contract_error()


def _row_id(row: dict) -> str:
    value = row.get("id", row.get("message_id"))
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise _contract_error()
    return str(value)


class CloudflareTempEmailAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(self, base_url: str, secret: str, path: str, *, params=None):
        for attempt in range(2):
            try:
                async with asyncio.timeout(20):
                    response = await self.client.get(
                        base_url.rstrip("/") + path,
                        headers={
                            "X-Admin-Auth": secret,
                            # The repository's current Worker uses x-admin-auth. Bearer
                            # is sent too for deployments exposing the documented contract.
                            "Authorization": f"Bearer {secret}",
                        },
                        params=params,
                        timeout=httpx.Timeout(20, connect=5),
                        follow_redirects=False,
                    )
            except (httpx.TimeoutException, TimeoutError):
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise ProviderError("UPSTREAM_TIMEOUT") from None
            except httpx.RequestError:
                raise _contract_error() from None
            if response.status_code in (401, 403):
                raise ProviderError("UPSTREAM_AUTH")
            if 500 <= response.status_code <= 599:
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise ProviderError("UPSTREAM_UNAVAILABLE")
            if not 200 <= response.status_code <= 299:
                raise _contract_error()
            return response
        raise _contract_error()

    async def probe(self, base_url: str, secret: str, config: dict | None = None) -> None:
        response = await self._request(base_url, secret, "/health_check")
        if not response.text.strip():
            raise ProviderError("UPSTREAM_UNAVAILABLE")

    async def check_mailbox(
        self, base_url: str, secret: str, address: str, config: dict | None = None
    ) -> str:
        result = await self.list_messages(base_url, secret, address, config)
        if result.issues and not result.summaries:
            raise _contract_error()
        return address

    @staticmethod
    def _parse_summary(item: dict) -> MessageSummary:
        if not isinstance(item, dict):
            raise _contract_error()
        raw = _decode_raw(item.get("raw", item.get("raw_blob")))
        sender, recipient, subject, body, _ = _parse_raw(raw)
        item_id = _row_id(item)
        created_at = _parse_datetime(item.get("created_at"))
        address = _text(item.get("address", ""), required=True)
        if recipient and address.lower() not in recipient.lower():
            raise _contract_error()
        return MessageSummary(
            provider_id=item_id,
            method="cloudflare",
            sender=sender,
            subject=subject,
            snippet=body[:500],
            received_at=created_at,
        )

    async def list_messages(
        self, base_url: str, secret: str, address: str, config: dict | None = None
    ) -> MessageListResult:
        summaries: list[MessageSummary] = []
        issues: list[MessageListIssue] = []
        for offset in range(0, 100, 100):
            response = await self._request(
                base_url,
                secret,
                "/admin/mails",
                params={"address": address, "limit": 100, "offset": offset},
            )
            try:
                payload = response.json()
            except ValueError:
                raise _contract_error() from None
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise _contract_error()
            page = payload["results"]
            if len(page) > 100 or (
                "count" in payload
                and (isinstance(payload["count"], bool) or not isinstance(payload["count"], int))
            ):
                raise _contract_error()
            for position, item in enumerate(page):
                try:
                    summaries.append(self._parse_summary(item))
                except ProviderError as exc:
                    issues.append(MessageListIssue(offset + position, exc.code))
            if len(page) < 100:
                break
        return MessageListResult(summaries=summaries[:100], issues=issues)

    async def get_message(
        self,
        base_url: str,
        secret: str,
        address: str,
        summary: MessageSummary,
        config: dict | None = None,
    ) -> NormalizedMessage:
        response = await self._request(
            base_url,
            secret,
            "/admin/mails/" + quote(summary.provider_id, safe="").replace(".", "%2E"),
        )
        try:
            payload = response.json()
        except ValueError:
            raise _contract_error() from None
        if not isinstance(payload, dict):
            raise _contract_error()
        raw = _decode_raw(payload.get("raw", payload.get("raw_blob")))
        sender, recipient, subject, body, rfc_id = _parse_raw(raw)
        item_id = _row_id(payload)
        if item_id != summary.provider_id:
            raise _contract_error()
        return NormalizedMessage(
            provider_id=item_id,
            method="cloudflare",
            sender=sender,
            subject=subject,
            snippet=body[:500],
            received_at=_parse_datetime(payload.get("created_at")),
            recipient=recipient or address,
            body_text=body,
            rfc_message_id=rfc_id,
        )
