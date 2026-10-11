"""Independent D-stage iCloud Hide My Email engine."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine

from icloud_api.browser_auth import CookieValidator, validate_cookies_upstream
from icloud_api.mail import service as mail_service
from icloud_api.models import Base
from icloud_api.routes.admin import router as admin_router
from icloud_api.routes.machine import router as machine_router
from icloud_api.routes.public import router as public_router
from icloud_api.settings import Settings

AppleAccountBootstrapper = Callable[[dict[str, str]], Awaitable[bool]]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    *,
    cookie_validator: CookieValidator = validate_cookies_upstream,
    apple_account_bootstrapper: AppleAccountBootstrapper | None = None,
    imap_client_factory=mail_service.build_imap_client,
) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="iCloud Engine API", lifespan=lifespan)
    app.state.settings = resolved
    app.state.engine = engine or create_engine(resolved.database_url)
    # 浏览器授权回填时的上游校验、Apple Account 引导、IMAP 客户端构造都从这里注入，
    # 测试用假上游替换。
    app.state.cookie_validator = cookie_validator
    app.state.apple_account_bootstrapper = apple_account_bootstrapper
    app.state.imap_client_factory = imap_client_factory
    if engine is None:
        Base.metadata.create_all(app.state.engine)

    app.include_router(machine_router)
    app.include_router(admin_router)
    app.include_router(public_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app
