"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

import dbx
from dbx.config import Config
from dbx.environment import environment_snapshot
from dbx.logs import MemorySampler, get_logger, memory_snapshot

# Built frontend (pnpm build) is served from here when present.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(cfg: Config) -> FastAPI:
    log = get_logger("dbx.app")
    sampler = MemorySampler(cfg.logging.memory_sample_interval, cfg.cache_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "startup",
            **environment_snapshot(),
            data_roots=[r.name for r in cfg.data_roots],
            **memory_snapshot(cfg.cache_dir),
        )
        sampler.start()
        yield
        sampler.stop()
        log.info("shutdown")

    app = FastAPI(title="dbx", version=dbx.__version__, lifespan=lifespan)
    app.state.config = cfg

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", method=request.method, path=request.url.path)
            raise
        ms = round((time.perf_counter() - t0) * 1000, 2)
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=ms,
        )
        response.headers["x-request-id"] = request_id
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
