"""公开接口：浏览器授权回填。

没有鉴权头——一次性授权码本身就是凭据（`iba_` + 32 字节随机数，只存 SHA-256，TTL 5 分钟，单次有效）。
用户在自己浏览器里登录 icloud.com 后，把 Cookie 提交到这里即完成授权。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from icloud_api.accounts import resolve_key
from icloud_api.browser_auth import BrowserAuthError, complete_browser_auth
from icloud_api.db import get_db

router = APIRouter(prefix="/icloud-api/public")


class CompleteBrowserAuthRequest(BaseModel):
    authorization_code: str = Field(min_length=1)
    cookies: dict[str, str] = Field(default_factory=dict)


@router.post("/browser-auth/complete")
async def complete_browser_auth_route(
    payload: CompleteBrowserAuthRequest, request: Request, db: Annotated[Session, Depends(get_db)]
) -> dict:
    settings = request.app.state.settings
    key = resolve_key(settings.data_key)
    try:
        return await complete_browser_auth(
            db,
            authorization_code=payload.authorization_code,
            cookies=dict(payload.cookies),
            data_key=key,
            validate=request.app.state.cookie_validator,
            bootstrap_apple_account=request.app.state.apple_account_bootstrapper,
        )
    except BrowserAuthError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from error
