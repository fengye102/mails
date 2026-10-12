"""Owner 管理的 Cloudflare 临时邮箱来源动作。

管理动作是无状态的上游调用：只复用连接里已加密保存的管理员口令，不落库、不改同步链路。
读操作要求 member，写操作额外要求 owner + CSRF，与 connections.py 保持一致。
管理员口令与上游返回的地址凭据绝不写日志、绝不进审计文本；查看地址口令的响应显式标记 sensitive。
"""

from typing import Annotated
from uuid import UUID

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.orm import Session as DbSession

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
from mails_api.sources_admin.base import SourceAdmin, SourceAdminError

SOURCE_KIND = "cloudflare_temp_email"

router = APIRouter(
    prefix="/api/v1/sources/cloudflare_temp_email",
    dependencies=[Depends(require_member)],
)

# 管理动作固定错误码 → HTTP 状态；detail 直接回固定错误码，凭据不进响应体。
_ERROR_STATUS = {
    "SOURCE_AUTH": 502,
    "SOURCE_TIMEOUT": 504,
    "SOURCE_UNAVAILABLE": 502,
    "SOURCE_CONTRACT": 502,
    "SOURCE_NOT_FOUND": 404,
    "SOURCE_CONFLICT": 409,
    "SOURCE_REJECTED": 422,
}


class CreateAddressInput(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    domain: str | None = Field(default=None, max_length=253)
    enable_prefix: bool = False


class ResetPasswordInput(BaseModel):
    # 不加长度约束，避免校验失败时 FastAPI 把口令明文回显到 422 响应里；长度在路由内校验。
    password: SecretStr


def _failure(exc: SourceAdminError) -> HTTPException:
    return HTTPException(status_code=_ERROR_STATUS.get(exc.code, 502), detail=exc.code)


async def _adapter(
    connection_id: UUID, request: Request, db: DbSession, client: object
) -> tuple[str, str, SourceAdmin]:
    connection = db.get(ProviderConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="CONNECTION_NOT_FOUND")
    if connection.kind != SOURCE_KIND:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_PROVIDER_KIND")
    origin = allowed_origin(connection.base_url, request)
    try:
        secret = decrypt_secret(encryption_key(request), connection.encrypted_secret)
    except (ValueError, InvalidTag, UnicodeError):
        raise HTTPException(status_code=503, detail="CONNECTION_SECRET_INVALID") from None
    return origin, secret, admin_for(SOURCE_KIND, client)


@router.post("/{connection_id}/probe", dependencies=[Depends(require_owner), Depends(require_csrf)])
async def probe_source(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        await adapter.probe(origin, secret)
    except SourceAdminError as exc:
        raise _failure(exc) from None
    return {"status": "ok"}


@router.get("/{connection_id}/items")
async def list_source_items(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> list[dict]:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        items = await adapter.list_items(origin, secret)
    except SourceAdminError as exc:
        raise _failure(exc) from None
    return [item.payload() for item in items]


@router.post(
    "/{connection_id}/items",
    status_code=201,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def create_source_item(
    connection_id: UUID,
    body: CreateAddressInput,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        item = await adapter.create_item(
            origin,
            secret,
            {"name": body.name, "domain": body.domain, "enable_prefix": body.enable_prefix},
        )
    except SourceAdminError as exc:
        raise _failure(exc) from None
    return item.payload()


@router.delete(
    "/{connection_id}/items/{provider_key}",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def delete_source_item(
    connection_id: UUID,
    provider_key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> None:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        await adapter.delete_item(origin, secret, provider_key)
    except SourceAdminError as exc:
        raise _failure(exc) from None


@router.post(
    "/{connection_id}/items/{provider_key}/clear_inbox",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def clear_source_inbox(
    connection_id: UUID,
    provider_key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> None:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        await adapter.clear_inbox(origin, secret, provider_key)
    except SourceAdminError as exc:
        raise _failure(exc) from None


@router.get("/{connection_id}/mails")
async def list_source_mails(
    connection_id: UUID,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
    address: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        return await adapter.list_mails(origin, secret, address=address, limit=limit, offset=offset)
    except SourceAdminError as exc:
        raise _failure(exc) from None


@router.delete(
    "/{connection_id}/mails/{mail_id}",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def delete_source_mail(
    connection_id: UUID,
    mail_id: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> None:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        await adapter.delete_mail(origin, secret, mail_id)
    except SourceAdminError as exc:
        raise _failure(exc) from None


@router.get(
    "/{connection_id}/items/{provider_key}/password",
    dependencies=[Depends(require_owner)],
)
async def show_source_password(
    connection_id: UUID,
    provider_key: str,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> dict:
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        return await adapter.show_password(origin, secret, provider_key)
    except SourceAdminError as exc:
        raise _failure(exc) from None


@router.post(
    "/{connection_id}/items/{provider_key}/password",
    status_code=204,
    dependencies=[Depends(require_owner), Depends(require_csrf)],
)
async def reset_source_password(
    connection_id: UUID,
    provider_key: str,
    body: ResetPasswordInput,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[object, Depends(get_http_client)],
) -> None:
    password = body.password.get_secret_value()
    if not 1 <= len(password) <= 256:
        raise HTTPException(status_code=422, detail="PASSWORD_INVALID")
    origin, secret, adapter = await _adapter(connection_id, request, db, client)
    try:
        await adapter.reset_password(origin, secret, provider_key, password)
    except SourceAdminError as exc:
        raise _failure(exc) from None
