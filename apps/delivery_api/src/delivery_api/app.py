"""Independent C-stage inventory and pickup service."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from delivery_api import pool
from delivery_api.cleanup import cleanup_expired_allocations
from delivery_api.models import Base
from delivery_api.routes.inventory import router as inventory_router
from delivery_api.routes.pickups import router as pickups_router
from delivery_api.routes.pool import admin_router as pool_admin_router
from delivery_api.routes.pool import public_router as pool_public_router
from delivery_api.routes.pool_verify import compat_router as pool_compat_router
from delivery_api.routes.pool_verify import verify_router as pool_verify_router
from delivery_api.settings import Settings


async def periodic_cleanup(app: FastAPI) -> None:
    """Release expired allocations without blocking request handling."""
    interval = max(1, app.state.settings.cleanup_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            with Session(app.state.engine) as db:
                cleanup_expired_allocations(db)
                # 池租约回收挂在这个循环上：注册机停了、没有流量进来时，也得把过期租约收回来，
                # 否则邮箱会一直显示 claimed，直到下一次有人来领取才被动自愈。
                pool.normalise_pool(db, app.state.settings)
        except Exception:  # noqa: BLE001 - cleanup must survive isolated failures
            logging.getLogger(__name__).warning("Expired allocation cleanup failed.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_cleanup(app))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        app.state.icloud_engine_client.close()


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="Delivery API", lifespan=lifespan)
    app.state.settings = resolved
    app.state.engine = engine or create_engine(resolved.database_url)
    # 引擎 HTTP 客户端挂在 app 上：同步接口与取件刷新共用一条连接池；
    # 测试覆盖 app.state.icloud_engine_client 即可注入假上游。
    app.state.icloud_engine_client = httpx.Client(follow_redirects=False, trust_env=False)
    # 池限流器挂在 app 上（而不是模块级单例）：每个应用实例一份，测试之间不会互相串。
    app.state.pool_rate_limiter = pool.RateLimiter()
    if engine is None:
        Base.metadata.create_all(app.state.engine)

    @app.middleware("http")
    async def public_pickup_headers(request, call_next):  # type: ignore[no-untyped-def]
        """公开取件路由与池接口统一加固：不缓存、不嗅探、不带来源、禁一切子资源。

        建池 Key 的响应里有明文 Key，同样按这个口径（不许进任何缓存）。
        """
        response = await call_next(request)
        guarded = ("/delivery-api/v1/pickups", "/pool-api/", "/delivery-api/v1/pool/keys")
        if request.url.path.startswith(guarded):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            )
        return response

    app.include_router(inventory_router)
    app.include_router(pickups_router)
    app.include_router(pool_admin_router)
    app.include_router(pool_public_router)
    app.include_router(pool_verify_router)
    app.include_router(pool_compat_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app
