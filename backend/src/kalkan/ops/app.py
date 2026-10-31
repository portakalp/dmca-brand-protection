"""The ops application factory (architecture.md §19, §24)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from redis import Redis

from kalkan.config import ConfigError, Settings
from kalkan.db.session import Database
from kalkan.ops.pages import (
    auth,
    cases,
    customers,
    files,
    jobs,
    labels,
    notices,
    replies,
    review,
    system,
)
from kalkan.ops.pages import settings as settings_page
from kalkan.ops.web import HERE, CsrfFailed, LoginRequired, Services, is_htmx
from kalkan.storage.gcs import ObjectStore

# The pages load nothing from elsewhere; captured content is only ever shown escaped or as an
# image (files are served with a sandbox policy of their own, kalkan.ops.pages.files).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "object-src 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def build_services(settings: Settings) -> Services:
    if settings.redis_url is None:
        raise ConfigError("REDIS_URL is required")
    return Services(
        settings=settings,
        database=Database(settings),
        store=ObjectStore.from_settings(settings),
        redis=Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5),
    )


def create_app(settings: Settings, services: Services | None = None) -> FastAPI:
    found = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        found.database.dispose()

    app = FastAPI(
        title="Kalkan ops", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.services = found

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _: LoginRequired) -> Response:
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(CsrfFailed)
    async def csrf_failed(_: Request, __: CsrfFailed) -> Response:
        return PlainTextResponse("CSRF token missing or wrong; reload the page.", status_code=403)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    for module in (
        auth,
        review,
        cases,
        notices,
        replies,
        labels,
        customers,
        settings_page,
        jobs,
        system,
        files,
    ):
        app.include_router(module.router)
    return app
