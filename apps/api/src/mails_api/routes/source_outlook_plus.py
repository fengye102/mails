"""OutlookEmailPlus 来源管理路由。

把管理动作（列/增/删账号、刷新令牌、导出清单）转发给 ``sources_admin.outlook_plus``
适配器；鉴权、来源白名单与密钥解密复用 ``routes.connections`` 的既有写法。

- 路由统一挂 ``require_member``；写操作再加 ``require_owner`` + ``require_csrf``。
- 密钥只用于向上游换取会话，绝不进入响应体、日志或异常消息。
- 上游错误码原样映射为 HTTP detail，不新增自定义字符串。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session as DbSession

from mails_api.db import get_db
from mails_api.models import ProviderConnection
from mails_api.routes.connections import allowed_origin, encryption_key, get_http_client
from mails_api.security.crypto import decrypt_secret
from mails_api.security.csrf import require_csrf, require_member, require_owner
from mails_api.sources_admin import admin_for
from mails_api.sources_admin.base import SourceAdminError

router = APIRouter(
    prefix="/api/v1/sources/outlook_plus", dependencies=[Depends(require_member)]
)

# 上游固定错误码 → HTTP 状态；未列出的按上游不可用处理。
_SOURCE_STATUS = {
    "SOURCE_AUTH": 502,
    "SOURCE_TIMEOUT": 502,
    "SOURCE_UNAVAILABLE": 502,
    "SOURCE_CONTRACT": 502,
    "SOURCE_NOT_FOUND": 404,
    "SOURCE_CONFLICT": 409,
    "SOURCE_REJECTED": 502,
}


def _load_connection(
    request: Request, db: DbSession, connection_id: UUID
) -> tuple[ProviderConnection, str, str]:
    connection = db.get(ProviderConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    if connection.kind != "outlook_plus":
        raise HTTPException(status_code=422, detail="UNSUPPORTED_PROVIDER_KIND")
    origin = allowed_origin(connection.base_url, request)
    try:
        secret = decrypt_secret(encryption_key(request), connection.encrypted_secret)
    except (ValueError, InvalidTag, UnicodeError):
        raise HTTPException(status_code=503, detail="CONNECTION_SECRET_INVALID") from None
    return connection, origin, secret


def _source_error(exc: SourceAdminError) -> HTTPException:
    return HTTPException(status_code=_SOURCE_STATUS.get(exc.code, 502), detail=exc.code)


@router.get("/{connection_id}/items")
async def list_items(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> list[dict]:
    connection, origin, secret = _load_connection(request, db, connection_id)
    admin = admin_for("outlook_plus", client)
    try:
        items = await admin.list_items(origin, secret, connection.config_json or {})
    except SourceAdminError as exc:
        raise _source_error(exc) from None
    return [item.payload() for item in items]


@router.post(
    "/{connection_id}/probe",
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def probe_connection(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> dict:
    connection, origin, secret = _load_connection(request, db, connection_id)
    admin = admin_for("outlook_plus", client)
    try:
        await admin.probe(origin, secret, connection.config_json or {})
    except SourceAdminError as exc:
        raise _source_error(exc) from None
    return {"status": "ok"}


@router.post(
    "/{connection_id}/items",
    status_code=201,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def create_item(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> dict:
    connection, origin, secret = _load_connection(request, db, connection_id)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="REQUEST_INVALID") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="REQUEST_INVALID")
    admin = admin_for("outlook_plus", client)
    try:
        item = await admin.create_item(origin, secret, body, connection.config_json or {})
    except SourceAdminError as exc:
        raise _source_error(exc) from None
    return item.payload()


@router.delete(
    "/{connection_id}/items/{provider_key}",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def delete_item(
    connection_id: UUID,
    provider_key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> None:
    connection, origin, secret = _load_connection(request, db, connection_id)
    admin = admin_for("outlook_plus", client)
    try:
        await admin.delete_item(origin, secret, provider_key, connection.config_json or {})
    except SourceAdminError as exc:
        raise _source_error(exc) from None


@router.post(
    "/{connection_id}/accounts/{account_id}/refresh",
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def refresh_account_token(
    connection_id: UUID,
    account_id: int,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> dict:
    connection, origin, secret = _load_connection(request, db, connection_id)
    admin = admin_for("outlook_plus", client)
    try:
        return await admin.refresh_token(
            origin, secret, account_id, connection.config_json or {}
        )
    except SourceAdminError as exc:
        raise _source_error(exc) from None


@router.get(
    "/{connection_id}/export", dependencies=[Depends(require_owner)]
)
async def export_accounts(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PlainTextResponse:
    connection, origin, secret = _load_connection(request, db, connection_id)
    admin = admin_for("outlook_plus", client)
    try:
        content = await admin.export_accounts(origin, secret, connection.config_json or {})
    except SourceAdminError as exc:
        raise _source_error(exc) from None
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="outlook_plus_accounts.txt"'
        },
    )
