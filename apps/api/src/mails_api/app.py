import httpx
from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from sqlalchemy import Engine, create_engine
from starlette.responses import JSONResponse

from mails_api.routes.connections import router as connections_router
from mails_api.routes.mailboxes import router as mailboxes_router
from mails_api.routes.session import router as session_router
from mails_api.security.login_limit import LoginLimiter
from mails_api.settings import Settings


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings()
    app = FastAPI(title="Mails API")
    app.state.settings = settings
    app.state.engine = (
        engine if engine is not None else create_engine(settings.database_url)
    )
    app.state.upstream_client = upstream_client
    app.state.login_limiter = LoginLimiter()
    app.include_router(session_router)
    app.include_router(connections_router)
    app.include_router(mailboxes_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors include submitted values; never echo provider secrets.
        if request.url.path.startswith("/api/v1/connections"):
            return JSONResponse(status_code=422, content={"detail": "REQUEST_INVALID"})
        return await request_validation_exception_handler(request, exc)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
