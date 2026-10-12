"""Structured logging: JSON lines to one rotating file per process, plus timing and memory
helpers.

Every log event is one JSON object per line and carries `pid`, `role` (server, cli, worker) and
`build`, plus the request or job id bound in the current context, so a request's or job's stages
can be reassembled from the files of several processes.

Privacy (config `logging.privacy`, see `dbx.privacy`): names are logged in dedicated fields
(`NAME_FIELDS`), which `strict` replaces by salted tokens. Exceptions are logged as type, code
locations and a message redacted to the level; Python warnings are captured the same way.
"""

from __future__ import annotations

import contextvars
import faulthandler
import logging
import logging.handlers
import os
import re
import resource
import secrets
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psutil
import structlog

from dbx.build import build_id
from dbx.config import LoggingConfig
from dbx.privacy import code_location, redact_text, token

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

LOG_PREFIX = "dbx-"
# Fields whose values are user-chosen names (paths, files, roots, elements, columns, genes):
# tokenised under `strict`, nested values included.
NAME_FIELDS = frozenset(
    {
        "path",
        "paths",
        "name",
        "root",
        "folder",
        "file",
        "element",
        "column",
        "table",
        "gene",
        "gene_index",  # with the public gene list, an index names a gene
        "layer",
        "x_source",  # "layers/<name>"
        "details",  # validator messages: they name columns and elements
        "config_file",
        "log_dir",
        "cache_dir",
        "dir",
        "data_roots",
        "target",
        "ignore",
    }
)
_MAX_FRAMES = 30
_MAX_CHAIN = 3
_WARNING = re.compile(r"^(?P<file>.*?):(?P<line>\d+): (?P<category>\w+): (?P<message>.*)", re.S)

_log_file: Path | None = None
_rollover_hook: Callable[[], None] | None = None
_stack_file: Any = None


class _RotatingFile(logging.handlers.RotatingFileHandler):
    """Rotating file that calls the rollover hook after each rotation (the server logs its
    environment again, so every file is self-contained)."""

    _in_hook = False

    def doRollover(self) -> None:  # noqa: N802 - logging API
        super().doRollover()
        if _rollover_hook is not None and not _RotatingFile._in_hook:
            _RotatingFile._in_hook = True
            try:
                _rollover_hook()
            finally:
                _RotatingFile._in_hook = False


def set_rollover_hook(hook: Callable[[], None] | None) -> None:
    global _rollover_hook
    _rollover_hook = hook


def install_stack_dumps(log_dir: Path | None) -> Path | None:
    """`kill -USR1 <pid>` writes the stack of every thread to `stacks-<pid>.txt` in the log
    folder (code locations only, no data), and a hard crash writes its stack there too. This is
    how a hung server on the host is diagnosed from files alone."""
    global _stack_file
    if log_dir is None or not hasattr(signal, "SIGUSR1"):
        return None
    path = log_dir / f"stacks-{os.getpid()}.txt"
    _stack_file = path.open("a")  # kept open: faulthandler writes to the descriptor
    faulthandler.enable(file=_stack_file, all_threads=True)
    faulthandler.register(signal.SIGUSR1, file=_stack_file, all_threads=True, chain=False)
    return path


def default_log_dir() -> Path:
    """Per-user fallback log folder (never the current directory)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "dbx" / "logs"


def current_log_file() -> Path | None:
    """The JSON-lines file this process writes to (None when file logging is off)."""
    return _log_file


def log_file_name(role: str, pid: int | None = None) -> str:
    return f"{LOG_PREFIX}{role}-{pid if pid is not None else os.getpid()}.jsonl"


def _writable_dir(*candidates: Path) -> Path | None:
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(d, os.W_OK):
            return d
    return None


def prune_logs(log_dir: Path, retention_days: float) -> int:
    """Delete dbx log files (all processes) not modified for `retention_days`; returns count."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in log_dir.glob(f"{LOG_PREFIX}*.jsonl*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------- privacy processors


def _tokenise(value: Any, salt: str) -> Any:
    if isinstance(value, str):
        return token(value, salt)
    if isinstance(value, list | tuple):
        return [_tokenise(v, salt) for v in value]
    if isinstance(value, dict):
        return {k: _tokenise(v, salt) for k, v in value.items()}
    return value


