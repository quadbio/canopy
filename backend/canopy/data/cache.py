"""Open datasets and loaded arrays, kept within the memory budget.

One LRU holds both: open sources (an open file costs nothing, a fully loaded legacy file costs
its estimated size) and arrays already served (embeddings, obs columns, genes, in-memory matrix
copies). Entries are evicted when the total exceeds `memory.cache_gb`, when more than
`MAX_OPEN` files are open, or after `memory.idle_minutes` without use. Every open, load and
eviction is logged with its size.

Loads are single-flight: concurrent requests for the same array wait for one load. A large load
first reserves its peak memory (`reserve`): older entries are evicted to make room, and the load
is refused (`MemoryBusyError`) when the cache plus all reservations would exceed
`memory.budget_gb`, or the host has less memory available than the load needs.
"""

from __future__ import annotations

import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import anndata
import zarr

from canopy.config import Config
from canopy.data.reader import (
    LazySource,
    MemorySource,
    NeedsConversionError,
    Source,
    UnknownElementError,
    estimate_memory_bytes,
)
from canopy.inspect.core import open_h5
from canopy.inspect.models import AnnDataInfo, Fingerprint
from canopy.logs import get_logger, memory_headroom, timed

T = TypeVar("T")
MAX_OPEN = 64


class MemoryBusyError(Exception):
    """A load would exceed the memory budget or the host's available memory; try again later."""


@dataclass
class _Entry:
    value: Any
    nbytes: int
    used: float
    is_source: bool


def table_info(fp: Fingerprint, table: str | None) -> tuple[str | None, AnnDataInfo]:
    """The AnnData description of a dataset's table (`table` names a SpatialData table)."""
    if fp.anndata is not None:
        if table not in (None, ""):
            raise UnknownElementError("table")
        return None, fp.anndata
    tables = fp.spatialdata.tables if fp.spatialdata else {}
    if not tables:
        raise NeedsConversionError("no_table")
    name = table or next(iter(tables))
    if name not in tables:
        raise UnknownElementError("table")
    return name, tables[name]


@dataclass
class StoreTable:
    path: Path  # the table group
    info: AnnDataInfo
    x_source: str  # which source matrix the store's X is ("X", "layers/<name>", "raw/X")
    built: str  # identifies this build of the store (for ETags)


def store_table(cache_dir: Path, fp: Fingerprint, table: str | None = None) -> StoreTable | None:
    """The converted store's table for this dataset version (`table`: a SpatialData table's
    source name, else the first), if it was converted and its contract major version is the one
    this server reads. Described once per store build (keyed by the manifest's modification
    time)."""
    from canopy.contract import MANIFEST
    from canopy.convert.anndata import store_dir

    manifest = store_dir(cache_dir, fp.dataset_id) / MANIFEST
    try:
        mtime = manifest.stat().st_mtime_ns
    except OSError:
        return None
    key = (str(manifest), mtime, table or "")
    # Requests arriving together for a new store (the page asks for meta, genes and embedding at
    # once) describe it once: the others wait for the first (host test: inspected twice, 1 s each).
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.setdefault(key, threading.Lock())
    with lock:
        return _store_table(*key)


