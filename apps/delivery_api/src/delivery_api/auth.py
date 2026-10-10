"""Authentication dependencies for the independent delivery service."""

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, Request


def require_operator(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require the C-stage operator bearer token without leaking config values."""

    expected = request.app.state.settings.operator_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, {"code": "DELIVERY_OPERATOR_REQUIRED"})