def _names(value: Any, salt: str) -> Any:
    if isinstance(value, dict):
        return {
            k: _tokenise(v, salt) if k in NAME_FIELDS else _names(v, salt) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_names(v, salt) for v in value]
    return value


_ERRNO_TEXT = re.compile(r"errno\s*=\s*(\d{1,4})")


def _errno(exc: BaseException) -> int | None:
    """The OS error number of an exception: its `errno`, or the one HDF5 puts in its message."""
    n = getattr(exc, "errno", None)
    if isinstance(n, int):
        return n
    m = _ERRNO_TEXT.search(str(exc))
    return int(m.group(1)) if m else None


def _exception_fields(exc: BaseException, privacy: str, salt: str) -> dict[str, Any]:
    out: dict[str, Any] = {"exc_type": type(exc).__qualname__}
    if (n := _errno(exc)) is not None:
        out["exc_errno"] = n  # a number: kept at every level (EIO, ESTALE, … on network storage)
    # str(KeyError('x')) is "'x'" (quoted), which would be tokenised whole: use the key itself.
    text = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    msg = redact_text(text, privacy, salt)
    if msg:
        out["exc_message"] = msg
    frames = traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]
    out["exc_frames"] = [f"{code_location(f.filename)}:{f.lineno}:{f.name}" for f in frames]
    chain: list[dict[str, Any]] = []
    cur = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    while cur is not None and len(chain) < _MAX_CHAIN:
        link: dict[str, Any] = {"exc_type": type(cur).__qualname__}
        if (n := _errno(cur)) is not None:
            link["exc_errno"] = n
        m = redact_text(str(cur), privacy, salt)
        if m:
            link["exc_message"] = m
        chain.append(link)
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    if chain:
        out["exc_chain"] = chain
    if privacy == "debug":
        out["exception"] = "".join(traceback.format_exception(exc))
    return out


def _privacy_processor(privacy: str, salt: str) -> Any:
    def process(_: Any, __: str, ed: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        exc_info = ed.pop("exc_info", None)
        if exc_info:
            if exc_info is True:
                exc_info = sys.exc_info()
            exc = exc_info if isinstance(exc_info, BaseException) else exc_info[1]
            if exc is not None:
                ed.update(_exception_fields(exc, privacy, salt))
        if ed.get("logger") == "py.warnings":
            m = _WARNING.match(str(ed.get("event", "")))
            ed["event"] = "python_warning"
            if m:
                ed["category"] = m["category"]
                ed["location"] = f"{code_location(m['file'])}:{m['line']}"
                msg = redact_text(m["message"].split("\n")[0], privacy, salt)
                if msg:
                    ed["message"] = msg
        elif ed.get("_from_structlog") is False and privacy != "debug":
            # Third-party log lines: free text, redacted like messages. uvicorn's come from fixed
            # templates (pid, host, port), so strict treats them like standard.
            level = "standard" if str(ed.get("logger", "")).startswith("uvicorn") else privacy
            ed["event"] = redact_text(str(ed.get("event", "")), level, salt) or "<redacted>"
        if privacy == "strict":
            for k in list(ed):
                if k in NAME_FIELDS:
                    ed[k] = _tokenise(ed[k], salt)
                elif isinstance(ed[k], dict | list):
                    ed[k] = _names(ed[k], salt)
        return ed

    return process


def setup_logging(
    cfg: LoggingConfig, *, role: str = "cli", salt: str = "", to_console: bool = True
) -> Path | None:
    """Configure structlog to write JSON lines to this process's rotating file (and a readable
    console). `salt` is the installation secret used for strict-mode tokens (a random one is
    used when empty, so tokens are then not comparable across processes).

    Returns the log folder actually used: the configured one, else the per-user fallback, else
    None (file logging off) when neither is writable, e.g. on a read-only share.
    """
    global _log_file
    log_dir = _writable_dir(cfg.dir, default_log_dir())
    level = _LEVELS[cfg.level]
    salt = salt or secrets.token_hex(16)
    static = {"pid": os.getpid(), "role": role, "build": build_id()}

    def add_static(_: Any, __: str, ed: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        for k, v in static.items():
            ed.setdefault(k, v)
        return ed

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_static,
        _privacy_processor(cfg.privacy, salt),
    ]
    handlers: list[logging.Handler] = []
    _log_file = None
    pruned = 0
    if log_dir is not None:
        pruned = prune_logs(log_dir, cfg.retention_days)
        _log_file = log_dir / log_file_name(role)
        file_handler = _RotatingFile(
            _log_file, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count
        )
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(file_handler)
    if to_console:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=False),
                ],
                foreign_pre_chain=shared,
            )
        )
        handlers.append(console)

    root = logging.getLogger()
    for h in root.handlers:
        h.close()
    root.handlers = handlers
    root.setLevel(level)
    # uvicorn's own loggers go through the same handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.captureWarnings(True)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )
    if pruned:
        get_logger("dbx.logs").info("logs_pruned", n_files=pruned)
    return log_dir


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


