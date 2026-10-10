"""B1 public mailbox routes."""
from datetime import timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from public_mail_api.db import get_db
from public_mail_api.models import PublicMailbox, utc_now
from public_mail_api.providers.cloudflare_public import (
    CloudflarePublicAdapter,
    UpstreamError,
)
from public_mail_api.rate_limit import RateLimiter, RateLimitExceeded
from public_mail_api.security import (
    decode_encryption_key,
    decrypt_address_jwt,
    encrypt_address_jwt,
    hash_capability,
    make_capability,
    remaining_seconds,
)
from public_mail_api.settings import Settings

router = APIRouter(prefix="/public-api/v1/mailboxes")


def settings(request: Request) -> Settings:
    return request.app.state.settings


def adapter(request: Request) -> CloudflarePublicAdapter:
    """Return an adapter backed by the app's upstream client.

    The fallback is useful for direct ASGI invocation outside the lifespan and
    records ownership so the replacement is closed during application
    shutdown.  Normal requests receive the client created by ``lifespan``.
    """
    client = request.app.state.upstream_client
    if client is None or getattr(client, "is_closed", False):
        import httpx

        client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        request.app.state.upstream_client = client
        request.app.state.owns_upstream_client = True
    return CloudflarePublicAdapter(client)


def _row(request: Request, db: Session, mailbox_id: UUID) -> PublicMailbox:
    row = db.get(PublicMailbox, str(mailbox_id))
    if row is None:
        raise HTTPException(404, "PUBLIC_MAILBOX_NOT_FOUND")

    now = utc_now()
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        row.status = "expired"
        db.commit()
        raise HTTPException(410, "PUBLIC_MAILBOX_EXPIRED")

    capability = request.cookies.get(settings(request).capability_cookie_name)
    if capability is None or hash_capability(capability) != row.token_hash:
        raise HTTPException(401, "PUBLIC_MAIL_CAPABILITY_REQUIRED")

    row.last_accessed_at = now
    db.commit()
    return row


def _upstream_error(exc: UpstreamError) -> HTTPException:
    return HTTPException(502, exc.code)


def _capability_for_request(request: Request) -> tuple[str, bool]:
    """Get the session capability, indicating whether a cookie was present."""
    value = request.cookies.get(settings(request).capability_cookie_name)
    if value:
        return value, True
    return make_capability(), False


@router.post("", status_code=201)
async def create(
    request: Request,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
):
    limiter: RateLimiter = request.app.state.rate_limiter
    ip = request.client.host if request.client else "unknown"
    try:
        limiter.check_create(ip)
    except RateLimitExceeded:
        raise HTTPException(429, "PUBLIC_MAIL_CREATE_RATE_LIMITED") from None

    capability, _had_cookie = _capability_for_request(request)
    token_hash = hash_capability(capability)
    now = utc_now()
    active_count = db.scalar(
        select(func.count())
        .select_from(PublicMailbox)
        .where(
            PublicMailbox.token_hash == token_hash,
            PublicMailbox.status == "active",
            PublicMailbox.expires_at > now,
        )
    )
    if active_count and active_count >= 5:
        raise HTTPException(429, "PUBLIC_MAIL_SESSION_LIMITED")

    try:
        created = await adapter(request).create_address(
            settings(request).upstream_url,
            settings(request).admin_key,
        )
    except UpstreamError as exc:
        raise _upstream_error(exc) from None

    expires_at = now + timedelta(seconds=settings(request).ttl_seconds)
    key = decode_encryption_key(settings(request).encryption_key)
    row = PublicMailbox(
        address=created.address,
        address_jwt_ciphertext=encrypt_address_jwt(key, created.jwt),
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Keep the same capability for an existing session so it can own up to
    # five active mailboxes.  A fresh browser receives a new capability.
    response.set_cookie(
        settings(request).capability_cookie_name,
        capability,
        httponly=True,
        secure=settings(request).cookie_secure,
        samesite="lax",
        path="/",
    )
    return {
        "id": str(row.id),
        "address": row.address,
        "status": row.status,
        "expires_at": row.expires_at.isoformat().replace("+00:00", "Z"),
        "remaining_seconds": settings(request).ttl_seconds,
    }


@router.get("/{mailbox_id}")
def info(
    mailbox_id: UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    row = _row(request, db, mailbox_id)
    return {
        "id": str(row.id),
        "address": row.address,
        "status": row.status,
        "expires_at": row.expires_at.isoformat().replace("+00:00", "Z"),
        "remaining_seconds": remaining_seconds(row.expires_at),
    }


@router.get("/{mailbox_id}/messages")
async def messages(
    mailbox_id: UUID,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    row = _row(request, db, mailbox_id)
    token = decrypt_address_jwt(
        decode_encryption_key(settings(request).encryption_key),
        row.address_jwt_ciphertext,
    )
    try:
        request.app.state.rate_limiter.check_list(row.token_hash)
    except RateLimitExceeded:
        raise HTTPException(429, "PUBLIC_MAIL_LIST_RATE_LIMITED") from None
    try:
        items = await adapter(request).list_messages(
            settings(request).upstream_url,
            token,
            row.address,
        )
    except UpstreamError as exc:
        raise _upstream_error(exc) from None
    return {
        "total": len(items),
        "items": [
            {key: value for key, value in item.items() if key != "body_text"}
            for item in items
        ],
    }


@router.get("/{mailbox_id}/messages/{message_id}")
async def message(
    mailbox_id: UUID,
    message_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    row = _row(request, db, mailbox_id)
    token = decrypt_address_jwt(
        decode_encryption_key(settings(request).encryption_key),
        row.address_jwt_ciphertext,
    )
    try:
        request.app.state.rate_limiter.check_detail(row.token_hash)
    except RateLimitExceeded:
        raise HTTPException(429, "PUBLIC_MAIL_DETAIL_RATE_LIMITED") from None
    try:
        item = await adapter(request).get_message(
            settings(request).upstream_url,
            token,
            row.address,
            message_id,
        )
    except UpstreamError as exc:
        raise _upstream_error(exc) from None
    return item

