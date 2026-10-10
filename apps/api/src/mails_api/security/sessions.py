"""Opaque cookie tokens backed by revocable database sessions."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from mails_api.db import get_db
from mails_api.models import Session, User, utc_now
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

COOKIE_NAME = "mails_session"


def create_session(db: DbSession, user: User, now: datetime) -> tuple[str, Session]:
    raw_token = secrets.token_urlsafe(32)
    session = Session(
        user_id=user.id,
        token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=8),
    )
    db.add(session)
    db.flush()
    return raw_token, session


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; PostgreSQL preserves the UTC offset.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def get_current_session(
    request: Request, db: Annotated[DbSession, Depends(get_db)]
) -> Session:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    session = db.scalar(
        select(Session).where(
            Session.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )
    )
    now = utc_now()
    if (
        session is None
        or session.revoked_at is not None
        or _as_utc(session.expires_at) <= now
        or _as_utc(session.last_seen_at) < now - timedelta(minutes=30)
    ):
        raise HTTPException(status_code=401, detail="Authentication required")
    session.last_seen_at = now
    db.commit()
    return session
