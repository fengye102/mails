"""Owner-only provider configuration; credentials never enter responses."""

from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from mails_api.db import get_db
from mails_api.models import ProviderConnection, utc_now
from mails_api.providers.base import MailProvider, ProviderError
from mails_api.providers.factory import PROVIDER_KINDS, provider_for
from mails_api.security.crypto import decode_key, encrypt_secret
from mails_api.security.csrf import require_csrf, require_owner
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

router = APIRouter(prefix="/api/v1/connections", dependencies=[Depends(require_owner)])


class ConnectionInput(BaseModel):
    kind: Literal["outlook_plus", "cloudflare_temp_email", "cymail"]
    name: str = Field(min_length=1, max_length=256)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr = Field(min_length=1, max_length=8192)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 16 or any(not isinstance(k, str) or len(k) > 64 for k in value):
            raise ValueError("Invalid provider config")
        return value


def canonical_origin(value: str) -> str:
    try:
        if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.netloc.endswith(":")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or "?" in value
            or "#" in value
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="UPSTREAM_ORIGIN_INVALID") from None


def _is_http_loopback_origin(value: str) -> bool:
    try:
        parsed = urlsplit(canonical_origin(value))
        if parsed.scheme != "http" or urlsplit(value).path:
            return False
        return parsed.hostname == "localhost" or ip_address(parsed.hostname).is_loopback
    except (HTTPException, ValueError):
        return False


def allowed_origin(value: str, request: Request) -> str:
    origin = canonical_origin(value)
    settings = request.app.state.settings
    if origin not in {canonical_origin(item) for item in settings.upstream_origins}:
        raise HTTPException(status_code=422, detail="UPSTREAM_ORIGIN_NOT_ALLOWED")
    parsed = urlsplit(origin)
    if parsed.scheme == "http" and (
        not _is_http_loopback_origin(origin)
        or not _is_http_loopback_origin(settings.public_origin)
    ):
        raise HTTPException(status_code=422, detail="UPSTREAM_HTTPS_REQUIRED")
    return origin


def encryption_key(request: Request) -> bytes:
    try:
        return decode_key(request.app.state.settings.encryption_key)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=503, detail="ENCRYPTION_CONFIGURATION_INVALID"
        ) from None


async def get_http_client(request: Request):
    client = request.app.state.upstream_client
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        yield client


def get_adapter_for(kind: str, client: httpx.AsyncClient) -> MailProvider:
    if kind not in PROVIDER_KINDS:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_PROVIDER_KIND")
    return provider_for(kind, client)


def checked_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=value.tzinfo or timezone.utc)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def connection_payload(connection: ProviderConnection) -> dict:
    return {
        "id": str(connection.id),
        "kind": connection.kind,
        "config": {k: v for k, v in (connection.config_json or {}).items() if k != "secret"},
        "name": connection.name,
        "base_url": connection.base_url,
        "status": connection.status,
        "last_error": connection.last_error,
        "last_checked_at": checked_time(connection.last_checked_at),
    }


@router.post("", status_code=201, dependencies=[Depends(require_csrf)])
async def create_connection(
    body: ConnectionInput,
    request: Request,
    db: Annotated[DbSession, Depends(get_db)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> dict:
    origin = allowed_origin(body.base_url, request)
    key = encryption_key(request)
    secret = body.api_key.get_secret_value()
    if secret != secret.strip() or any(not 32 <= ord(c) <= 126 for c in secret):
        raise HTTPException(status_code=422, detail="API_KEY_INVALID")
    adapter = get_adapter_for(body.kind, client)
    try:
        await adapter.probe(origin, secret, body.config)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.code) from None
    connection = ProviderConnection(
        kind=body.kind,
        name=body.name,
        base_url=origin,
        encrypted_secret=encrypt_secret(key, secret),
        config_json=body.config,
        status="active",
        last_checked_at=utc_now(),
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection_payload(connection)


@router.get("")
def list_connections(db: Annotated[DbSession, Depends(get_db)]) -> list[dict]:
    return [
        connection_payload(item)
        for item in db.scalars(
            select(ProviderConnection).order_by(ProviderConnection.id)
        )
    ]
