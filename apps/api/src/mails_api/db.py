from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from mails_api.models import Base

__all__ = ["Base", "get_db"]


def get_db(request: Request) -> Iterator[Session]:
    """Close the request session; callers explicitly commit successful writes."""
    with Session(request.app.state.engine) as session:
        yield session
