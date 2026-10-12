"""统一收件箱的「上游实时读信」：不登记邮箱也能读三家来源的邮件。

设计要点：

- 只读。统一挂 ``require_member``，不引入任何写操作，也不改变上游邮箱状态；
  写动作仍然只走 ``/api/v1/sources/*`` 与邮箱登记路由。
- 复用 provider 层（``providers.*``）的读契约，与定时同步走同一套解析：
  「实时看到的」和「归档下来的」永远是同一份字段（含验证码）。
- 验证码在服务端用 ``services.extract`` 提取；正文在 provider 层已转纯文本。
- 关键词过滤按发件人/主题/摘要做本地子串匹配：三家上游都没有通用服务端搜索。
- 上游错误码原样映射为 502，凭据绝不进响应体、日志或异常消息。
"""

from __future__ import annotations

from datetime import UTC
from typing import Annotated
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session as DbSession

from mails_api.db import get_db
from mails_api.models import ProviderConnection
from mails_api.providers.base import MessageSummary, ProviderError
from mails_api.providers.factory import AccountIdRequired, mailbox_key_for
from mails_api.routes.connections import (
    allowed_origin,
    encryption_key,
    get_adapter_for,
    get_http_client,
)
from mails_api.security.crypto import decrypt_secret
from mails_api.security.csrf import require_member
from mails_api.services.extract import extract_code, extract_link

router = APIRouter(
    prefix="/api/v1/connections", dependencies=[Depends(require_member)]
)

_PROVIDER_STATUS = {
    "UPSTREAM_UNAVAILABLE": 502,
    "UPSTREAM_AUTH": 502,
    "UPSTREAM_TIMEOUT": 502,
    "UPSTREAM_CONTRACT": 502,
}


def _provider_error(exc: ProviderError) -> HTTPException:
    return HTTPException(
        status_code=_PROVIDER_STATUS.get(exc.code, 502), detail=exc.code
    )


def _utc_iso(value) -> str:
    received_at = value.replace(tzinfo=value.tzinfo or UTC)
    return received_at.astimezone(UTC).isoformat()


def _load_connection(
    request: Request, db: DbSession, connection_id: UUID
) -> tuple[ProviderConnection, str, str]:
    connection = db.get(ProviderConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    if connection.status != "active":
        raise HTTPException(status_code=502, detail="UPSTREAM_UNAVAILABLE")
    origin = allowed_origin(connection.base_url, request)
    try:
        secret = decrypt_secret(encryption_key(request), connection.encrypted_secret)
    except (ValueError, InvalidTag, UnicodeError):
        raise HTTPException(
            status_code=503, detail="CONNECTION_SECRET_INVALID"
        ) from None
    return connection, origin, secret


def _provider_address(connection: ProviderConnection, address: str) -> str:
    """寻址规则统一走 provider 层：账号寻址的来源（CYMail / iCloud 引擎）编进 key。"""
    try:
        return mailbox_key_for(connection.kind, connection.config_json or {}, address)
    except AccountIdRequired as exc:
        raise HTTPException(status_code=422, detail=exc.code) from None


def _summary_payload(summary: MessageSummary, kind: str) -> dict:
    return {
        "provider_key": summary.provider_message_key,
        "source_kind": kind,
        "sender": summary.sender,
        "subject": summary.subject,
        "snippet": summary.snippet,
        "received_at": _utc_iso(summary.received_at),
        "verification_code": extract_code(summary.subject, summary.snippet),
    }


async def _list(
    request: Request,
    db: DbSession,
    client: httpx.AsyncClient,
    connection_id: UUID,
    address: str,
):
    connection, origin, secret = _load_connection(request, db, connection_id)
    provider_address = _provider_address(connection, address)
    adapter = get_adapter_for(connection.kind, client)
    try:
        result = await adapter.list_messages(
            origin, secret, provider_address, connection.config_json or {}
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    return connection, origin, secret, provider_address, adapter, result


@router.get("/{connection_id}/live-mail")
async def list_live_mail(
    connection_id: UUID,
    address: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    query: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict:
    connection, _origin, _secret, _address, _adapter, result = await _list(
        request, db, client, connection_id, address
    )
    items = [_summary_payload(summary, connection.kind) for summary in result.summaries]
    if query:
        needle = query.casefold()
        items = [
            item
            for item in items
            if needle
            in f"{item['sender']} {item['subject']} {item['snippet']}".casefold()
        ]
    items.sort(key=lambda item: (item["received_at"], item["provider_key"]), reverse=True)
    return {
        "total": len(items),
        "items": items[offset : offset + limit],
        "issues": [
            {"index": issue.absolute_index, "code": issue.code}
            for issue in result.issues
        ],
    }


@router.get("/{connection_id}/live-mail/detail")
async def get_live_mail(
    connection_id: UUID,
    address: str,
    key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> dict:
    connection, origin, secret, provider_address, adapter, result = await _list(
        request, db, client, connection_id, address
    )
    summary = next(
        (item for item in result.summaries if item.provider_message_key == key), None
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="LIVE_MESSAGE_NOT_FOUND")
    try:
        detail = await adapter.get_message(
            origin, secret, provider_address, summary, connection.config_json or {}
        )
    except ProviderError as exc:
        raise _provider_error(exc) from None
    if detail.provider_message_key != key:
        raise HTTPException(status_code=502, detail="UPSTREAM_CONTRACT")
    return {
        **_summary_payload(detail, connection.kind),
        "recipient": detail.recipient,
        "rfc_message_id": detail.rfc_message_id,
        "body_text": detail.body_text,
        "verification_code": extract_code(detail.subject, detail.body_text),
        "link": extract_link(detail.subject, detail.body_text),
    }
