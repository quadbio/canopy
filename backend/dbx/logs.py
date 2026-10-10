"""Structured logging: JSON lines to rotating files, plus timing and memory helpers.

Every log event is one JSON object per line. Events carry the request or job id bound in
the current context, so a request's stages can be reassembled from the log.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import structlog

from dbx.config import LoggingConfig

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LOG_FILE = "dbx.jsonl"


def default_log_dir() -> Path:
    """Per-user fallback log folder (never the current directory)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "dbx" / "logs"


def _writable_dir(*candidates: Path) -> Path | None:
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(d, os.W_OK):
            return d
    return None


def setup_logging(cfg: LoggingConfig, *, to_console: bool = True) -> Path | None:
    """Configure structlog to write JSON lines to a rotating file (and readable console).

    Returns the log folder actually used: the configured one, else the per-user fallback, else
    None (file logging off) when neither is writable, e.g. on a read-only share.
    """
    log_dir = _writable_dir(cfg.dir, default_log_dir())
    level = _LEVELS[cfg.level]

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    handlers: list[logging.Handler] = []
    if log_dir is not None:
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / LOG_FILE, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(), foreign_pre_chain=shared
            )
        )
        handlers.append(file_handler)
    if to_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=False), foreign_pre_chain=shared
            )
        )
        handlers.append(console)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)
    # uvicorn's own loggers go through the same handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    return log_dir


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def rss_mb() -> float:
    return float(psutil.Process(os.getpid()).memory_info().rss) / 2**20


@contextmanager
def timed(stage: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log the duration and memory change of a block as a `stage` event.

    Yields a dict the block can add fields to (e.g. bytes read); they are logged on exit.
    Failures are logged with the exception and re-raised.
    """
    log = get_logger("dbx.timing")
    extra: dict[str, Any] = {}
    t0 = time.perf_counter()
    m0 = rss_mb()
    try:
        yield extra
    except Exception:
        log.exception("stage_failed", stage=stage, ms=_ms(t0), **fields, **extra)
        raise
    log.info(
        "stage",
        stage=stage,
        ms=_ms(t0),
        rss_mb=round(rss_mb(), 1),
        rss_delta_mb=round(rss_mb() - m0, 1),
        **fields,
        **extra,
    )


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def memory_snapshot(cache_dir: Path | None = None) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    snap: dict[str, Any] = {
        "rss_mb": round(rss_mb(), 1),
        "host_total_gb": round(vm.total / 2**30, 1),
        "host_available_gb": round(vm.available / 2**30, 1),
    }
    cgroup = _cgroup_memory()
    if cgroup:
        snap.update(cgroup)
    if cache_dir is not None and cache_dir.exists():
        du = psutil.disk_usage(str(cache_dir))
        snap["cache_disk_free_gb"] = round(du.free / 2**30, 1)
    return snap


def _cgroup_memory() -> dict[str, float]:
    """cgroup v2 usage and limit of this process (Linux only; empty elsewhere)."""
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        base = Path("/sys/fs/cgroup") / rel.lstrip("/")
        usage = int((base / "memory.current").read_text())
        limit_raw = (base / "memory.max").read_text().strip()
    except (OSError, IndexError, ValueError):
        return {}
    out = {"cgroup_usage_gb": round(usage / 2**30, 2)}
    if limit_raw != "max":
        out["cgroup_limit_gb"] = round(int(limit_raw) / 2**30, 2)
    return out


class MemorySampler:
    """Background thread logging a memory snapshot every `interval` seconds."""

    def __init__(self, interval: float, cache_dir: Path | None = None) -> None:
        self.interval = interval
        self.cache_dir = cache_dir
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval <= 0:
            return
        self._thread = threading.Thread(target=self._run, name="memory-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        log = get_logger("dbx.memory")
        while not self._stop.wait(self.interval):
            log.info("memory_sample", **memory_snapshot(self.cache_dir))
