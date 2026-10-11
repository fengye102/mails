"""Independent D-stage iCloud Hide My Email engine."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine

from icloud_api.models import Base
from icloud_api.routes.machine import router as machine_router
from icloud_api.settings import Settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="iCloud Engine API", lifespan=lifespan)
    app.state.settings = resolved
    app.state.engine = engine or create_engine(resolved.database_url)
    if engine is None:
        Base.metadata.create_all(app.state.engine)

    app.include_router(machine_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app
