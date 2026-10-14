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

import contextlib
import fnmatch
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from canopy.catalog.db import CatalogDB
from canopy.config import CatalogConfig, DataRoot
from canopy.inspect.core import ZARR_META, dataset_id, detect_kind, inspect_path, version_stamp
from canopy.inspect.models import Fingerprint
from canopy.logs import get_logger, timed

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


def walk(
    root: DataRoot, cfg: CatalogConfig, stats: WalkStats, unreadable: list[str] | None = None
) -> Iterator[Candidate]:
    """Yield the dataset candidates below a root. Raises OSError if the root is unreadable.

    A folder is recognised as a zarr store from its own listing (`zarr.json`, `.zgroup` or
    `.zarray` present): no extra file checks per folder, which matters on network storage.
    Folders that cannot be listed are appended to `unreadable` (root-relative paths)."""
    log = get_logger("canopy.catalog")
    root_real = os.path.realpath(root.path)
    os.stat(root_real)  # raises if the root itself is unavailable
    followed: set[str] = set()  # real paths of symlinked folders already walked
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
            if unreadable is not None:
                unreadable.append(rel)
            log.warning("dir_unreadable", root=root.name, folder=rel, error=type(err).__name__)
            continue
        first = False
        stats.dirs += 1
        if rel and any(e.name in STORE_MARKERS for e in entries):
            yield Candidate(root.name, rel, Path(dirpath), is_file=False)
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            relpath = f"{rel}/{e.name}" if rel else e.name
            if _ignored(e.name, relpath, cfg.ignore):
                stats.ignored += 1
                continue
            try:
                is_link = e.is_symlink()
                is_dir = e.is_dir()
                is_file = not is_dir and e.is_file()
                real = os.path.realpath(e.path) if is_link else ""
            except OSError:
                stats.unreadable += 1
                continue
            if is_link and not inside(real, root_real):
                stats.links_outside += 1
                log.info("symlink_outside_root", root=root.name, path=relpath)
                continue
            if is_dir:
                if depth + 1 > cfg.max_depth:
                    stats.too_deep += 1
                    continue
                if is_link:
                    # A link to a folder being walked (an ancestor) or already walked would
                    # loop or index the same data twice.
                    here = os.path.realpath(dirpath)
                    if real in followed or inside(here, real):
                        continue
                    followed.add(real)
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
    if isinstance(e, ValueError) and msg == "not_anndata":
        return "unsupported", "not_anndata"
    if isinstance(e, ValueError) and msg == "links_outside":
        # it points at data outside itself (HDF5 external storage or links, symlinks out of a
        # zarr store): never read (review full-2 A-H1)
        return "unsupported", "links_outside"
    if isinstance(e, OSError) and msg.startswith("hdf5_lock"):
        return "failed", "hdf5_lock"
    if isinstance(e, OSError) and msg.startswith("hdf5_truncated"):
        return "failed", "truncated"
    if isinstance(e, PermissionError):
        return "failed", "permission_denied"
    if isinstance(e, FileNotFoundError):
        return "failed", "vanished"
    return "failed", f"error:{type(e).__name__}"


class Progress:
    """Live state of a scan, logged as `scan_progress` every `HEARTBEAT_S` seconds, so a scan
    stuck on a stalled network read shows what it is waiting for."""

    HEARTBEAT_S = 30.0

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.root = ""
        self.dirs = 0
        self.candidates = 0
        self.checked = 0
        self.active: dict[int, tuple[float, str, str]] = {}  # thread -> (start, root, relpath)

    def begin(self, c: Candidate) -> None:
        with self.lock:
            self.active[threading.get_ident()] = (time.monotonic(), c.root, c.relpath)

    def end(self) -> None:
        with self.lock:
            self.active.pop(threading.get_ident(), None)
            self.checked += 1

    def report(self) -> None:
        now = time.monotonic()
        with self.lock:
            oldest = min(self.active.values(), default=None)
            fields: dict[str, Any] = {
                "root": self.root,
                "dirs": self.dirs,
                "candidates": self.candidates,
                "checked": self.checked,
                "inspecting": len(self.active),
            }
        if oldest is not None:
            fields.update(oldest_s=round(now - oldest[0], 1), oldest_path=oldest[2])
        get_logger("canopy.catalog").info("scan_progress", **fields)


