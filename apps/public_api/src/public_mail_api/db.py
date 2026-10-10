from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from public_mail_api.models import Base

__all__ = ["Base", "get_db"]


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session
