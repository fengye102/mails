"""机器接口鉴权：Bearer 运营 token（与 C 的运营 token 同构，比较用固定时间实现）。"""

import hmac

from fastapi import Header, HTTPException, Request


def require_operator(request: Request, authorization: str | None = Header(default=None)) -> None:
    expected = request.app.state.settings.operator_token
    if not expected:
        raise HTTPException(status_code=503, detail="ICLOUD_OPERATOR_TOKEN is not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing operator token")
    provided = authorization[len(prefix) :].strip()
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid operator token")