@contextmanager
def heartbeat(progress: Progress) -> Iterator[None]:
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(Progress.HEARTBEAT_S):
            progress.report()

    t = threading.Thread(target=run, name="scan-heartbeat", daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()


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
    adopted: int = 0  # converted copies carried over to a dataset's new id
    walk: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _check(
    c: Candidate,
    known: dict[str, tuple[str, str, str | None]],
    full: bool,
    cfg: CatalogConfig,
    progress: Progress | None = None,
) -> dict[str, Any] | None:
    """Inspect a candidate unless unchanged; returns the row to write, or None if unchanged."""
    if progress is None:
        return _check_one(c, known, full, cfg)
    progress.begin(c)
    try:
        return _check_one(c, known, full, cfg)
    finally:
        progress.end()


def _check_one(
    c: Candidate, known: dict[str, tuple[str, str, str | None]], full: bool, cfg: CatalogConfig
) -> dict[str, Any] | None:
    log = get_logger("canopy.catalog")
    try:
        stamp = c.stamp or version_stamp(c.path)
    except OSError as e:
        stamp = f"error:{type(e).__name__}"
    prev = known.get(c.relpath)
    identity = f"{c.root}/{c.relpath}"
    # unchanged: the same stamp, and the same version id. The id holds the root's name and the
    # path in it (owner, host test 3: moving the whole data folder keeps the conversions); an
    # index written by an earlier release (real paths) is re-inspected once, its converted
    # copies carried over (`adopt_store`).
    if (
        prev is not None
        and prev[0] == stamp
        and not full
        and (prev[2] is None or prev[2] == dataset_id(c.path, stamp, identity))
    ):
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
        fp = inspect_path(c.path, measure_size=cfg.measure_store_size, identity=identity)
    except Exception as e:
        state, reason = _reason(e)
        row.update(state=state, reason=reason, inspect_ms=_ms(t0))
        if state == "failed":
            log.warning("inspect_failed", root=c.root, path=c.relpath, reason=reason, exc_info=e)
        return row
    if prev is not None and prev[0] == stamp and prev[2] and prev[2] != fp.dataset_id:
        row["_previous_version"] = prev[2]  # the same content under its former id
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
    summary = row["summary"]
    log.info(
        "dataset_inspected",
        # links requests (dataset_id) to the content version and the file (tokens at strict)
        dataset_id=entry_id(c.root, c.relpath),
        version=fp.dataset_id,
        root=c.root,
        path=c.relpath,
        kind=fp.kind,
        size_bytes=fp.size_bytes,
        n_obs=summary.get("n_obs"),
        n_vars=summary.get("n_vars"),
        x_encoding=summary.get("x_encoding"),
        n_obs_columns=summary.get("n_obs_columns"),
        n_embeddings=len(summary.get("embeddings") or []),
        n_errors=len(fp.errors),
        n_warnings=len(fp.warnings),
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
    cache_dir: Path | None = None,
    state_dir: Path | None = None,
) -> ScanCounts:
    """Scan every root, update the index and return the counts (also stored with the scan).
    With `cache_dir`, a dataset whose id changed while its content did not (an index from an
    earlier release) keeps its converted copy."""
    log = get_logger("canopy.catalog")
    counts = ScanCounts()
    scan_id = db.start_scan(trigger, full)
    error = None
    progress = Progress()
    t0 = time.perf_counter()
    try:
        with timed("catalog_scan", trigger=trigger, full=full) as extra, heartbeat(progress):
            counts.removed += db.remove_roots_except(r.name for r in roots)
            with ThreadPoolExecutor(cfg.inspect_workers, thread_name_prefix="inspect") as pool:
                for root in roots:
                    _scan_root(
                        db, root, cfg, pool, scan_id, full, counts, progress, cache_dir, state_dir
                    )
            extra.update(counts.as_dict())
        elapsed = time.perf_counter() - t0
        if cfg.rescan_minutes and elapsed > cfg.rescan_minutes * 30:
            # Longer than half the rescan interval: the server would be scanning most of the time.
            log.warning(
                "scan_slow_for_interval", s=round(elapsed, 1), rescan_minutes=cfg.rescan_minutes
            )
    except Exception as e:
        error = f"error:{type(e).__name__}"
        log.exception("catalog_scan_failed")
        raise
    finally:
        db.finish_scan(scan_id, counts.as_dict(), error)
    return counts


def adopt_store(
    cache_dir: Path,
    old: str,
    new: str,
    state_dir: Path | None = None,
    fingerprint: str | None = None,
) -> bool:
    """Give the converted copy of version `old` to version `new` (the same content under a new
    id): its folder is renamed and its manifest updated. Not while a conversion writes into it,
    nor over an existing copy."""
    from canopy.contract import MANIFEST

    src, dst = cache_dir / old, cache_dir / new
    if not src.is_dir() or dst.exists() or any(src.glob("c*.tmp-*")):
        return False
    try:
        src.rename(dst)
    except OSError:
        return False
    if state_dir is not None:
        # a conversion queued for the old version (e.g. requeued across the upgrade) publishes
        # under the new one (review full-2 A-L6)
        import sqlite3

        jobs = state_dir / "jobs.sqlite"
        if jobs.exists():
            with contextlib.suppress(sqlite3.Error), sqlite3.connect(jobs, timeout=30) as con:
                # the job's own fingerprint too: the converter publishes under its version
                con.execute(
                    "UPDATE jobs SET version=?, fingerprint=COALESCE(?, fingerprint) "
                    "WHERE version=? AND state='queued'",
                    (new, fingerprint, old),
                )
    for manifest in dst.glob(f"c*/{MANIFEST}"):
        with contextlib.suppress(OSError, ValueError):
            m = json.loads(manifest.read_text())
            m["dataset_id"] = new
            part = manifest.with_name(manifest.name + ".part")
            part.write_text(json.dumps(m, indent=2))
            part.replace(manifest)  # the manifest marks a complete store: never half-written
    get_logger("canopy.catalog").info("store_adopted", version=new, previous=old)
    return True


def _scan_root(
    db: CatalogDB,
    root: DataRoot,
    cfg: CatalogConfig,
    pool: ThreadPoolExecutor,
    scan_id: int,
    full: bool,
    counts: ScanCounts,
    progress: Progress | None = None,
    cache_dir: Path | None = None,
    state_dir: Path | None = None,
) -> None:
    log = get_logger("canopy.catalog")
    counts.roots += 1
    known = db.stamps(root.name)
    stats = WalkStats()
    unreadable: list[str] = []
    progress = progress or Progress()
    with progress.lock:
        progress.root = root.name
    with timed("root", root=root.name) as extra:
        try:
            candidates = []
            for c in walk(root, cfg, stats, unreadable):
                candidates.append(c)
                with progress.lock:
                    progress.dirs, progress.candidates = stats.dirs, len(candidates)
        except OSError as e:
            # An unavailable root (share not mounted) keeps its entries: never wipe the index
            # because of a transient outage.
            counts.roots_unavailable += 1
            log.warning("root_unavailable", root=root.name, error=type(e).__name__)
            db.touch(root.name, known, scan_id)
            return
        if not candidates and known and not full:
            # A mount point that lists empty is far more likely an unmounted share than a root
            # whose datasets were all deleted; a `full` scan removes them for real.
            counts.roots_unavailable += 1
            log.warning("root_empty_kept", root=root.name, n_kept=len(known))
            db.touch(root.name, known, scan_id)
            return
        extra["n_candidates"] = len(candidates)
        unchanged: list[str] = []
        batch: list[dict[str, Any]] = []
        results = pool.map(lambda c: (c, _check(c, known, full, cfg, progress)), candidates)
        for c, row in results:
            counts.found += 1
            if row is None:
                counts.unchanged += 1
                unchanged.append(c.relpath)
                continue
            previous = row.pop("_previous_version", None)
            if (
                previous
                and cache_dir is not None
                and adopt_store(
                    cache_dir, previous, row["version"], state_dir, row.get("fingerprint")
                )
            ):
                counts.adopted += 1
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
        # Entries under folders that could not be listed this time are kept (a permission blip
        # or a stale NFS handle must not drop them).
        kept = [p for p in known if any(p == u or p.startswith(u + "/") for u in unreadable if u)]
        db.touch(root.name, kept, scan_id)
        removed = db.remove_unseen(root.name, scan_id)
        counts.removed += removed
        for k, v in stats.__dict__.items():
            counts.walk[k] = counts.walk.get(k, 0) + v
        extra.update(stats.__dict__, removed=removed)
