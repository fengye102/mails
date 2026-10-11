"""Read-only OutlookEmailPlus adapter for the pinned external HTTP contract."""

import asyncio
import math
import re
from datetime import datetime, timezone
from email import policy
from email.parser import Parser
from urllib.parse import quote

import httpx
from mails_api.providers.base import (
    MessageListIssue,
    MessageListResult,
    MessageSummary,
    NormalizedMessage,
    ProviderError,
)
from mails_api.services.htmltext import html_to_text, looks_like_html


def _plain_body(data: dict) -> str:
    """OEP 的 ``content`` 实测是整页 HTML；阅读器与归档只接受纯文本。"""
    body = _text(data, "content")
    return html_to_text(body) if looks_like_html(body) else body


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


def _rfc_message_id(raw: str) -> str | None:
    # Graph may put HTML/plain body here rather than an RFC message.
    if not raw or not re.search(r"\r?\n\r?\n", raw):
        return None
    try:
        message = Parser(policy=policy.default).parsestr(raw)
        ids = message.get_all("Message-ID", [])
        if message.defects or len(ids) != 1:
            return None
        value = str(ids[0]).strip()
        if getattr(ids[0], "defects", ()):
            return None
        if re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", value):
            return value
    except (ValueError, TypeError):
        pass
    return None


class OutlookPlusAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get_data(
        self, base_url: str, api_key: str, path: str, *, params: dict | None = None
    ) -> dict:
        for attempt in range(2):
            try:
                # httpx's read timeout is an inactivity limit, not a total deadline.
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
            if 500 <= response.status_code <= 599 and attempt == 0:
                await asyncio.sleep(0.2)
                continue
            if not 200 <= response.status_code <= 299:
                raise _contract_error()
            try:
                payload = response.json()
            except ValueError:
                raise _contract_error() from None
            if (
                not isinstance(payload, dict)
                or payload.get("success") is not True
                or payload.get("code") != "OK"
                or not isinstance(payload.get("data"), dict)
            ):
                raise _contract_error()
            return payload["data"]
        raise _contract_error()  # All request outcomes return or raise above.

    async def probe(self, base_url: str, api_key: str, config: dict | None = None) -> None:
        health = await self._get_data(base_url, api_key, "/api/external/health")
        status = _text(health, "status", required=True)
        database = _text(health, "database", required=True)
        if status != "ok" or database != "ok":
            raise ProviderError("UPSTREAM_UNAVAILABLE")
        data = await self._get_data(base_url, api_key, "/api/external/capabilities")
        features = data.get("features")
        if (
            not isinstance(features, list)
            or not all(isinstance(f, str) for f in features)
            or not {"message_list", "message_detail"}.issubset(features)
        ):
            raise _contract_error()

    async def check_mailbox(self, base_url: str, api_key: str, email: str, config: dict | None = None) -> str | None:
        data = await self._get_data(
            base_url, api_key, "/api/external/account-status", params={"email": email}
        )
        if data.get("exists") is not True or data.get("can_read") is not True:
            raise _contract_error()

    @staticmethod
    def _parse_summary(item: dict) -> MessageSummary:
        if not isinstance(item, dict):
            raise _contract_error()
        timestamp = item.get("timestamp")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or (isinstance(timestamp, float) and not math.isfinite(timestamp))
        ):
            raise _contract_error()
        try:
            received_at = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise _contract_error() from None
        return MessageSummary(
            provider_id=_text(item, "id", required=True),
            method=_text(item, "method", required=True),
            sender=_text(item, "from_address"),
            subject=_text(item, "subject"),
            snippet=_text(item, "content_preview"),
            received_at=received_at,
        )

    async def list_messages(
        self, base_url: str, api_key: str, email: str, config: dict | None = None
    ) -> MessageListResult:
        summaries = []
        issues = []
        for skip in (0, 50):
            data = await self._get_data(
                base_url,
                api_key,
                "/api/external/messages",
                params={"email": email, "folder": "inbox", "skip": skip, "top": 50},
            )
            page = data.get("emails")
            if not isinstance(page, list) or len(page) > 50:
                raise _contract_error()
            for position, item in enumerate(page):
                try:
                    summaries.append(self._parse_summary(item))
                except ProviderError as exc:
                    issues.append(
                        MessageListIssue(absolute_index=skip + position, code=exc.code)
                    )
            if len(page) < 50:
                break
        return MessageListResult(summaries=summaries[:100], issues=issues)

    async def get_message(
        self,
        base_url: str,
        api_key: str,
        email: str,
        summary: MessageSummary,
        config: dict | None = None,
    ) -> NormalizedMessage:
        data = await self._get_data(
            base_url,
            api_key,
            "/api/external/messages/"
            + quote(summary.provider_id, safe="").replace(".", "%2E"),
            params={"email": email, "folder": "inbox"},
        )
        # Detail has no content_preview; retain the list snippet, but normalize
        # detail identity/time/headers rather than guessing from the request.
        detail = self._parse_summary({**data, "content_preview": summary.snippet})
        if detail.provider_id != summary.provider_id or detail.method != summary.method:
            raise _contract_error()
        return NormalizedMessage(
            provider_id=detail.provider_id,
            method=detail.method,
            sender=detail.sender,
            subject=detail.subject,
            snippet=detail.snippet,
            received_at=detail.received_at,
            recipient=_text(data, "to_address"),
            body_text=_plain_body(data),
            rfc_message_id=_rfc_message_id(_text(data, "raw_content")),
        )
