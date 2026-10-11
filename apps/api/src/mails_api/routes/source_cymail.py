"""CYMail（iCloud 隐藏邮箱）来源管理路由。

读操作只要求 member；写操作（创建/删除/停用/启用别名、库存同步）叠加 owner + CSRF。
连接凭据仍走既有加密存储，密钥只用于请求上游（X-API-Key），绝不进入响应体或日志。
"""

from typing import Annotated, Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request
from mails_api.db import get_db
from mails_api.models import ProviderConnection
from mails_api.routes.connections import (
    allowed_origin,
    encryption_key,
    get_http_client,
)
from mails_api.security.crypto import decrypt_secret
from mails_api.security.csrf import require_csrf, require_member, require_owner
from mails_api.sources_admin import admin_for
from mails_api.sources_admin.base import SourceAdminError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as DbSession

router = APIRouter(prefix="/api/v1/sources/cymail", dependencies=[Depends(require_member)])

# SourceAdminError.code → 对外 HTTP 状态；detail 固定为错误码，前端按码映射文案。
_STATUS_BY_CODE = {
    "SOURCE_AUTH": 502,
    "SOURCE_TIMEOUT": 504,
    "SOURCE_UNAVAILABLE": 502,
    "SOURCE_CONTRACT": 502,
    "SOURCE_NOT_FOUND": 404,
    "SOURCE_CONFLICT": 409,
    "SOURCE_REJECTED": 502,
}


class AliasCreateInput(BaseModel):
    label: str = Field(default="", max_length=256)

    @field_validator("label")
    @classmethod
    def valid_label(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("Invalid alias label")
        return value.strip()


def _fail(exc: SourceAdminError) -> HTTPException:
    return HTTPException(status_code=_STATUS_BY_CODE.get(exc.code, 502), detail=exc.code)


def _load_connection(db: DbSession, connection_id: UUID) -> ProviderConnection:
    connection = db.get(ProviderConnection, connection_id)
    if connection is None or connection.kind != "cymail":
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    return connection


def _resolve(
    request: Request, connection: ProviderConnection, client: Any
) -> tuple[str, str, dict[str, Any], Any]:
    origin = allowed_origin(connection.base_url, request)
    try:
        secret = decrypt_secret(encryption_key(request), connection.encrypted_secret)
    except (ValueError, InvalidTag, UnicodeError):
        raise HTTPException(
            status_code=503, detail="CONNECTION_SECRET_INVALID"
        ) from None
    adapter = admin_for("cymail", client)
    return origin, secret, connection.config_json or {}, adapter


@router.get("/accounts")
async def list_accounts(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> list[dict[str, Any]]:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        return await adapter.list_accounts(origin, secret, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None


@router.get("/aliases")
async def list_aliases(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> list[dict[str, Any]]:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        items = await adapter.list_items(origin, secret, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None
    return [item.payload() for item in items]


@router.post(
    "/aliases",
    status_code=201,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def create_alias(
    connection_id: UUID,
    body: AliasCreateInput,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict[str, Any]:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        item = await adapter.create_item(origin, secret, {"label": body.label}, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None
    return item.payload()


@router.post(
    "/aliases/{provider_key}/deactivate",
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def deactivate_alias(
    provider_key: str,
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict[str, Any]:
    return await _alias_action(
        provider_key, connection_id, request, db, client, "deactivate_alias"
    )


@router.post(
    "/aliases/{provider_key}/reactivate",
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def reactivate_alias(
    provider_key: str,
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict[str, Any]:
    return await _alias_action(
        provider_key, connection_id, request, db, client, "reactivate_alias"
    )


async def _alias_action(
    provider_key: str,
    connection_id: UUID,
    request: Request,
    db: DbSession,
    client: Any,
    method: str,
) -> dict[str, Any]:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        ok = await getattr(adapter, method)(origin, secret, provider_key, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None
    return {"provider_key": provider_key, "success": bool(ok)}


@router.delete(
    "/aliases/{provider_key}",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def delete_alias(
    provider_key: str,
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> None:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        await adapter.delete_item(origin, secret, provider_key, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None


@router.post(
    "/inventory/sync",
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def sync_inventory(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict[str, Any]:
    connection = _load_connection(db, connection_id)
    origin, secret, config, adapter = _resolve(request, connection, client)
    try:
        return await adapter.sync_inventory(origin, secret, config)
    except SourceAdminError as exc:
        raise _fail(exc) from None
