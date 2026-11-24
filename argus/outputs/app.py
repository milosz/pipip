"""Standalone web application and its background scheduler."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from argus.config import AppConfig
from argus.core.engine import ArgusEngine
from argus.db.session import Database
from argus.locking import FileLock
from argus.outputs.api import api_router
from argus.outputs.atom import AtomOutput
from argus.outputs.badges import BadgesOutput
from argus.outputs.dashboard import Dashboard
from argus.outputs.metrics import metrics_router
from argus.scheduler import ArgusScheduler


def create_app(settings: AppConfig) -> FastAPI:
    database = Database(settings.database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        path = database.path
        lock = FileLock(path.with_name(f"{path.name}.lock")) if path else nullcontext()
        with lock:
            async with AsyncExitStack() as cleanup:
                cleanup.push_async_callback(database.dispose)
                engine = ArgusEngine(settings, database)
                scheduler = ArgusScheduler(settings, engine)
                cleanup.push_async_callback(scheduler.shutdown)
                await database.init()
                await engine.sync_targets()
                await scheduler.start()
                yield

    app = FastAPI(
        title=settings.name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'"
        )
        return response

    if settings.http.root_path:
        root_path = settings.http.root_path
        route_prefix = f"{root_path}/"

        @app.middleware("http")
        async def configure_root_path(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            path = request.scope["path"]
            if path == "/health":  # Docker's healthcheck always uses this path.
                return await call_next(request)
            if path != root_path and not path.startswith(route_prefix):
                return Response(status_code=404)
            request.scope["root_path"] = root_path
            return await call_next(request)

    @app.get("/health")
    async def health() -> Response:
        return Response(status_code=204 if await database.ping() else 503)

    if settings.api.enabled:
        app.include_router(api_router(settings, database))
    if settings.metrics.enabled:
        app.include_router(metrics_router(database))
    app.include_router(Dashboard(settings, database).router())
    if settings.atom.enabled:
        app.include_router(AtomOutput(settings, database).router())
    if settings.badges.enabled:
        app.include_router(BadgesOutput(settings, database).router())
    return app
