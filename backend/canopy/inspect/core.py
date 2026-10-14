"""Detect what a path is and produce its fingerprint."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import h5py

from canopy.inspect.anndata_info import anndata_info
from canopy.inspect.guard import check_h5, open_zarr
from canopy.inspect.models import AnnDataInfo, DatasetKind, Fingerprint, Notice
from canopy.inspect.nodes import Node
from canopy.inspect.spatialdata_info import spatialdata_info
from canopy.logs import timed

HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
ZARR_META = ("zarr.json", ".zgroup")
# Stop counting files in a directory store after this many entries (size is then a lower bound).
SIZE_SCAN_LIMIT = 200_000


def _zarr_root_attrs(path: Path) -> dict[str, object] | None:
    if (path / "zarr.json").is_file():
        meta = json.loads((path / "zarr.json").read_text())
        if meta.get("node_type") != "group":
            return None
        return dict(meta.get("attributes") or {})
    if (path / ".zgroup").is_file():
        za = path / ".zattrs"
        return dict(json.loads(za.read_text())) if za.is_file() else {}
    return None


SPATIAL_GROUPS = ("images", "labels", "points", "shapes", "tables")


def _is_group_dir(path: Path) -> bool:
    if (path / ".zgroup").is_file():
        return True
    try:
        return bool(json.loads((path / "zarr.json").read_text()).get("node_type") == "group")
    except (OSError, ValueError):
        return False


def detect_kind(path: Path) -> DatasetKind | None:
    """Cheap detection from file signature or root metadata; None if not a dataset."""
    if path.is_file():
        if path.suffix.lower() != ".h5ad":
            return None
        with path.open("rb") as f:
            return "h5ad" if f.read(8) == HDF5_SIGNATURE else None
    if path.is_dir():
        attrs = _zarr_root_attrs(path)
        if attrs is None:
            return None
        if "spatialdata_attrs" in attrs:
            return "spatialdata_zarr"
        if attrs.get("encoding-type") == "anndata":
            return "anndata_zarr"
        # Older SpatialData writers left the root attributes empty.
        if any(_is_group_dir(path / g) for g in SPATIAL_GROUPS):
            return "spatialdata_zarr"
    return None


def is_zarr_store(path: Path) -> bool:
    return path.is_dir() and any((path / m).is_file() for m in ZARR_META)


def _dir_size(path: Path, limit: int = SIZE_SCAN_LIMIT) -> tuple[int, bool]:
    total, count = 0, 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            count += 1
            if count >= limit:
                return total, False
    return total, True


def _store_stamps(path: Path, depth: int = 4) -> list[int]:
    """Modification times of a store's group folders and their direct entries (metadata files,
    array folders, parquet files). Only groups are descended into, never array chunk folders,
    so this stays cheap on network storage. Rewriting chunks in place without touching any
    metadata is not detected."""
    stamps = [path.stat().st_mtime_ns]
    try:
        entries = list(os.scandir(path))
    except OSError:
        return stamps
    for e in entries:
        try:
            stamps.append(e.stat().st_mtime_ns)
        except OSError:
            continue
        if depth > 1 and e.is_dir() and _is_group_dir(Path(e.path)):
            stamps += _store_stamps(Path(e.path), depth - 1)
    return stamps


def version_stamp(path: Path) -> str:
    """What changes when a dataset changes: size and mtime (file), or the newest modification
    time of the store's groups and metadata (zarr store)."""
    if path.is_file():
        st = path.stat()
        return f"{st.st_size}|{st.st_mtime_ns}"
    return str(max(_store_stamps(path)))


def dataset_id(path: Path, stamp: str | None = None, identity: str | None = None) -> str:
    """Stable id of a dataset version: what it is (`identity`: "<root name>/<path in the root>"
    for a dataset of the index, so that moving or remounting the whole data folder keeps it;
    otherwise its real path) plus its version stamp."""
    where = identity or os.path.realpath(path)
    return hashlib.sha256(f"{where}|{stamp or version_stamp(path)}".encode()).hexdigest()[:16]


# HDF5's own wording for lock failures ("sblock" in other messages must not match).
_LOCK_ERRORS = ("unable to lock file", "file locking", "lock failed")


def open_h5(path: Path) -> h5py.File:
    """Open an HDF5 file read-only without HDF5 file locking (locks commonly fail on network
    shares and are not needed for reading). Lock failures and truncated files (half-copied,
    still being written, disk full) get recognisable messages: `hdf5_lock: …`,
    `hdf5_truncated: …`."""
    try:
        # no locking flag here: HDF5_USE_FILE_LOCKING=FALSE (canopy/__init__.py) applies to
        # this and to libraries' own opens (anndata) alike
        f = h5py.File(path, "r")
    except OSError as e:
        msg = str(e).lower()
        if "truncated file" in msg:
            raise OSError("hdf5_truncated: the file is shorter than its header says") from e
        if any(m in msg for m in _LOCK_ERRORS):
            raise OSError("hdf5_lock: the file cannot be opened because of a file lock") from e
        raise
    try:
        check_h5(f)  # nothing outside the file is read through it (review full-2 A-H1)
    except BaseException:
        f.close()
        raise
    return f


