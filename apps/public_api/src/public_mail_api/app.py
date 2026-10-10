"""Application factory for the independently deployed B1 API."""
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, create_engine

from public_mail_api.rate_limit import RateLimiter
from public_mail_api.settings import Settings


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the isolated public-mail application."""
    resolved_settings = settings or Settings()
    app = FastAPI(title="Public Mail API", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.engine = engine or create_engine(resolved_settings.database_url)
    app.state.upstream_client = upstream_client
    app.state.owns_upstream_client = upstream_client is None
    app.state.rate_limiter = RateLimiter()
    if resolved_settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        )

    from public_mail_api.routes.mailboxes import router as mailbox_router

    app.include_router(mailbox_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create an upstream HTTP client when the app owns one."""
    if app.state.upstream_client is None:
        app.state.upstream_client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )
        app.state.owns_upstream_client = True
    try:
        yield
    finally:
        client = app.state.upstream_client
        if client is not None and app.state.owns_upstream_client:
            await client.aclose()