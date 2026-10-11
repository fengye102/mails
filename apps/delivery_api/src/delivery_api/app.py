"""Independent C-stage inventory and pickup service."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from delivery_api.cleanup import cleanup_expired_allocations
from delivery_api.models import Base
from delivery_api.routes.inventory import router as inventory_router
from delivery_api.routes.pickups import router as pickups_router
from delivery_api.settings import Settings


async def periodic_cleanup(app: FastAPI) -> None:
    """Release expired allocations without blocking request handling."""
    interval = max(1, app.state.settings.cleanup_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            with Session(app.state.engine) as db:
                cleanup_expired_allocations(db)
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


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="Delivery API", lifespan=lifespan)
    app.state.settings = resolved
    app.state.engine = engine or create_engine(resolved.database_url)
    if engine is None:
        Base.metadata.create_all(app.state.engine)
    app.include_router(inventory_router)
    app.include_router(pickups_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app