_STORE_LOCKS: dict[tuple[str, int, str], threading.Lock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


@lru_cache(maxsize=256)
def _store_table(manifest: str, mtime: int, table: str) -> StoreTable | None:
    from canopy.contract import CONTRACT_MAJOR, read_manifest

    try:
        m = read_manifest(Path(manifest).parent)
    except (OSError, ValueError):
        get_logger("canopy.cache").warning("store_unreadable")
        return None
    if m.major != CONTRACT_MAJOR:
        get_logger("canopy.cache").warning(
            "store_incompatible", store_version=m.contract_version, reads=CONTRACT_MAJOR
        )
        return None
    from canopy.inspect import inspect_path

    if not m.tables:
        return None
    if table:
        # stores name tables by their safe names, keeping the source's in `source_name`
        t = next((e for n, e in m.tables.items() if (e.source_name or n) == table), None)
        if t is None:
            return None
    else:
        t = next(iter(m.tables.values()))
    path = Path(manifest).parent / t.path
    info = inspect_path(path).anndata
    return StoreTable(path, info, t.x_source, f"{m.converter.created}|{mtime}") if info else None


def open_source(
    fp: Fingerprint,
    path: Path,
    table: str | None,
    small_file_bytes: int,
    cache_dir: Path | None = None,
) -> Source:
    if cache_dir is not None and (st := store_table(cache_dir, fp, table)) is not None:
        if fp.anndata is not None and table not in (None, ""):
            raise UnknownElementError("table")
        src = LazySource(st.info, zarr.open_group(st.path, mode="r"))
        src.mode = "converted"
        return src
    name, info = table_info(fp, table)
    if info.layout != "current":
        # Only anndata's whole-file reader understands pre-0.8 layouts.
        if fp.kind != "h5ad" or estimate_memory_bytes(info) > small_file_bytes:
            raise NeedsConversionError("legacy_layout")
        with warnings.catch_warnings():
            # One OldFormatWarning per element is expected here; the layout is in the log.
            warnings.simplefilter("ignore", anndata.OldFormatWarning)
            adata = anndata.read_h5ad(path)
        get_logger("canopy.cache").info("legacy_layout_loaded", layout=info.layout)
        return MemorySource(info, adata)
    if fp.kind == "h5ad":
        f = open_h5(path)
        return LazySource(info, f, f)
    root: Any = zarr.open_group(path, mode="r")
    return LazySource(info, root if name is None else root["tables"][name])


class DatasetCache:
    def __init__(self, cfg: Config) -> None:
        self.budget = int(cfg.memory.cache_gb * 2**30)
        self.total_budget = int(cfg.memory.budget_gb * 2**30)
        # memory reserved by running conversion jobs (set by the app): one account for both
        self.jobs_reserved: Callable[[], int] = lambda: 0
        # requests refused for good per (version, table, kind, name): not read again
        self.refused: dict[tuple[str, ...], str] = {}
        self.small_file = int(cfg.memory.small_file_gb * 2**30)
        self.cache_dir = cfg.cache_dir
        self._inflight: dict[tuple[Any, ...], Future[Any]] = {}
        self._reserved = 0
        self.idle = cfg.memory.idle_minutes * 60
        self._lru: OrderedDict[tuple[Any, ...], _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.log = get_logger("canopy.cache")

    # ------------------------------------------------------------ public
    def drop_closed_source(self, version: str, table: str | None) -> None:
        """Forget sources found closed, so the next `source()` opens them again."""
        with self._lock:
            for which in ("", "original"):
                key = ("source", version, table or "", which)
                e = self._lru.get(key)
                if e is not None and getattr(e.value, "closed", False):
                    del self._lru[key]

    def drop_version(self, version: str) -> None:
        """Forget everything of a dataset version (e.g. after it was converted: the next request
        reads the store)."""
        with self._lock:
            items = [(k, e) for k, e in self._lru.items() if len(k) > 1 and k[1] == version]
            for k, _ in items:
                del self._lru[k]
            for r in [r for r in self.refused if r and r[0] == version]:
                del self.refused[r]
        for k, e in items:
            self._close(k, e, "converted")

    def has_source(self, version: str, table: str | None) -> bool:
        with self._lock:
            return ("source", version, table or "", "") in self._lru

    def source(
        self,
        version: str,
        fp: Fingerprint,
        path: Path,
        table: str | None,
        *,
        dataset: str = "",
        original: bool = False,
    ) -> Source:
        """The open source of a dataset version: its converted store when there is one, else
        (or with `original`) the file itself."""
        key = ("source", version, table or "", "original" if original else "")
        hit = self._get(key)
        if hit is not None:
            if original:
                return hit  # type: ignore[no-any-return]
            st = store_table(self.cache_dir, fp, table)
            # the same store build (compared by its key: the description may have been
            # re-read after leaving the lookup cache, which must not close the open dataset)
            if (hit.mode == "converted") == (st is not None) and (
                st is None or getattr(hit, "store_build", None) == st.built
            ):
                return hit  # type: ignore[no-any-return]
            # converted, re-converted or deleted since it was opened: open it again
            self.drop_version(version)
        ad = fp.anndata
        if ad is None and fp.spatialdata is not None:
            ad = next(iter(fp.spatialdata.tables.values()), None)
        with timed(
            "open_dataset",
            kind=fp.kind,
            dataset_id=dataset,
            version=fp.dataset_id,
            n_obs=ad.n_obs if ad else None,
            x_encoding=ad.X.encoding if ad and ad.X else None,
            layout=ad.layout if ad else None,
        ) as extra:
            src = open_source(
                fp, path, table, self.small_file, None if original else self.cache_dir
            )
            if src.mode == "converted":
                st_now = store_table(self.cache_dir, fp, table)
                src.store_build = st_now.built if st_now else None
            nbytes = estimate_memory_bytes(src.info) if src.mode == "memory" else 0
            extra.update(mode=src.mode, nbytes=nbytes)
        return self._put(key, src, nbytes, is_source=True)  # type: ignore[no-any-return]

    def array(
        self,
        key: tuple[Any, ...],
        load: Callable[[], T],
        nbytes: Callable[[T], int],
        *,
        reserve: int = 0,
        outcome: dict[str, Any] | None = None,
        **log_fields: Any,
    ) -> T:
        """A cached array; on a miss `load()` runs as a timed `load_array` stage, once even when
        several requests miss at the same time. `reserve`: peak bytes the load needs (see the
        module docstring). `outcome["cache"]` is set to hit, miss or waited (for logging)."""
        k = ("array", *key)
        out = outcome if outcome is not None else {}
        hit = self._get(k)
        if hit is not None:
            out["cache"] = "hit"
            return hit  # type: ignore[no-any-return]
        with self._lock:
            fut = self._inflight.get(k)
            owner = fut is None
            if owner:
                fut = self._inflight[k] = Future()
        assert fut is not None
        if not owner:
            out["cache"] = "waited"
            return fut.result()  # type: ignore[no-any-return]  # re-raises the loader's error
        out["cache"] = "miss"
        try:
            with self.reserve(reserve, **log_fields), timed("load_array", **log_fields) as extra:
                value = load()
                extra["nbytes"] = nbytes(value)
                if reserve:
                    extra["reserved"] = reserve
            value = self._put(k, value, extra["nbytes"], is_source=False)
            fut.set_result(value)
            return value
        except BaseException as e:
            fut.set_exception(e)
            raise
        finally:
            with self._lock:
                self._inflight.pop(k, None)

    @contextmanager
    def reserve(self, nbytes: int, **log_fields: Any) -> Iterator[None]:
        """Hold `nbytes` of the memory budget while a large load runs (no-op for 0)."""
        if nbytes <= 0:
            yield
            return
        evicted: list[tuple[tuple[Any, ...], _Entry]] = []
        with self._lock:
            cached = sum(e.nbytes for e in self._lru.values())
            # make room in the cache first (the result is cached there afterwards)
            for key in list(self._lru):
                if cached + self._reserved + nbytes <= self.budget:
                    break
                if self._lru[key].nbytes == 0:
                    continue  # open files cost nothing: closing them frees no memory
                e = self._lru.pop(key)
                cached -= e.nbytes
                evicted.append((key, e))
            available = memory_headroom()  # the host's, or less under a cgroup limit
            jobs = self.jobs_reserved()
            busy = cached + self._reserved + jobs + nbytes > self.total_budget or nbytes > available
            if not busy:
                self._reserved += nbytes
        for key, e in evicted:
            self._close(key, e, "reserve")
        if busy:
            self.log.warning(
                "memory_busy",
                need=nbytes,
                cache_bytes=cached,
                reserved=self._reserved,
                jobs_bytes=jobs,
                budget=self.total_budget,
                available=available,
                **log_fields,
            )
            raise MemoryBusyError
        try:
            yield
        finally:
            with self._lock:
                self._reserved -= nbytes

    def cached_bytes(self) -> int:
        """Bytes held by cached arrays and loads in progress (for the jobs' admission)."""
        with self._lock:
            return sum(e.nbytes for e in self._lru.values()) + self._reserved

    def sweep(self) -> None:
        """Drop entries idle for longer than `memory.idle_minutes` (also run by a timer)."""
        self._sweep()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._lru),
                "open": sum(e.is_source for e in self._lru.values()),
                "bytes": sum(e.nbytes for e in self._lru.values()),
                "reserved": self._reserved,
                "loading": len(self._inflight),
            }

    def clear(self) -> None:
        with self._lock:
            items = list(self._lru.items())
            self._lru.clear()
        for key, e in items:
            self._close(key, e, "shutdown", wait=True)

    # ------------------------------------------------------------ internals
    def _get(self, key: tuple[Any, ...]) -> Any:
        self._sweep()
        with self._lock:
            e = self._lru.get(key)
            if e is None:
                return None
            e.used = time.monotonic()
            self._lru.move_to_end(key)
            return e.value

    def _put(self, key: tuple[Any, ...], value: Any, nbytes: int, *, is_source: bool) -> Any:
        evicted: list[tuple[tuple[Any, ...], _Entry]] = []
        with self._lock:
            old = self._lru.get(key)
            if old is not None:  # loaded concurrently by another request: keep the first
                if is_source and old.value is not value:
                    evicted.append((key + ("dup",), _Entry(value, 0, 0, True)))
                value = old.value
            else:
                self._lru[key] = _Entry(value, nbytes, time.monotonic(), is_source)
            evicted += self._over_budget(keep=key)
        for k, e in evicted:
            self._close(k, e, "lru")
        if old is None:
            self.log.info("cache_load", what=key[0], nbytes=nbytes, **self._totals())
        return value

    def _over_budget(self, keep: tuple[Any, ...]) -> list[tuple[tuple[Any, ...], _Entry]]:
        out = []
        total = sum(e.nbytes for e in self._lru.values())
        n_open = sum(e.is_source for e in self._lru.values())
        for k in list(self._lru):
            if total <= self.budget and n_open <= MAX_OPEN:
                break
            if k == keep:
                continue
            e = self._lru.pop(k)
            total -= e.nbytes
            n_open -= e.is_source
            out.append((k, e))
        return out

    def _sweep(self) -> None:
        if self.idle <= 0:
            return
        cutoff = time.monotonic() - self.idle
        with self._lock:
            stale = [(k, e) for k, e in self._lru.items() if e.used < cutoff]
            for k, _ in stale:
                del self._lru[k]
        for k, e in stale:
            self._close(k, e, "idle")

    def _totals(self) -> dict[str, int]:
        with self._lock:
            return {
                "cache_bytes": sum(e.nbytes for e in self._lru.values()),
                "cache_entries": len(self._lru),
            }

    def _close(self, key: tuple[Any, ...], e: _Entry, reason: str, *, wait: bool = False) -> None:
        if e.is_source:
            # Closing waits for a read in progress on that source; never make the request that
            # caused the eviction wait for it.
            if wait:
                self._close_source(e.value)
            else:
                threading.Thread(target=self._close_source, args=(e.value,), daemon=True).start()
        if key[-1] != "dup":
            self.log.info("cache_evict", what=key[0], reason=reason, nbytes=e.nbytes)

    def _close_source(self, src: Source) -> None:
        try:
            with src.lock:
                src.close()
        except Exception as exc:  # closing must never fail anything
            self.log.warning("close_failed", error=type(exc).__name__)
