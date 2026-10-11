"""机器接口（A/C/注册机调用）。

能力清单如实反映当前已移植的部分：没做完的一律 `false`，避免调用方以为某条链路可用。
"""

from fastapi import APIRouter, Depends, Request

from icloud_api.auth import require_operator

router = APIRouter(prefix="/icloud-api/v1", dependencies=[Depends(require_operator)])

# 每个子批次落地后改这里；调用方按这个清单决定走哪条链路。
FEATURES: dict[str, bool] = {
    "aliases_read": False,  # D1：解析与限速已移植，上游读接口在 aliases.py 客户端落地后打开
    "aliases_write": False,  # D4
    "mail_imap": False,  # D3
    "mail_web": False,  # D3
    "mail_netease_web": False,  # 明确不移植（163 网页取件不做）
    "browser_auth": False,  # D2
    "apple_srp_login": True,  # D1：SRP 数学与登录会话骨架
    "scheduler": False,  # D4
}


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "icloud-hme-engine"}


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "service": "icloud-hme-engine",
        "features": dict(FEATURES),
        "limits": {
            "alias_min_interval_seconds": settings.alias_min_interval_seconds,
            "scheduler_default_enabled": settings.scheduler_default_enabled,
            "browser_auth_ttl_seconds": settings.browser_auth_ttl_seconds,
        },
    }
