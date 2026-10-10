"""Walk the configured data roots and keep the catalog index current.

- `os.scandir` only, no shell commands. `.h5ad` files and zarr directories are candidates;
  a zarr store is never descended into. Hidden entries and `catalog.ignore` patterns are skipped.
- Path safety: every directory and candidate is resolved with `realpath` and must lie inside
  its root's real path; symlinks leaving the root are skipped (and counted). Directories are
  visited once by (device, inode), so symlink loops end.
- Incremental: a candidate whose version stamp (size + mtime, or the store's newest group
  stamp) matches the index is not inspected again, unless the scan is `full`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbx.catalog.db import CatalogDB
from dbx.config import CatalogConfig, DataRoot
from dbx.inspect.core import ZARR_META, detect_kind, inspect_path, version_stamp
from dbx.inspect.models import Fingerprint
from dbx.logs import get_logger, timed

STORE_MARKERS = (*ZARR_META, ".zarray")
WRITE_BATCH = 200


def entry_id(root: str, relpath: str) -> str:
    return hashlib.sha256(f"{root}\0{relpath}".encode()).hexdigest()[:16]


def inside(real: str, root_real: str) -> bool:
    return real == root_real or real.startswith(root_real.rstrip(os.sep) + os.sep)


@dataclass
class Candidate:
    root: str
    relpath: str
    path: Path  # under the root's real path (not resolved through symlinks)
    is_file: bool
    stamp: str | None = None  # known from the walk for files


@dataclass
class WalkStats:
    dirs: int = 0
    unreadable: int = 0
    links_outside: int = 0
    ignored: int = 0
    too_deep: int = 0


def _ignored(name: str, relpath: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relpath, p) for p in patterns)


def _is_store(path: str) -> bool:
    return any(os.path.isfile(os.path.join(path, m)) for m in STORE_MARKERS)


def walk(root: DataRoot, cfg: CatalogConfig, stats: WalkStats) -> Iterator[Candidate]:
    """Yield the dataset candidates below a root. Raises OSError if the root is unreadable."""
    log = get_logger("dbx.catalog")
    root_real = os.path.realpath(root.path)
    st = os.stat(root_real)
    seen = {(st.st_dev, st.st_ino)}
    stack: list[tuple[str, str, int]] = [(root_real, "", 0)]
    first = True
    while stack:
        dirpath, rel, depth = stack.pop()
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name)
        except OSError as err:
            if first:
                raise
            stats.unreadable += 1
            log.warning("dir_unreadable", root=root.name, folder=rel, error=type(err).__name__)
            continue
        first = False
        stats.dirs += 1
        for e in entries:
            if e.name.startswith("."):
                continue
            relpath = f"{rel}/{e.name}" if rel else e.name
            if _ignored(e.name, relpath, cfg.ignore):
                stats.ignored += 1
                continue
            try:
                is_dir = e.is_dir()
                is_file = not is_dir and e.is_file()
                if e.is_symlink():
                    real = os.path.realpath(e.path)
                    if not inside(real, root_real):
                        stats.links_outside += 1
                        log.info("symlink_outside_root", root=root.name, path=relpath)
                        continue
            except OSError:
                stats.unreadable += 1
                continue
            if is_dir:
                if _is_store(e.path):
                    yield Candidate(root.name, relpath, Path(e.path), is_file=False)
                    continue
                if depth + 1 >= cfg.max_depth:
                    stats.too_deep += 1
                    continue
                try:
                    dst = e.stat()
                except OSError:
                    stats.unreadable += 1
                    continue
                key = (dst.st_dev, dst.st_ino)
                if key not in seen:
                    seen.add(key)
                    stack.append((e.path, relpath, depth + 1))
            elif is_file and e.name.lower().endswith(".h5ad"):
                try:
                    fst = e.stat()
                except OSError:
                    stats.unreadable += 1
                    continue
                stamp = f"{fst.st_size}|{fst.st_mtime_ns}"
                yield Candidate(root.name, relpath, Path(e.path), is_file=True, stamp=stamp)


def summarise(fp: Fingerprint) -> dict[str, Any]:
    """What a file listing shows without loading the fingerprint."""
    s: dict[str, Any] = {"n_warnings": len(fp.warnings), "n_errors": len(fp.errors)}
    ad = fp.anndata
    if fp.spatialdata is not None:
        sd = fp.spatialdata
        s.update(
            n_images=len(sd.images),
            n_labels=len(sd.labels),
            n_points=len(sd.points),
            n_shapes=len(sd.shapes),
            n_tables=len(sd.tables),
            coordinate_systems=len(sd.coordinate_systems),
        )
        ad = next(iter(sd.tables.values()), None)
    if ad is not None:
        s.update(
            n_obs=ad.n_obs,
            n_vars=ad.n_vars,
            x_encoding=ad.X.encoding if ad.X else None,
            n_obs_columns=len(ad.obs.columns),
            embeddings=sorted(ad.obsm),
        )
    s["warnings"] = sorted({w.code for w in fp.warnings})
    return s


def _reason(e: BaseException) -> tuple[str, str]:
    """(state, reason) for a failed inspection; the reason never contains names or values."""
    msg = str(e)
    if isinstance(e, ValueError) and msg == "not a supported dataset":
        return "unsupported", "not_supported"
    if isinstance(e, OSError) and msg.startswith("hdf5_lock"):
        return "failed", "hdf5_lock"
    if isinstance(e, PermissionError):
        return "failed", "permission_denied"
    if isinstance(e, FileNotFoundError):
        return "failed", "vanished"
    return "failed", f"error:{type(e).__name__}"


@dataclass
class ScanCounts:
    roots: int = 0
    roots_unavailable: int = 0
    found: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    unsupported: int = 0
    walk: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _check(
    c: Candidate, known: dict[str, tuple[str, str]], full: bool, cfg: CatalogConfig
) -> dict[str, Any] | None:
    """Inspect a candidate unless unchanged; returns the row to write, or None if unchanged."""
    log = get_logger("dbx.catalog")
    try:
        stamp = c.stamp or version_stamp(c.path)
    except OSError as e:
        stamp = f"error:{type(e).__name__}"
    prev = known.get(c.relpath)
    if prev is not None and prev[0] == stamp and not full:
        return None
    row: dict[str, Any] = {
        "id": entry_id(c.root, c.relpath),
        "root": c.root,
        "relpath": c.relpath,
        "folder": c.relpath.rpartition("/")[0],
        "name": c.path.name,
        "stamp": stamp,
        "_new": prev is None,
    }
    t0 = time.perf_counter()
    try:
        row["kind"] = detect_kind(c.path)
        fp = inspect_path(c.path, measure_size=cfg.measure_store_size)
    except Exception as e:
        state, reason = _reason(e)
        row.update(state=state, reason=reason, inspect_ms=_ms(t0))
        if state == "failed":
            log.warning("inspect_failed", root=c.root, path=c.relpath, reason=reason, exc_info=e)
        return row
    row.update(
        kind=fp.kind,
        version=fp.dataset_id,
        size_bytes=fp.size_bytes,
        mtime=fp.mtime,
        state="inspected",
        reason=None,
        summary=summarise(fp),
        fingerprint=fp.model_dump_json(),
        inspect_ms=_ms(t0),
    )
    log.info(
        "dataset_inspected",
        root=c.root,
        path=c.relpath,
        kind=fp.kind,
        size_bytes=fp.size_bytes,
        n_errors=len(fp.errors),
        ms=row["inspect_ms"],
    )
    return row


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def scan(
    db: CatalogDB,
    roots: list[DataRoot],
    cfg: CatalogConfig,
    *,
    full: bool = False,
    trigger: str = "manual",
) -> ScanCounts:
    """Scan every root, update the index and return the counts (also stored with the scan)."""
    log = get_logger("dbx.catalog")
    counts = ScanCounts()
    scan_id = db.start_scan(trigger, full)
    error = None
    try:
        with timed("catalog_scan", trigger=trigger, full=full) as extra:
            counts.removed += db.remove_roots_except(r.name for r in roots)
            with ThreadPoolExecutor(cfg.inspect_workers, thread_name_prefix="inspect") as pool:
                for root in roots:
                    _scan_root(db, root, cfg, pool, scan_id, full, counts)
            extra.update(counts.as_dict())
    except Exception as e:
        error = f"error:{type(e).__name__}"
        log.exception("catalog_scan_failed")
        raise
    finally:
        db.finish_scan(scan_id, counts.as_dict(), error)
    return counts


def _scan_root(
    db: CatalogDB,
    root: DataRoot,
    cfg: CatalogConfig,
    pool: ThreadPoolExecutor,
    scan_id: int,
    full: bool,
    counts: ScanCounts,
) -> None:
    log = get_logger("dbx.catalog")
    counts.roots += 1
    known = db.stamps(root.name)
    stats = WalkStats()
    with timed("root", root=root.name) as extra:
        try:
            candidates = list(walk(root, cfg, stats))
        except OSError as e:
            # An unavailable root (share not mounted) keeps its entries: never wipe the index
            # because of a transient outage.
            counts.roots_unavailable += 1
            log.warning("root_unavailable", root=root.name, error=type(e).__name__)
            db.touch(root.name, known, scan_id)
            return
        extra["n_candidates"] = len(candidates)
        unchanged: list[str] = []
        batch: list[dict[str, Any]] = []
        results = pool.map(lambda c: (c, _check(c, known, full, cfg)), candidates)
        for c, row in results:
            counts.found += 1
            if row is None:
                counts.unchanged += 1
                unchanged.append(c.relpath)
                continue
            if row.pop("_new"):
                counts.new += 1
            else:
                counts.changed += 1
            counts.failed += row["state"] == "failed"
            counts.unsupported += row["state"] == "unsupported"
            batch.append(row)
            if len(batch) >= WRITE_BATCH:
                db.upsert(batch, scan_id)
                batch = []
        db.upsert(batch, scan_id)
        db.touch(root.name, unchanged, scan_id)
        removed = db.remove_unseen(root.name, scan_id)
        counts.removed += removed
        for k, v in stats.__dict__.items():
            counts.walk[k] = counts.walk.get(k, 0) + v
        extra.update(stats.__dict__, removed=removed)
