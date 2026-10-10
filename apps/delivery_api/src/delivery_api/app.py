"""Independent C-stage inventory and pickup service."""
from fastapi import FastAPI
from sqlalchemy import Engine, create_engine

from delivery_api.models import Base
from delivery_api.routes.inventory import router as inventory_router
from delivery_api.routes.pickups import router as pickups_router
from delivery_api.settings import Settings


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="Delivery API")
    app.state.settings = resolved
    app.state.engine = engine or create_engine(resolved.database_url)
    if engine is None:
        Base.metadata.create_all(app.state.engine)
    app.include_router(inventory_router)
    app.include_router(pickups_router)
    app.add_api_route("/healthz", lambda: {"status": "ok"}, methods=["GET"])
    return app
