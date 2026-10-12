"""The catalog as the server uses it: background scans (start-up, timer, on demand) and the
only way from a dataset id back to a file."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from canopy.catalog.crawl import inside, scan
from canopy.catalog.db import DB_FILE, CatalogDB, Row
from canopy.config import Config
from canopy.environment import LOCAL_FS, filesystem
from canopy.logs import get_logger


class PathEscapeError(LookupError):
    """A dataset's path no longer resolves inside its data root (e.g. a symlink was changed)."""


def use_wal(cfg: Config) -> bool:
    """SQLite write-ahead log for the cache's databases: on known local file systems only, unless
    `catalog.sqlite_wal` says otherwise."""
    wal = cfg.catalog.sqlite_wal
    if wal is None:
        wal = filesystem(cfg.state).get("fstype") in LOCAL_FS
    return bool(wal)


class Catalog:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.roots = {r.name: r for r in cfg.data_roots}
        self.wal = use_wal(cfg)
        self.db = CatalogDB(cfg.state / DB_FILE, wal=self.wal)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._full = False
        self._thread: threading.Thread | None = None
        self.log = get_logger("canopy.catalog")

    # ------------------------------------------------------------ scanning
    @property
    def scanning(self) -> bool:
        return self._lock.locked()

    def scan_now(self, *, full: bool = False, trigger: str = "manual") -> bool:
        """Scan in the calling thread; False if another scan is running."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            scan(self.db, list(self.roots.values()), self.cfg.catalog, full=full, trigger=trigger)
        except Exception as e:  # logged in full by scan(); the scheduler must survive
            self.log.warning("catalog_scan_aborted", error=type(e).__name__)
        finally:
            self._lock.release()
        return True

    def start(self) -> None:
        """Scan at start-up, then every `rescan_minutes`, and whenever `refresh()` is called."""
        self._thread = threading.Thread(target=self._run, name="catalog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def refresh(self, *, full: bool = False) -> bool:
        """Ask the background thread for a scan now; False if one is already running."""
        if self.scanning:
            return False
        self._full = self._full or full
        self._wake.set()
        return True

    def _run(self) -> None:
        trigger = "startup"
        interval = self.cfg.catalog.rescan_minutes * 60 or None
        while not self._stop.is_set():
            full, self._full = self._full, False
            self.scan_now(full=full, trigger=trigger)
            woke = self._wake.wait(interval)
            self._wake.clear()
            trigger = "refresh" if woke else "timer"

    # ------------------------------------------------------------ lookup
    def get(self, id: str) -> Row | None:
        row = self.db.get(id)
        return row if row is not None and row.root in self.roots else None

    def resolve(self, row: Row) -> Path:
        """The real path of an indexed dataset, re-checked against its root now (symlinks may
        have changed since the scan). Raises PathEscapeError or FileNotFoundError."""
        root = self.roots.get(row.root)
        if root is None:
            raise PathEscapeError("root no longer configured")
        root_real = os.path.realpath(root.path)
        real = os.path.realpath(os.path.join(root_real, row.relpath))
        if not inside(real, root_real):
            self.log.warning("path_escape", root=row.root, path=row.relpath)
            raise PathEscapeError("dataset path leaves its data root")
        if not os.path.exists(real):
            raise FileNotFoundError("dataset vanished")
        return Path(real)
