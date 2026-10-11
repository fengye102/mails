"""机器接口（A/C/注册机调用）。

能力清单分两栏如实上报：`shipped` = 代码已落地，`verified_live` = 已在真实 Apple 账号上跑通。
没做完的链路一律 `shipped: false`，避免调用方以为某条链路可用；
还没上真机验证的链路 `verified_live: false`，调用方可以据此决定是否先用假上游/降级路径。
"""

from fastapi import APIRouter, Depends, Request

from icloud_api.auth import require_operator

router = APIRouter(prefix="/icloud-api/v1", dependencies=[Depends(require_operator)])

# 每个子批次落地后改这里；调用方按这个清单决定走哪条链路。
SHIPPED_FEATURES: dict[str, bool] = {
    "aliases_read": True,  # D1：会话校验 + 别名/转发设置读取（含假上游测试）
    "aliases_write": False,  # D4：客户端方法已移植，但建/停/删接口未暴露
    "mail_imap": False,  # D3
    "mail_web": False,  # D3
    "mail_netease_web": False,  # 明确不移植（163 网页取件不做）
    "browser_auth": False,  # D2
    "apple_srp_login": True,  # D1：SRP 数学与登录会话骨架
    "scheduler": False,  # D4
}

# 只有拿真实账号跑通过的链路才进来；上真机前一律留在外面。
LIVE_VERIFIED_FEATURES: frozenset[str] = frozenset()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "icloud-hme-engine"}


@router.get("/capabilities")
def capabilities(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "service": "icloud-hme-engine",
        "features": {
            name: {"shipped": shipped, "verified_live": name in LIVE_VERIFIED_FEATURES}
            for name, shipped in SHIPPED_FEATURES.items()
        },
        "limits": {
            "alias_min_interval_seconds": settings.alias_min_interval_seconds,
            "scheduler_default_enabled": settings.scheduler_default_enabled,
            "browser_auth_ttl_seconds": settings.browser_auth_ttl_seconds,
        },
    }
