"""iCloud 隐藏邮箱引擎（``apps/icloud_api``）的只读适配器。

引擎对外只有机器接口 ``/icloud-api/v1/*``（Bearer 运营 token）。读信链路一条：

    GET {base_url}/icloud-api/v1/accounts/{account_id}/mail?address=&limit=&days=

与 CYMail 适配器的两点差别，都是照上游真实行为写的：

1. **引擎没有「单封详情」端点**，但列表响应已经带完整正文——上游 IMAP 抓的是整封 RFC822
   （``mail/imap_client.py`` 的 ``parse_message`` 会填 ``body_text``）。因此 ``list_messages``
   顺手把正文缓存进适配器实例，``get_message`` 优先复用；缓存不在（换了个实例）时按同一组
   参数重读一次列表。缓存只活在实例内，不落任何持久数据。
2. **``method`` 用固定值**。``provider_message_key`` 由 ``f"{method}:{provider_id}"`` 组成，
   而引擎自己的 ``method`` 字段会在 ``imap`` / ``imap_recipient_search`` 之间切换；照抄的话
   同一封邮件换个查询方式就会变成两个 key，A 站归档里会出现重复邮件。
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from mails_api.providers.base import (
    MessageListIssue,
    MessageListResult,
    MessageSummary,
    NormalizedMessage,
    ProviderError,
)

_KEY_PREFIX = "icloud_hme:"
# 引擎当前只有 IMAP 一条读信链路（web 回退未落地），所以 method 固定即可。
_METHOD = "imap"
# 引擎侧上限：limit ≤ 200、days ≤ 365（apps/icloud_api/.../mail/service.py）。
_LIST_LIMIT = 100
_LIST_DAYS = 7
# 账号 id 会拼进 URL 路径，长度与字符集按 A 站连接配置的同一套规则收敛。
_MAX_ACCOUNT_ID = 256


def mailbox_key(account_id: str, address: str) -> str:
    """把「引擎账号 + 别名地址」编进统一 mailbox key。

    引擎按「账号 + 别名」寻址，A 站的 ``mailboxes.address`` 只存裸地址，
    所以寻址信息必须编进 provider key（与 CYMail 同一套做法）。
    """
    if not _valid_account_id(account_id) or not isinstance(address, str) or not address.strip():
        raise ValueError("invalid iCloud HME mailbox key")

    def encode(value: str) -> str:
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")

    return f"{_KEY_PREFIX}{encode(account_id.strip())}:{encode(address.strip().lower())}"


def parse_mailbox_key(value: str) -> tuple[str, str]:
    """``mailbox_key`` 的逆运算；非法输入一律 ``ValueError``。"""
    if not isinstance(value, str) or not value.startswith(_KEY_PREFIX):
        raise ValueError("invalid iCloud HME mailbox key")
    parts = value[len(_KEY_PREFIX) :].split(":")
    if len(parts) != 2 or not all(parts):
        raise ValueError("invalid iCloud HME mailbox key")

    def decode(encoded: str) -> str:
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeError):
            raise ValueError("invalid iCloud HME mailbox key") from None
        if not decoded or "\x00" in decoded:
            raise ValueError("invalid iCloud HME mailbox key")
        return decoded

    account_id, address = (decode(item) for item in parts)
    if not _valid_account_id(account_id):
        raise ValueError("invalid iCloud HME mailbox key")
    return account_id, address.lower()


def _valid_account_id(value: object) -> bool:
    """账号 id 来自连接配置，会拼进 URL 路径——挡住路径穿越与控制字符。"""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_ACCOUNT_ID
        and all(ord(char) >= 32 for char in value)
        and "/" not in value
        and "\\" not in value
    )


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
    """引擎的 ``date`` 是 RFC3339（``2026-10-12T06:32:10Z``），同时容忍 RFC2822。

    取不到时间就报契约错（这条会进 ``issues``），**不用「现在」顶替**：
    归档里的时间必须是上游真实时间，编一个时间戳等于往索引里写假数据。
    """
    if not isinstance(value, str) or not value.strip():
        raise _contract_error()
    try:
        # requires-python >= 3.12：fromisoformat 直接吃 `Z` 结尾，不必手工替换成 +00:00。
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            raise _contract_error() from None
    if parsed.tzinfo is None:
        raise _contract_error()
    try:
        return parsed.astimezone(UTC)
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


class ICloudHMEAdapter:
    """引擎机器接口的读适配器：登记前验证可读，读信映射成 core 的 DTO。"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._details: dict[tuple[str, str, str], _CachedDetail] = {}

    async def _get_data(
        self,
        base_url: str,
        token: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
    ) -> object:
        """带 Bearer token 取 JSON；错误一律收敛成固定错误码。

        5xx 与超时各重试一次（引擎背后是真 IMAP，抖一下就失败对读信太脆），
        401/403 直接报认证失败——重试一个坏 token 没有意义。
        """
        url = base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(2):
            try:
                async with asyncio.timeout(20):
                    response = await self.client.get(
                        url,
                        headers=headers,
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
                # 400/404/429 在这里都意味着「这次调用拿不到可信数据」：
                # 账号没配 App Password、账号不存在、引擎侧限速，如实报契约错，
                # 不把「读不到」伪装成空列表。
                raise _contract_error()
            try:
                return response.json()
            except ValueError:
                raise _contract_error() from None
        raise _contract_error()

    async def probe(self, base_url: str, token: str, config: dict | None = None) -> None:
        """确认引擎在线、token 有效。

        只打 ``/capabilities``（机器接口里最便宜的一个），不在这里校验具体账号：
        账号是否存在、能不能读信由 ``check_mailbox`` 在登记时如实报错，
        探测阶段多触发一次 Apple 上游没有收益。
        """
        data = await self._get_data(base_url, token, "/icloud-api/v1/capabilities")
        if not isinstance(data, dict) or data.get("service") != "icloud-hme-engine":
            raise _contract_error()

    async def check_mailbox(
        self, base_url: str, token: str, mailbox: str, config: dict | None = None
    ) -> str:
        """登记前确认「这封地址真的读得到」——引擎会真连一次 IMAP（limit=1）。

        引擎侧语义原样上抛：400（缺 App Password）/ 404（账号不存在）→ 契约错，
        401（登录失败）→ 认证失败，502（IMAP 故障）→ 上游不可用。
        宁可登记失败，也不要把读不到的地址登记成可用库存。
        """
        account_id, address = _split_key(mailbox)
        data = await self._get_data(
            base_url,
            token,
            f"/icloud-api/v1/accounts/{quote(account_id, safe='')}/mail",
            params={"address": address, "limit": 1, "days": _LIST_DAYS},
        )
        echoed_account, echoed_address, _items = self._parse_envelope(data)
        if echoed_account != account_id or echoed_address != address:
            raise _contract_error()
        return mailbox

    @staticmethod
    def _parse_envelope(data: object) -> tuple[str, str, list]:
        """校验列表响应的信封；``method`` 是上游自述、本适配器不使用，故不校验取值。"""
        if not isinstance(data, dict):
            raise _contract_error()
        account_id = _text(data, "account_id", required=True)
        address = _text(data, "address", required=True).strip().lower()
        messages = data.get("messages")
        if not isinstance(messages, list) or len(messages) > 200:
            raise _contract_error()
        return account_id, address, messages

    @staticmethod
    def _parse_message(item: object, fallback_address: str) -> tuple[MessageSummary, _CachedDetail]:
        if not isinstance(item, dict):
            raise _contract_error()
        provider_id = _text(item, "id", required=True)
        summary = MessageSummary(
            provider_id=provider_id,
            method=_METHOD,
            sender=_text(item, "sender"),
            subject=_text(item, "subject"),
            snippet=_text(item, "snippet"),
            received_at=_parse_date(item.get("date")),
        )
        recipient = _text(item, "to").strip().lower() or fallback_address
        body = _text(item, "body_text")
        return summary, _CachedDetail(
            recipient=recipient,
            # 引擎偶有正文为空的条目（上游解析失败的早退分支）：退回首段摘要，
            # 不假装有完整正文，也不因为一条正文缺失就把整封信丢掉。
            body_text=body or summary.snippet,
            rfc_message_id=_text(item, "rfc_message_id").strip() or None,
        )

    def _cache_key(self, base_url: str, token: str, mailbox: str) -> tuple[str, str, str]:
        return base_url.rstrip("/"), token, mailbox

    def _mail_path(self, account_id: str) -> str:
        return f"/icloud-api/v1/accounts/{quote(account_id, safe='')}/mail"

    async def list_messages(
        self, base_url: str, token: str, mailbox: str, config: dict | None = None
    ) -> MessageListResult:
        account_id, address = _split_key(mailbox)
        data = await self._get_data(
            base_url,
            token,
            self._mail_path(account_id),
            params={"address": address, "limit": _LIST_LIMIT, "days": _LIST_DAYS},
        )
        echoed_account, echoed_address, items = self._parse_envelope(data)
        if echoed_account != account_id or echoed_address != address:
            # 回显不一致说明取到的不是这封地址的信——宁可报错，也不要把别人的邮件归档进来。
            raise _contract_error()
        summaries: list[MessageSummary] = []
        issues: list[MessageListIssue] = []
        cache = self._cache_key(base_url, token, mailbox)
        for index, item in enumerate(items):
            try:
                summary, detail = self._parse_message(item, address)
            except ProviderError as exc:
                issues.append(MessageListIssue(index, exc.code))
                continue
            summaries.append(summary)
            self._details[(*cache[0:2], summary.provider_message_key)] = detail
        return MessageListResult(summaries=summaries, issues=issues)

    async def get_message(
        self,
        base_url: str,
        token: str,
        mailbox: str,
        summary: MessageSummary,
        config: dict | None = None,
    ) -> NormalizedMessage:
        _account_id, address = _split_key(mailbox)
        cache = self._cache_key(base_url, token, mailbox)
        detail = self._details.get((*cache[0:2], summary.provider_message_key))
        if detail is None:
            # 换了适配器实例（进程重启、并发路径）时重读一次列表把正文补回来。
            result = await self.list_messages(base_url, token, mailbox)
            if any(item.provider_message_key == summary.provider_message_key for item in result.summaries):
                detail = self._details.get((*cache[0:2], summary.provider_message_key))
        if detail is None:
            raise _contract_error()
        if summary.method != _METHOD or not summary.provider_id:
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
