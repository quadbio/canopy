"""FastAPI application factory."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import dbx
from dbx.build import build_id
from dbx.catalog import Catalog
from dbx.catalog.api import router as catalog_router
from dbx.config import Config
from dbx.environment import environment_snapshot
from dbx.logs import MemorySampler, get_logger, memory_snapshot

# Built frontend (pnpm build) is served from here when present.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# Client-supplied request ids are kept only if short and plain (they end up in every log line).
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _route(request: Request) -> str:
    """The matched route template (e.g. /api/catalog/datasets/{id}): no ids or names."""
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or "<static>")


def create_app(cfg: Config, *, background: bool = True) -> FastAPI:
    """`background=False` (tests) skips the catalog's scan thread; scans are then run directly."""
    log = get_logger("dbx.app")
    sampler = MemorySampler(cfg.logging.memory_sample_interval, cfg.cache_dir)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            **environment_snapshot(cfg),
            data_roots=[r.name for r in cfg.data_roots],
            **memory_snapshot(cfg.cache_dir),
        )
        sampler.start()
        if background:
            catalog.start()
        yield
        catalog.stop()
        sampler.stop()
        log.info("shutdown")

    app = FastAPI(title="dbx", version=dbx.__version__, lifespan=lifespan)
    app.state.config = cfg
    app.state.catalog = catalog
    app.include_router(catalog_router)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get("x-request-id", "")
        request_id = given if REQUEST_ID.fullmatch(given) else uuid.uuid4().hex[:12]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Every failure still gets a status, a duration and the request id in the reply,
            # so a user's screenshot of the error can be matched to the log.
            ms = round((time.perf_counter() - t0) * 1000, 2)
            log.exception(
                "request_failed",
                method=request.method,
                route=_route(request),
                path=request.url.path,
                status=500,
                ms=ms,
            )
            response = JSONResponse(
                {"detail": "internal error", "request_id": request_id}, status_code=500
            )
        else:
            ms = round((time.perf_counter() - t0) * 1000, 2)
            log.info(
                "request",
                method=request.method,
                route=_route(request),
                path=request.url.path,
                status=response.status_code,
                ms=ms,
            )
        response.headers["x-request-id"] = request_id
        response.headers["x-dbx-build"] = build_id()
        response.headers["server-timing"] = f"total;dur={ms}"
        return response

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": dbx.__version__}

    @app.get("/api/version")
    def version_info() -> dict[str, object]:
        return environment_snapshot()

    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app
