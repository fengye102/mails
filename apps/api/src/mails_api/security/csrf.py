"""Reusable session CSRF, login origin, and owner authorization checks."""

import hmac
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from mails_api.models import Session, User
from mails_api.security.sessions import get_current_session
from sqlalchemy.orm import object_session


def require_csrf(
    request: Request, session: Annotated[Session, Depends(get_current_session)]
) -> None:
    provided = request.headers.get("x-csrf-token", "")
    if not provided or not hmac.compare_digest(
        provided.encode(), session.csrf_token.encode()
    ):
        raise HTTPException(status_code=403, detail="CSRF_TOKEN_INVALID")


def current_user(
    session: Annotated[Session, Depends(get_current_session)],
) -> User:
    db = object_session(session)
    user = db.get(User, session.user_id) if db is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_member(
    user: Annotated[User, Depends(current_user)],
) -> User:
    if user.role not in {"owner", "member"}:
        raise HTTPException(status_code=403, detail="MEMBER_REQUIRED")
    return user


def require_owner(
    user: Annotated[User, Depends(current_user)],
) -> User:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="OWNER_REQUIRED")
    return user

def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port
        return (
            parsed.scheme,
            parsed.hostname,
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def require_login_origin(request: Request) -> None:
    # A present Origin takes precedence; a foreign/null value cannot fall back.
    provided = request.headers.get("origin")
    if provided is None:
        provided = request.headers.get("referer", "")
    expected = _origin(request.app.state.settings.public_origin)
    if expected is None or _origin(provided) != expected:
        raise HTTPException(status_code=403, detail="LOGIN_ORIGIN_INVALID")