def inspect_path(
    path: Path, *, measure_size: bool = True, identity: str | None = None
) -> Fingerprint:
    """Fingerprint a dataset. Raises ValueError if the path is not a supported dataset.

    `measure_size=False` skips counting the files of a directory store (slow on network
    storage); `size_bytes` is then None. `identity`: see `dataset_id`.
    """
    path = Path(path).absolute()
    kind = detect_kind(path)
    if kind is None:
        raise ValueError("not a supported dataset")
    with timed("inspect", kind=kind) as extra:
        size: int | None
        if path.is_file():
            size, exact = path.stat().st_size, True
        elif measure_size:
            size, exact = _dir_size(path)
        else:
            size, exact = None, False
        fp = Fingerprint(
            dataset_id=dataset_id(path, identity=identity),
            identity=identity,
            kind=kind,
            path=str(path),
            name=path.name,
            size_bytes=size,
            size_exact=exact,
            mtime=path.stat().st_mtime,
        )
        if kind == "h5ad":
            with open_h5(path) as f:
                if "obs" not in f or "var" not in f:
                    raise ValueError("not_anndata")  # an HDF5 file, but not AnnData
                fp.anndata = anndata_info(Node(f))
        elif kind == "anndata_zarr":
            fp.anndata = anndata_info(Node(open_zarr(path)))
        else:
            fp.spatialdata = spatialdata_info(Node(open_zarr(path)), path, fp.errors)
        fp.warnings = _warnings(fp)
        extra["version"] = fp.dataset_id  # the content version (the catalog id is `dataset_id`)
        extra["size_bytes"] = size
        extra["n_errors"] = len(fp.errors)
    return fp


def _warnings(fp: Fingerprint) -> list[Notice]:
    """Structural facts that change how the dataset can be served."""
    out: list[Notice] = []
    tables: dict[str, AnnDataInfo] = (
        {"": fp.anndata} if fp.anndata else dict(fp.spatialdata.tables if fp.spatialdata else {})
    )
    for name, ad in tables.items():
        el = {"kind": "table", "element": name} if name else {}
        if ad.X is not None and ad.X.encoding == "csr":
            out.append(
                Notice(
                    code="x_csr",
                    message="X is CSR (cell-major): gene queries need conversion or full load",
                    **el,
                )
            )
        if ad.X is not None and ad.X.dtype not in ("float32", "float64"):
            out.append(Notice(code="x_dtype", message=f"X dtype is {ad.X.dtype}", **el))
        if ad.layout != "current":
            out.append(
                Notice(
                    code="legacy_layout",
                    message=f"legacy anndata layout ({ad.layout}): served after conversion",
                    **el,
                )
            )
        if ad.X is not None and len(ad.X.shape) == 2 and ad.X.shape != [ad.n_obs, ad.n_vars]:
            out.append(
                Notice(
                    code="shape_mismatch",
                    message=f"obs/var describe {ad.n_obs}x{ad.n_vars} but X is "
                    f"{ad.X.shape[0]}x{ad.X.shape[1]}",
                    **el,
                )
            )
    if fp.spatialdata:
        sd = fp.spatialdata
        if sd.format_version is None:
            out.append(
                Notice(code="no_spatialdata_attrs", message="no SpatialData version at the root")
            )
        for name, img in sd.images.items():
            if len(img.levels) == 1:
                out.append(
                    Notice(
                        code="single_level",
                        message="image has a single resolution level (no pyramid)",
                        kind="image",
                        element=name,
                    )
                )
            lvl = img.levels[0]
            if lvl.chunks and lvl.chunks == lvl.shape:
                out.append(
                    Notice(
                        code="single_chunk",
                        message="image is stored as one chunk",
                        kind="image",
                        element=name,
                    )
                )
        for name, pts in sd.points.items():
            if pts.feature_key is None:
                out.append(
                    Notice(
                        code="no_feature_key",
                        message="points declare no feature_key",
                        kind="points",
                        element=name,
                    )
                )
    if fp.errors:
        out.append(
            Notice(code="element_errors", message=f"{len(fp.errors)} element(s) could not be read")
        )
    uniq = {(n.code, n.kind, n.element): n for n in out}
    return [uniq[k] for k in sorted(uniq, key=lambda k: tuple(x or "" for x in k))]
