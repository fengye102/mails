"""HTTP adapter for the deployed Cloudflare temporary-mail Worker."""
import asyncio
import base64
import binascii
import re
from email import policy
from email.parser import Parser
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import quote

import httpx

from public_mail_api.providers.contracts import CreatedAddress


class UpstreamError(Exception):
    _messages: ClassVar[dict[str, str]] = {
        "UPSTREAM_AUTH": "Upstream authentication failed.",
        "UPSTREAM_TIMEOUT": "Upstream request timed out.",
        "UPSTREAM_UNAVAILABLE": "Upstream service is unavailable.",
        "UPSTREAM_CONTRACT": "Upstream response does not meet the read contract.",
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self._messages.get(code, "Upstream request failed."))


def _text(value: object, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value.strip()) or "\x00" in value:
        raise UpstreamError("UPSTREAM_CONTRACT")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise UpstreamError("UPSTREAM_CONTRACT") from None
    return value


def _row_id(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("id", value.get("message_id"))
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise UpstreamError("UPSTREAM_CONTRACT")
    return str(value)


def _parse_datetime(value: object) -> str:
    value = _text(value, required=True)
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT.+", value):
        raise UpstreamError("UPSTREAM_CONTRACT")
    return value.replace("Z", "+00:00")


def _address(value: object) -> str:
    text = _text(value)
    parsed = getaddresses([text])
    return parsed[0][1] if len(parsed) == 1 and parsed[0][1] else text


class _PlainTextHtmlParser(HTMLParser):
    """Extract text without exposing markup or script/style payloads."""

    _block_tags: ClassVar[set[str]] = {"br", "div", "li", "p", "section", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    parser = _PlainTextHtmlParser()
    parser.feed(value)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _plain_body(raw: str) -> tuple[str, str, str, str]:
    try:
        message = Parser(policy=policy.default).parsestr(raw)
        if message.defects:
            raise ValueError
        sender = _address(message.get("From", ""))
        recipient = _address(message.get("To", ""))
        subject = _text(message.get("Subject", ""))
        if message.is_multipart():
            plain_part = next(
                (part for part in message.walk()
                 if part.get_content_type() == "text/plain" and not part.is_multipart()),
                None,
            )
            html_part = next(
                (part for part in message.walk()
                 if part.get_content_type() == "text/html" and not part.is_multipart()),
                None,
            )
            body_value = plain_part.get_content() if plain_part is not None else (
                _html_to_text(html_part.get_content()) if html_part is not None else ""
            )
        else:
            body_value = message.get_content()
            if message.get_content_type() == "text/html" and isinstance(body_value, str):
                body_value = _html_to_text(body_value)
        body = _text(body_value if isinstance(body_value, str) else "")
        return sender, recipient, subject, body
    except (LookupError, TypeError, ValueError, UnicodeError, UpstreamError):
        raise UpstreamError("UPSTREAM_CONTRACT") from None


def _body_from_item(item: dict) -> tuple[str, str, str, str]:
    raw = item.get("raw", item.get("raw_blob"))
    if isinstance(raw, dict) and set(raw) == {"base64"}:
        try:
            raw = base64.b64decode(raw["base64"], validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError):
            raise UpstreamError("UPSTREAM_CONTRACT") from None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeError:
            raise UpstreamError("UPSTREAM_CONTRACT") from None
    if isinstance(raw, str):
        return _plain_body(raw)
    sender = _address(item.get("sender", item.get("from", "")))
    recipient = _address(item.get("recipient", item.get("to", "")))
    subject = _text(item.get("subject", ""))
    body = _text(item.get("body_text", item.get("body", "")))
    return sender, recipient, subject, body


def _message(item: object) -> dict:
    if not isinstance(item, dict):
        raise UpstreamError("UPSTREAM_CONTRACT")
    sender, recipient, subject, body = _body_from_item(item)
    return {
        "id": _row_id(item),
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "body_text": body,
        "snippet": body[:500],
        "received_at": _parse_datetime(item.get("created_at", item.get("received_at"))),
        "verification_code": _verification_code(body),
    }


def _verification_code(body: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", body)
    return match.group(1) if match else None


class CloudflarePublicAdapter:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _request(self, base_url: str, token: str, method: str, path: str, *, admin=False, params=None):
        headers = {"Authorization": f"Bearer {token}"}
        if admin:
            headers["X-Admin-Auth"] = token
        for attempt in range(2):
            try:
                async with asyncio.timeout(20):
                    response = await self.client.request(
                        method, base_url.rstrip("/") + path, headers=headers, params=params,
                        timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
                    )
            except (httpx.TimeoutException, TimeoutError):
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise UpstreamError("UPSTREAM_TIMEOUT") from None
            except httpx.RequestError:
                raise UpstreamError("UPSTREAM_UNAVAILABLE") from None
            if response.status_code in (401, 403):
                raise UpstreamError("UPSTREAM_AUTH")
            if response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(0.2)
                    continue
                raise UpstreamError("UPSTREAM_UNAVAILABLE")
            if not 200 <= response.status_code < 300:
                raise UpstreamError("UPSTREAM_CONTRACT")
            return response
        raise UpstreamError("UPSTREAM_UNAVAILABLE")

    async def create_address(self, base_url: str, admin_key: str) -> CreatedAddress:
        response = await self._request(base_url, admin_key, "POST", "/admin/new_address", admin=True)
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError("UPSTREAM_CONTRACT") from None
        if not isinstance(payload, dict):
            raise UpstreamError("UPSTREAM_CONTRACT")
        address = payload.get("address", payload.get("email"))
        jwt = payload.get("jwt", payload.get("token", payload.get("access_token")))
        return CreatedAddress(address=_text(address, required=True), jwt=_text(jwt, required=True))

    async def list_messages(self, base_url: str, address_jwt: str, address: str) -> list[dict]:
        response = await self._request(base_url, address_jwt, "GET", "/api/mails", params={"address": address})
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError("UPSTREAM_CONTRACT") from None
        rows = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or len(rows) > 100:
            raise UpstreamError("UPSTREAM_CONTRACT")
        messages = []
        for row in rows:
            try:
                messages.append(_message(row))
            except UpstreamError:
                continue
        return messages

    async def get_message(self, base_url: str, address_jwt: str, address: str, message_id: str) -> dict:
        response = await self._request(base_url, address_jwt, "GET", "/api/mails/" + quote(message_id, safe=""))
        try:
            payload = response.json()
        except ValueError:
            raise UpstreamError("UPSTREAM_CONTRACT") from None
        result = _message(payload)
        if result["id"] != message_id:
            raise UpstreamError("UPSTREAM_CONTRACT")
        return result




