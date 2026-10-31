"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import IO

from fastapi import FastAPI

from kalkan import __version__
from kalkan.api import evidence, health, v1, webhooks
from kalkan.api.middleware import EdgeAuthMiddleware, RequestContextMiddleware
from kalkan.api.problems import register_problem_handlers
from kalkan.config import Settings
from kalkan.db.engine import create_db_engine
from kalkan.db.session import Database
from kalkan.storage.gcs import ObjectStore


def create_app(settings: Settings) -> FastAPI:
    engine = create_db_engine(settings.database_url_api) if settings.database_url_api else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            engine.dispose()
        app.state.database.dispose()

    local = settings.env == "local"
    app = FastAPI(
        title="Kalkan API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if local else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if local else None,
    )
    app.state.settings = settings
    app.state.db_engine = engine
    # The customer API and the evidence pages (§17): resolvers and RLS sessions as kalkan_api,
    # files from storage.
    app.state.database = Database(settings)
    app.state.evidence_limiter = evidence.RateLimiter()
    app.state.store = _LazyStore(settings)

    register_problem_handlers(app)
    edge_secret = settings.edge_shared_secret
    app.add_middleware(
        EdgeAuthMiddleware, secret=edge_secret.get_secret_value() if edge_secret else None
    )
    # Added last, so it runs first: edge rejections also carry a request id.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(evidence.router)
    app.include_router(webhooks.router)
    app.include_router(v1.router)
    return app


class _LazyStore:
    """The storage client is made on the first evidence file, not at start-up."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store: ObjectStore | None = None

    def open_stream(self, key: str) -> IO[bytes]:
        if self._store is None:
            self._store = ObjectStore.from_settings(self._settings)
        return self._store.open_stream(key)