_PROC: dict[int, psutil.Process] = {}


def rss_mb() -> float:
    pid = os.getpid()  # a forked child must not measure its parent
    proc = _PROC.get(pid) or _PROC.setdefault(pid, psutil.Process(pid))
    return float(proc.memory_info().rss) / 2**20


def peak_rss_process_mb() -> float:
    """Highest RSS of this process since it started (ru_maxrss: bytes on macOS, KiB on Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(peak) / (2**20 if sys.platform == "darwin" else 2**10)


class _PeakTracker:
    """One daemon thread sampling RSS while any `timed` stage is open, so each stage can report
    its own peak (RSS is per process: concurrent stages see each other's memory)."""

    INTERVAL = 0.05

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[int, list[float]] = {}
        self._thread: threading.Thread | None = None

    def open(self) -> list[float]:
        holder = [rss_mb()]
        with self._lock:
            self._open[id(holder)] = holder
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="rss-peak", daemon=True)
                self._thread.start()
        return holder

    def close(self, holder: list[float]) -> float:
        with self._lock:
            self._open.pop(id(holder), None)
        return max(holder[0], rss_mb())

    def _run(self) -> None:
        while True:
            time.sleep(self.INTERVAL)
            with self._lock:
                if not self._open:
                    continue
                r = rss_mb()
                for h in self._open.values():
                    if r > h[0]:
                        h[0] = r


_PEAK = _PeakTracker()
_STAGES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("dbx_stages", default=())


@contextmanager
def timed(stage: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log the duration and memory of a block as a `stage` event.

    Stages nest: inside `timed("convert")`, `timed("x_csc")` is logged as `convert/x_csc`.
    Logged: `ms`, `rss_mb` (at exit), `rss_delta_mb`, `rss_peak_mb` (sampled every 50 ms while
    the stage is open) and `rss_peak_process_mb`. Yields a dict the block can add fields to
    (e.g. bytes read); they are logged on exit. Failures are logged and re-raised.
    """
    log = get_logger("dbx.timing")
    path = (*_STAGES.get(), stage)
    reset = _STAGES.set(path)
    name = "/".join(path)
    extra: dict[str, Any] = {}
    t0 = time.perf_counter()
    m0 = rss_mb()
    holder = _PEAK.open()
    try:
        yield extra
    except Exception:
        peak = _PEAK.close(holder)
        log.exception(
            "stage_failed", stage=name, ms=_ms(t0), rss_peak_mb=round(peak, 1), **fields, **extra
        )
        raise
    finally:
        _STAGES.reset(reset)
    peak = _PEAK.close(holder)
    m1 = rss_mb()
    log.info(
        "stage",
        stage=name,
        ms=_ms(t0),
        rss_mb=round(m1, 1),
        rss_delta_mb=round(m1 - m0, 1),
        rss_peak_mb=round(peak, 1),
        rss_peak_process_mb=round(peak_rss_process_mb(), 1),
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


def memory_headroom() -> int:
    """Bytes this process could still use: the host's available memory, or less if the cgroup
    (e.g. a systemd slice with MemoryMax) leaves less."""
    avail = int(psutil.virtual_memory().available)
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        base = Path("/sys/fs/cgroup") / rel.lstrip("/")
        usage = int((base / "memory.current").read_text())
        limit_raw = (base / "memory.max").read_text().strip()
        if limit_raw != "max":
            avail = min(avail, int(limit_raw) - usage)
    except (OSError, IndexError, ValueError):
        pass
    return max(avail, 0)


def memory_total() -> int:
    """Bytes of memory this process could ever have: the host's, or its cgroup limit."""
    total = int(psutil.virtual_memory().total)
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        limit_raw = (Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.max").read_text().strip()
        if limit_raw != "max":
            total = min(total, int(limit_raw))
    except (OSError, IndexError, ValueError):
        pass
    return total


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
