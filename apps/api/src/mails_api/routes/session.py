"""Owner login, session inspection, and server-side logout."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from mails_api.db import get_db
from mails_api.models import Session, User, utc_now
from mails_api.security.csrf import require_csrf, require_login_origin
from mails_api.security.passwords import verify_password
from mails_api.security.sessions import COOKIE_NAME, create_session, get_current_session
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

router = APIRouter(prefix="/api/v1")


class LoginCredentials(BaseModel):
    username: str
    password: str


def _session_payload(session: Session, db: DbSession) -> dict:
    user = db.get(User, session.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return {
        "user": {"id": str(user.id), "username": user.username, "role": user.role},
        "csrf_token": session.csrf_token,
    }


@router.post("/session")
def login(
    credentials: LoginCredentials,
    request: Request,
    response: Response,
    db: Annotated[DbSession, Depends(get_db)],
) -> dict:
    require_login_origin(request)
    peer_ip = request.client.host if request.client is not None else ""
    limiter = request.app.state.login_limiter
    now = utc_now()
    if limiter.is_limited(credentials.username, peer_ip, now):
        raise HTTPException(status_code=429, detail="LOGIN_RATE_LIMITED")
    user = db.scalar(select(User).where(User.username == credentials.username))
    if user is None or not verify_password(user.password_hash, credentials.password):
        limiter.record_failure(credentials.username, peer_ip, now)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token, session = create_session(db, user, now)
    db.commit()
    limiter.reset(credentials.username, peer_ip)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=urlsplit(request.app.state.settings.public_origin).scheme == "https",
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_payload(session, db)


@router.get("/session")
def current_session(
    response: Response,
    session: Annotated[Session, Depends(get_current_session)],
    db: Annotated[DbSession, Depends(get_db)],
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _session_payload(session, db)


@router.delete("/session", status_code=204)
def logout(
    request: Request,
    session: Annotated[Session, Depends(get_current_session)],
    db: Annotated[DbSession, Depends(get_db)],
) -> Response:
    require_csrf(request, session)
    session.revoked_at = utc_now()
    db.commit()
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=urlsplit(request.app.state.settings.public_origin).scheme == "https",
        samesite="lax",
        path="/",
    )
    return response
