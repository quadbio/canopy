"""FastAPI application factory: routers, middleware (request ids, timing, host and POST checks,
gzip), error envelope, and the housekeeping thread (slow-request watchdog, idle cache sweep)."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

import canopy
from canopy.auth import COOKIE, LOGIN_PAGE, OPEN_PATHS, access_token
from canopy.auth import valid as auth_valid
from canopy.build import build_id
from canopy.catalog import Catalog
from canopy.catalog.api import router as catalog_router
from canopy.config import Config
from canopy.data.api import router as datasets_router
from canopy.data.cache import DatasetCache
from canopy.environment import environment_snapshot
from canopy.errors import error_response, install_error_handlers
from canopy.jobs.api import router as jobs_router
from canopy.jobs.manager import JobManager
from canopy.logs import MemorySampler, get_logger, memory_snapshot, set_rollover_hook
from canopy.stores import SLOW_MS
from canopy.stores import router as stores_router
from canopy.stores import stats as store_stats
from canopy.telemetry import router as telemetry_router

# Built frontend (pnpm build) is served from here when present.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
# Client-supplied request ids are kept only if short and plain (they end up in every log line).
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class ApiGZip:
    """gzip for API replies and the app's own files when the client accepts it. Store files are
    left alone (read by byte range, already zstd-compressed), and so are replies that set
    `content-encoding: identity` (float arrays, which barely compress)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=1)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/stores/"):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class SpaStaticFiles(StaticFiles):
    """The built frontend; unknown paths without a file extension get index.html, so links into
    the app (e.g. /datasets/…) survive a reload. /api paths are never answered with the page."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404 or path.startswith("api") or "." in path.rsplit("/", 1)[-1]:
                raise
            response = await super().get_response("index.html", scope)
        # Built assets have content hashes in their names: cache them for good. The page itself
        # must be revalidated so a new build is picked up.
        immutable = path.startswith("assets/") and response.status_code in (200, 304)
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if immutable else "no-cache"
        )
        return response


class InFlight:
    """Requests being served, so a hang shows up in the log while it lasts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, Scope]] = {}
        self._reported: dict[str, float] = {}

    def start(self, request_id: str, scope: Scope) -> None:
        with self._lock:
            self._items[request_id] = (time.monotonic(), scope)

    def end(self, request_id: str) -> None:
        with self._lock:
            self._items.pop(request_id, None)
            self._reported.pop(request_id, None)

    def report_slow(self, threshold_s: float, every_s: float = 30.0) -> None:
        now = time.monotonic()
        with self._lock:
            slow = [
                (rid, now - t0, scope)
                for rid, (t0, scope) in self._items.items()
                if now - t0 >= threshold_s and now - self._reported.get(rid, 0.0) >= every_s
            ]
            for rid, _, _ in slow:
                self._reported[rid] = now
            n = len(self._items)
        log = get_logger("canopy.app")
        for rid, age, scope in slow:
            route = scope.get("route")
            log.warning(
                "slow_request",
                request_id=rid,
                route=str(getattr(route, "path", None) or "<routing>"),
                method=scope.get("method"),
                dataset_id=(scope.get("path_params") or {}).get("id"),
                age_s=round(age, 1),
                n_in_flight=n,
            )


class Housekeeper:
    """One background thread: reports slow requests every few seconds and drops idle cache
    entries (so memory is released even when nobody uses the server)."""

    def __init__(self, inflight: InFlight, datasets: DatasetCache, slow_s: float) -> None:
        self.inflight, self.datasets, self.slow_s = inflight, datasets, slow_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="housekeeper", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        tick = 0
        while not self._stop.wait(5):
            tick += 1
            try:
                self.inflight.report_slow(self.slow_s)
                if tick % 6 == 0:
                    self.datasets.sweep()
            except Exception as e:  # housekeeping must never die
                get_logger("canopy.app").warning("housekeeping_failed", error=type(e).__name__)


def _route(request: Request) -> str:
    """The matched route template (e.g. /api/catalog/datasets/{id}): no ids or names."""
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or "<static>")


def _authorised(request: Request, token: str) -> bool:
    return request.url.path in OPEN_PATHS or auth_valid(request, token)


def _auth_required(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return error_response(
            request,
            401,
            "auth_required",
            "Open the access link printed when the server started (canopy link shows it again).",
        )
    return HTMLResponse(LOGIN_PAGE, status_code=401)


def _remember_token(request: Request, response: Response, token: str) -> Response:
    """The link's token becomes a cookie; a page request is redirected to the address without
    it, so the token does not stay in the address bar or the browser history."""
    if not request.url.path.startswith("/api/") and request.method == "GET":
        rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
        url = request.url.path + (f"?{urlencode(rest)}" if rest else "")
        response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="strict", max_age=400 * 24 * 3600, path="/"
    )
    return response


