"""Detect what a path is and produce its fingerprint."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import h5py
import zarr

from dbx.inspect.anndata_info import anndata_info
from dbx.inspect.models import DatasetKind, Fingerprint
from dbx.inspect.nodes import Node
from dbx.inspect.spatialdata_info import spatialdata_info
from dbx.logs import timed

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


def _mtime_for_id(path: Path) -> int:
    """File mtime, or for a store the newest of its root and root metadata."""
    if path.is_file():
        return path.stat().st_mtime_ns
    stamps = [path.stat().st_mtime_ns]
    stamps += [(path / m).stat().st_mtime_ns for m in ZARR_META if (path / m).exists()]
    return max(stamps)


def dataset_id(path: Path) -> str:
    real = os.path.realpath(path)
    return hashlib.sha256(f"{real}|{_mtime_for_id(path)}".encode()).hexdigest()[:16]


def inspect_path(path: Path) -> Fingerprint:
    """Fingerprint a dataset. Raises ValueError if the path is not a supported dataset."""
    path = Path(path)
    kind = detect_kind(path)
    if kind is None:
        raise ValueError(f"not a supported dataset: {path}")
    with timed("inspect", kind=kind) as extra:
        if path.is_file():
            size, exact = path.stat().st_size, True
        else:
            size, exact = _dir_size(path)
        fp = Fingerprint(
            dataset_id=dataset_id(path),
            kind=kind,
            path=str(path),
            name=path.name,
            size_bytes=size,
            size_exact=exact,
            mtime=path.stat().st_mtime,
        )
        if kind == "h5ad":
            with h5py.File(path, "r") as f:
                fp.anndata = anndata_info(Node(f))
        elif kind == "anndata_zarr":
            fp.anndata = anndata_info(Node(zarr.open_group(path, mode="r")))
        else:
            fp.spatialdata = spatialdata_info(Node(zarr.open_group(path, mode="r")), path)
        fp.warnings = _warnings(fp)
        extra["dataset_id"] = fp.dataset_id
        extra["size_bytes"] = size
    return fp


def _warnings(fp: Fingerprint) -> list[str]:
    """Structural facts that change how the dataset can be served."""
    out: list[str] = []
    tables = (
        {"": fp.anndata} if fp.anndata else dict(fp.spatialdata.tables if fp.spatialdata else {})
    )
    for name, ad in tables.items():
        where = f"table {name!r}: " if name else ""
        if ad.X is not None and ad.X.encoding == "csr":
            out.append(f"{where}X is CSR (cell-major): gene queries need conversion or full load")
        if ad.X is not None and ad.X.dtype not in ("float32", "float64"):
            out.append(f"{where}X dtype is {ad.X.dtype}")
    if fp.spatialdata:
        for name, img in fp.spatialdata.images.items():
            if len(img.levels) == 1:
                out.append(f"image {name!r} has a single resolution level (no pyramid)")
            lvl = img.levels[0]
            if lvl.chunks and lvl.chunks == lvl.shape:
                out.append(f"image {name!r} is stored as one chunk")
        for name, pts in fp.spatialdata.points.items():
            if pts.feature_key is None:
                out.append(f"points {name!r} declare no feature_key")
    return sorted(set(out))
