import httpx
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine

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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
