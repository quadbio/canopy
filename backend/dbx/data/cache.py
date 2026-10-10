"""Open datasets and loaded arrays, kept within the memory budget.

One LRU holds both: open sources (an open file costs nothing, a fully loaded legacy file costs
its estimated size) and arrays already served (embeddings, obs columns). Entries are evicted
when the total exceeds `memory.cache_gb`, when more than `MAX_OPEN` files are open, or after
`memory.idle_minutes` without use. Every open, load and eviction is logged with its size.
"""

from __future__ import annotations

import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import anndata
import zarr

from dbx.config import Config
from dbx.data.reader import (
    LazySource,
    MemorySource,
    NeedsConversionError,
    Source,
    estimate_memory_bytes,
)
from dbx.inspect.core import open_h5
from dbx.inspect.models import AnnDataInfo, Fingerprint
from dbx.logs import get_logger, timed

T = TypeVar("T")
MAX_OPEN = 64


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
            raise KeyError("table")
        return None, fp.anndata
    tables = fp.spatialdata.tables if fp.spatialdata else {}
    if not tables:
        raise NeedsConversionError("no_table")
    name = table or next(iter(tables))
    if name not in tables:
        raise KeyError("table")
    return name, tables[name]


def open_source(fp: Fingerprint, path: Path, table: str | None, small_file_bytes: int) -> Source:
    name, info = table_info(fp, table)
    if info.layout != "current":
        # Only anndata's whole-file reader understands pre-0.8 layouts.
        if fp.kind != "h5ad" or estimate_memory_bytes(info) > small_file_bytes:
            raise NeedsConversionError("legacy_layout")
        with warnings.catch_warnings():
            # One OldFormatWarning per element is expected here; the layout is in the log.
            warnings.simplefilter("ignore", anndata.OldFormatWarning)
            adata = anndata.read_h5ad(path)
        get_logger("dbx.cache").info("legacy_layout_loaded", layout=info.layout)
        return MemorySource(info, adata)
    if fp.kind == "h5ad":
        f = open_h5(path)
        return LazySource(info, f, f)
    root: Any = zarr.open_group(path, mode="r")
    return LazySource(info, root if name is None else root["tables"][name])


class DatasetCache:
    def __init__(self, cfg: Config) -> None:
        self.budget = int(cfg.memory.cache_gb * 2**30)
        self.small_file = int(cfg.memory.small_file_gb * 2**30)
        self.idle = cfg.memory.idle_minutes * 60
        self._lru: OrderedDict[tuple[Any, ...], _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.log = get_logger("dbx.cache")

    # ------------------------------------------------------------ public
    def has_source(self, version: str, table: str | None) -> bool:
        with self._lock:
            return ("source", version, table or "") in self._lru

    def source(self, version: str, fp: Fingerprint, path: Path, table: str | None) -> Source:
        key = ("source", version, table or "")
        hit = self._get(key)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        with timed("open_dataset", kind=fp.kind) as extra:
            src = open_source(fp, path, table, self.small_file)
            nbytes = estimate_memory_bytes(src.info) if src.mode == "memory" else 0
            extra.update(mode=src.mode, nbytes=nbytes)
        return self._put(key, src, nbytes, is_source=True)  # type: ignore[no-any-return]

    def array(self, key: tuple[Any, ...], load: Callable[[], T], nbytes: Callable[[T], int]) -> T:
        hit = self._get(("array", *key))
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        value = load()
        return self._put(("array", *key), value, nbytes(value), is_source=False)  # type: ignore[no-any-return]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._lru),
                "open": sum(e.is_source for e in self._lru.values()),
                "bytes": sum(e.nbytes for e in self._lru.values()),
            }

    def clear(self) -> None:
        with self._lock:
            items = list(self._lru.items())
            self._lru.clear()
        for key, e in items:
            self._close(key, e, "shutdown")

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

    def _close(self, key: tuple[Any, ...], e: _Entry, reason: str) -> None:
        if e.is_source:
            try:
                with e.value.lock:
                    e.value.close()
            except Exception as exc:  # closing must never fail a request
                self.log.warning("close_failed", error=type(exc).__name__)
        if key[-1] != "dup":
            self.log.info("cache_evict", what=key[0], reason=reason, nbytes=e.nbytes)