def create_app(cfg: Config, *, background: bool = True) -> FastAPI:
    """`background=False` (tests) skips the catalog's scan thread; scans are then run directly."""
    log = get_logger("canopy.app")
    sampler = MemorySampler(cfg.logging.memory_sample_interval, cfg.cache_dir)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(cfg)
    datasets = DatasetCache(cfg)

    def converted(job: Any) -> None:
        # the next request on this dataset reads the converted store
        datasets.drop_version(job.version)
        log.info("dataset_converted", dataset_id=job.dataset, job=job.id)

    jobs = JobManager(cfg, wal=catalog.wal, on_done=converted)
    inflight = InFlight()
    housekeeper = Housekeeper(inflight, datasets, cfg.server.slow_request_s)
    allowed = {h.lower() for h in cfg.server.allowed_hosts}
    token = access_token(cfg.state) if cfg.server.auth == "token" else None

    def log_environment(event: str = "startup") -> None:
        log.info(
            event,
            **environment_snapshot(cfg),
            data_roots=[r.name for r in cfg.data_roots],
            allowed_hosts=sorted(allowed),
            auth=cfg.server.auth,
            file_access=cfg.server.file_access,
            **memory_snapshot(cfg.cache_dir),
        )
        open_to_others = [
            name
            for name, d in (
                ("cache", cfg.cache_dir),
                ("state", cfg.state),
                ("logs", cfg.logging.dir),
            )
            if d.exists() and d.stat().st_mode & 0o007
        ]
        if open_to_others:
            # created before file_access applied, or by hand: other users may read them
            log.warning("folders_readable_by_others", folders=open_to_others)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log_environment()
        # Every new log file starts with the environment, so a rotated log is self-contained.
        set_rollover_hook(lambda: log_environment("environment"))
        sampler.start()
        housekeeper.start()
        if background:
            catalog.start()
            jobs.start()
        yield
        jobs.stop()
        catalog.stop()
        housekeeper.stop()
        datasets.clear()
        sampler.stop()
        set_rollover_hook(None)
        store_stats.flush(log)
        log.info("shutdown")

    # no interactive API pages: they are a map of the API for anyone who reaches the port
    app = FastAPI(
        title="Canopy",
        version=canopy.__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = cfg
    app.state.catalog = catalog
    app.state.datasets = datasets
    app.state.jobs = jobs
    app.include_router(catalog_router)
    app.include_router(datasets_router)
    app.include_router(stores_router)
    app.include_router(telemetry_router)
    app.include_router(jobs_router)
    install_error_handlers(app)
    app.add_middleware(ApiGZip)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        given = request.headers.get("x-request-id", "")
        request_id = given if REQUEST_ID.fullmatch(given) else uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        t0 = time.perf_counter()
        inflight.start(request_id, request.scope)
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        try:
            if "*" not in allowed and host not in allowed:
                # A page on another site that resolves its own name to this machine (DNS
                # rebinding) arrives with a foreign Host header.
                response: Response = error_response(
                    request,
                    400,
                    "host_not_allowed",
                    "This host name is not allowed; add it to server.allowed_hosts.",
                )
            elif token is not None and not _authorised(request, token):
                response = _auth_required(request)
            elif request.method in ("POST", "PUT", "DELETE") and (
                request.headers.get("content-type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                # Other sites can only send "simple" cross-site POSTs (form or text bodies);
                # requiring JSON makes the browser ask first, and this server never agrees.
                response = error_response(
                    request, 415, "json_required", "Send this request as application/json."
                )
            else:
                response = await call_next(request)
                if token is not None and request.query_params.get("token"):
                    response = _remember_token(request, response, token)
        except Exception:
            # Every failure still gets a status, a duration and the request id in the reply,
            # so a user's screenshot of the error can be matched to the log.
            log.exception(
                "request_failed",
                method=request.method,
                route=_route(request),
                path=request.url.path,
                dataset_id=(request.scope.get("path_params") or {}).get("id"),
                status=500,
                ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            response = error_response(
                request,
                500,
                "internal_error",
                f"Internal error. The server log has the details (request {request_id}).",
            )
        else:
            ms_req = (time.perf_counter() - t0) * 1000
            if (
                request.url.path.startswith("/api/stores/")
                and (response.status_code < 400 or response.status_code == 404)
                and ms_req < SLOW_MS
            ):
                # many small range reads: summed per minute (canopy.stores.StoreStats)
                store_stats.record(
                    request.url.path,
                    response.status_code,
                    int(response.headers.get("content-length") or 0),
                    ms_req,
                    log,
                )
            else:
                log.info(
                    "request",
                    method=request.method,
                    route=_route(request),
                    path=request.url.path,
                    dataset_id=(request.scope.get("path_params") or {}).get("id"),
                    status=response.status_code,
                    code=getattr(request.state, "error_code", None),
                    bytes=response.headers.get("content-length"),
                    ms=round((time.perf_counter() - t0) * 1000, 2),
                )
        finally:
            inflight.end(request_id)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        response.headers["x-request-id"] = request_id
        response.headers["x-canopy-build"] = build_id()
        response.headers["server-timing"] = f"total;dur={ms}"
        return response

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"status": "ok", "version": canopy.__version__}

    @app.get("/api/version")
    def version_info() -> dict[str, object]:
        return environment_snapshot()

    if FRONTEND_DIST.is_dir():
        app.mount("/", SpaStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app
