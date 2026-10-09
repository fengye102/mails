"""Read-only CYMail adapter for the unified mail core."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from mails_api.providers.base import (
    MessageListIssue,
    MessageListResult,
    MessageSummary,
    NormalizedMessage,
    ProviderError,
)

_KEY_PREFIX = "cymail:"
_ALLOWED_METHODS = {"imap", "web_api"}


def mailbox_key(account_id: str, address: str) -> str:
    """Encode CYMail's account and alias into the core mailbox key."""
    if (
        not isinstance(account_id, str)
        or not account_id.strip()
        or not isinstance(address, str)
        or not address.strip()
    ):
        raise ValueError("invalid CYMail mailbox key")
    def encode(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")

    return f"{_KEY_PREFIX}{encode(account_id.strip())}:{encode(address.strip().lower())}"


def parse_mailbox_key(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.startswith(_KEY_PREFIX):
        raise ValueError("invalid CYMail mailbox key")
    parts = value[len(_KEY_PREFIX) :].split(":")
    if len(parts) != 2 or not all(parts):
        raise ValueError("invalid CYMail mailbox key")
    def decode(encoded: str) -> str:
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("invalid CYMail mailbox key") from None
        if not decoded or "\x00" in decoded:
            raise ValueError("invalid CYMail mailbox key")
        return decoded

    account_id, address = (decode(item) for item in parts)
    return account_id, address.lower()


def _contract_error() -> ProviderError:
    return ProviderError("UPSTREAM_CONTRACT")


def _text(data: dict, key: str, *, required: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (required and not value.strip()):
        raise _contract_error()
    if "\x00" in value:
        raise _contract_error()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _contract_error() from None
    return value


def _parse_date(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise _contract_error()
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            raise _contract_error() from None
    if parsed.tzinfo is None:
        raise _contract_error()
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _contract_error() from None


def _split_key(value: str) -> tuple[str, str]:
    try:
        return parse_mailbox_key(value)
    except ValueError:
        raise _contract_error() from None


@dataclass(frozen=True)
class _CachedDetail:
    recipient: str
    body_text: str
    rfc_message_id: str | None


class CYMailAdapter:
    """Read-only adapter for CYMail's internal ``/api/inbox`` endpoint.

    CYMail has no single-message read endpoint.  A list response's optional
    ``body`` is retained for the current adapter instance; when a caller asks
    with a fresh instance, the adapter re-reads the same bounded inbox and
    selects the matching provider ID.
    """

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._details: dict[tuple[str, str, str], _CachedDetail] = {}

    async def _get_data(
        self,
        base_url: str,
        api_key: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> dict:
        for attempt in range(2):
            try:
                async with asyncio.timeout(20):
                    response = await self.client.get(
                        base_url.rstrip("/") + path,
                        headers={"X-API-Key": api_key},
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
            try:
                payload = response.json()
            except ValueError:
                raise _contract_error() from None
            if (
                not isinstance(payload, dict)
                or payload.get("success") is not True
                or not isinstance(payload.get("data"), (dict, list))
            ):
                raise _contract_error()
            return payload["data"]
        raise _contract_error()

    @staticmethod
    def _parse_envelope_messages(data: object) -> tuple[str, str, list[dict]]:
        if not isinstance(data, dict):
            raise _contract_error()
        account_id = _text(data, "account_id", required=True)
        address = _text(data, "alias", required=True).strip().lower()
        method = _text(data, "method", required=True)
        if method not in _ALLOWED_METHODS:
            raise _contract_error()
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise _contract_error()
        if len(messages) > 100:
            raise _contract_error()
        return account_id, address, messages

    @staticmethod
    def _parse_message(item: object, method: str, fallback_address: str) -> tuple[MessageSummary, _CachedDetail]:
        if not isinstance(item, dict):
            raise _contract_error()
        provider_id = _text(item, "id", required=True)
        recipient = _text(item, "to", required=True).strip().lower()
        summary = MessageSummary(
            provider_id=provider_id,
            method=method,
            sender=_text(item, "from"),
            subject=_text(item, "subject"),
            snippet=_text(item, "preview"),
            received_at=_parse_date(item.get("date")),
        )
        # CYMail's stable /api/inbox contract exposes a preview, not a detail
        # body.  Never consume undocumented body/html fields as if complete.
        return summary, _CachedDetail(recipient or fallback_address, summary.snippet, None)

    def _cache_key(self, base_url: str, api_key: str, mailbox: str) -> tuple[str, str, str]:
        return base_url.rstrip("/"), api_key, mailbox

    async def probe(self, base_url: str, api_key: str) -> None:
        data = await self._get_data(base_url, api_key, "/api/accounts")
        if not isinstance(data, list):
            raise _contract_error()

    async def check_mailbox(self, base_url: str, api_key: str, mailbox: str) -> None:
        account_id, address = _split_key(mailbox)
        data = await self._get_data(
            base_url,
            api_key,
            "/api/aliases",
            params={"account_id": account_id},
        )
        if not isinstance(data, dict) or data.get("account_id") != account_id:
            raise _contract_error()
        aliases = data.get("aliases")
        if not isinstance(aliases, list):
            raise _contract_error()
        for item in aliases:
            if not isinstance(item, dict):
                raise _contract_error()
            alias_email = item.get("email")
            if not isinstance(alias_email, str):
                raise _contract_error()
            if alias_email.strip().lower() == address:
                if item.get("active") is not True:
                    raise _contract_error()
                return
        raise _contract_error()

    async def list_messages(
        self, base_url: str, api_key: str, mailbox: str
    ) -> MessageListResult:
        account_id, address = _split_key(mailbox)
        data = await self._get_data(
            base_url,
            api_key,
            "/api/inbox",
            params={"account_id": account_id, "alias": address, "limit": 100, "days": 7},
        )
        response_account, response_address, items = self._parse_envelope_messages(data)
        if response_account != account_id or response_address != address:
            raise _contract_error()
        summaries: list[MessageSummary] = []
        issues: list[MessageListIssue] = []
        cache_key = self._cache_key(base_url, api_key, mailbox)
        for index, item in enumerate(items):
            try:
                summary, detail = self._parse_message(item, _text(data, "method", required=True), address)
            except ProviderError as exc:
                issues.append(MessageListIssue(index, exc.code))
                continue
            summaries.append(summary)
            self._details[(cache_key[0], cache_key[1], summary.provider_message_key)] = detail
        return MessageListResult(summaries=summaries, issues=issues)

    async def get_message(
        self,
        base_url: str,
        api_key: str,
        mailbox: str,
        summary: MessageSummary,
    ) -> NormalizedMessage:
        _account_id, address = _split_key(mailbox)
        cache_key = self._cache_key(base_url, api_key, mailbox)
        detail = self._details.get((cache_key[0], cache_key[1], summary.provider_message_key))
        if detail is None:
            result = await self.list_messages(base_url, api_key, mailbox)
            if any(item.provider_message_key == summary.provider_message_key for item in result.summaries):
                detail = self._details.get((cache_key[0], cache_key[1], summary.provider_message_key))
        if detail is None:
            raise _contract_error()
        if summary.method not in _ALLOWED_METHODS or not summary.provider_id:
            raise _contract_error()
        return NormalizedMessage(
            provider_id=summary.provider_id,
            method=summary.method,
            sender=summary.sender,
            subject=summary.subject,
            snippet=summary.snippet,
            received_at=summary.received_at,
            recipient=detail.recipient or address,
            body_text=detail.body_text,
            rfc_message_id=detail.rfc_message_id,
        )
