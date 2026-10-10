"""Validate a dbx store against the contract (SPEC.md): manifest + array layout.

Cheap checks (shapes, dtypes, metadata, small index arrays) always run. `deep=True` also reads
large arrays fully (sortedness of points, ranges of rows); use it in tests and after conversion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from pydantic import ValidationError

from dbx.contract.models import (
    CONTRACT_MAJOR,
    DOWNSAMPLE_THRESHOLD,
    TILE,
    ImageEntry,
    LabelsEntry,
    Manifest,
    PointsEntry,
    ShapesEntry,
    TableEntry,
)

MANIFEST = "manifest.json"


@dataclass
class Report:
    store: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    manifest: Manifest | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def read_manifest(store: Path) -> Manifest:
    return Manifest.model_validate_json((store / MANIFEST).read_text())


def validate_store(store: Path, deep: bool = False) -> Report:
    store = Path(store)
    rep = Report(store=str(store))
    if not (store / MANIFEST).is_file():
        rep.error("store", "manifest.json missing (incomplete store)")
        return rep
    try:
        m = read_manifest(store)
    except (ValidationError, json.JSONDecodeError) as e:
        rep.error("manifest", str(e).replace("\n", " | "))
        return rep
    rep.manifest = m
    if m.major != CONTRACT_MAJOR:
        rep.error("manifest", f"contract major {m.major} != supported {CONTRACT_MAJOR}")
        return rep
    try:
        # Read nodes as they are on disk; consolidated metadata is checked separately.
        root = zarr.open_group(store, mode="r", use_consolidated=False)
    except Exception as e:  # noqa: BLE001 - any failure to open is a store error
        rep.error("store", f"not a zarr group: {e}")
        return rep

    systems = set(m.coordinate_systems)
    for kind in ("images", "labels", "shapes", "points"):
        for name, el in getattr(m, kind).items():
            unknown = set(el.transforms) - systems
            if unknown:
                rep.error(f"{kind}/{name}", f"transforms to undeclared systems {sorted(unknown)}")
            if not el.transforms:
                rep.warn(f"{kind}/{name}", "no transforms: element cannot be placed")

    for name, t in m.tables.items():
        _check(rep, f"tables/{name}", lambda: _table(rep, root, t, deep, name))  # noqa: B023
    for name, img in m.images.items():
        _check(rep, f"images/{name}", lambda: _image(rep, root, img, name))  # noqa: B023
    for name, lab in m.labels.items():
        _check(rep, f"labels/{name}", lambda: _labels(rep, root, m, lab, deep, name))  # noqa: B023
    for name, sh in m.shapes.items():
        _check(rep, f"shapes/{name}", lambda: _shapes(rep, root, m, sh, deep, name))  # noqa: B023
    for name, pt in m.points.items():
        _check(rep, f"points/{name}", lambda: _points(rep, root, m, pt, deep, name))  # noqa: B023
    return rep


def _check(rep: Report, where: str, fn: Any) -> None:
    try:
        fn()
    except KeyError as e:
        rep.error(where, f"missing node {e}")
    except Exception as e:  # noqa: BLE001 - report, keep validating other elements
        rep.error(where, f"{type(e).__name__}: {e}")


def _arr(root: zarr.Group, path: str) -> zarr.Array[Any]:
    node = root[path]
    if not isinstance(node, zarr.Array):
        raise KeyError(path)
    return node


def _attrs(node: zarr.Group | zarr.Array[Any]) -> dict[str, Any]:
    """Node attributes as a plain dict (zarr types them as JSON unions)."""
    return dict(node.attrs)


def _grp(root: zarr.Group, path: str) -> zarr.Group:
    node = root[path]
    if not isinstance(node, zarr.Group):
        raise KeyError(path)
    return node


def _dtype(a: zarr.Array[Any]) -> str:
    return str(np.dtype(a.dtype).name)


# ---------------------------------------------------------------- tables
def _table(rep: Report, root: zarr.Group, t: TableEntry, deep: bool, name: str) -> None:
    w = f"tables/{name}"
    g = _grp(root, t.path)
    if _attrs(g).get("encoding-type") != "anndata":
        rep.error(w, "group is not AnnData (encoding-type)")
    x = _grp(root, f"{t.path}/X")
    if _attrs(x).get("encoding-type") != "csc_matrix":
        rep.error(w, f"X must be csc_matrix, found {_attrs(x).get('encoding-type')}")
    shape = list(_attrs(x).get("shape", []))
    if shape != [t.n_obs, t.n_vars]:
        rep.error(w, f"X shape {shape} != manifest {[t.n_obs, t.n_vars]}")
    data, indices, indptr = (_arr(root, f"{t.path}/X/{k}") for k in ("data", "indices", "indptr"))
    if _dtype(data) != "float32":
        rep.error(w, f"X/data dtype {_dtype(data)} != float32")
    if _dtype(indices) not in ("int32", "int64"):
        rep.error(w, f"X/indices dtype {_dtype(indices)}")
    if _dtype(indptr) != "int64":
        rep.error(w, f"X/indptr dtype {_dtype(indptr)} != int64")
    if data.shape != indices.shape or data.shape[0] != t.nnz:
        rep.error(w, f"X data/indices length {data.shape}/{indices.shape} != nnz {t.nnz}")
    if indptr.shape[0] != t.n_vars + 1:
        rep.error(w, f"X/indptr length {indptr.shape[0]} != n_vars + 1")
    else:
        ip = np.asarray(indptr[:])
        if ip[0] != 0 or ip[-1] != t.nnz or np.any(np.diff(ip) < 0):
            rep.error(w, "X/indptr must start at 0, end at nnz and be non-decreasing")
    if deep and t.nnz:
        idx = np.asarray(indices[:])
        if idx.min() < 0 or idx.max() >= t.n_obs:
            rep.error(w, "X/indices out of range")

    try:
        consolidated = zarr.open_group(root.store, path=t.path, mode="r", use_consolidated=True)
        if "X" not in consolidated or "dbx" not in consolidated["uns"]:  # type: ignore[operator]
            rep.error(w, "consolidated metadata is stale (re-consolidate after writing)")
    except ValueError:
        rep.error(w, "consolidated metadata missing")

    obs = _grp(root, f"{t.path}/obs")
    for col, info in t.categorical.items():
        cat = _grp(obs, col)
        n = _arr(cat, "categories").shape[0]
        if n != info.n_categories:
            rep.error(w, f"obs/{col}: {n} categories != manifest {info.n_categories}")
        colors_path = f"{t.path}/uns/{col}_colors"
        if info.has_colors:
            try:
                nc = _arr(root, colors_path).shape[0]
            except KeyError:
                rep.error(w, f"uns/{col}_colors missing")
            else:
                if nc != n:
                    rep.error(w, f"uns/{col}_colors has {nc} colours for {n} categories")
    for col in t.numeric:
        if col not in obs:
            rep.error(w, f"obs/{col} missing")
    for key, dims in [*t.embeddings.items(), *((k, 2) for k in t.spatial_keys)]:
        a = _arr(root, f"{t.path}/obsm/{key}")
        if a.shape[0] != t.n_obs or (key in t.embeddings and a.shape[1] != dims):
            rep.error(w, f"obsm/{key} shape {a.shape} does not match n_obs/dims")
        if _dtype(a) != "float32":
            rep.error(w, f"obsm/{key} dtype {_dtype(a)} != float32")

    stats = _grp(root, f"{t.path}/uns/dbx/gene_stats")
    for s in ("nnz", "mean", "max", "p99"):
        a = _arr(stats, s)
        if a.shape != (t.n_vars,) or _dtype(a) != "float32":
            rep.error(w, f"gene_stats/{s} must be float32 of length n_vars")
    expected = (
        [n for n in (200_000, 1_000_000) if n < t.n_obs] if t.n_obs > DOWNSAMPLE_THRESHOLD else []
    )
    if sorted(t.downsample) != expected:
        rep.error(w, f"downsample sizes {t.downsample} != expected {expected}")
    for n in t.downsample:
        a = _arr(root, f"{t.path}/uns/dbx/downsample/{n}")
        if a.shape != (n,):
            rep.error(w, f"downsample/{n} has length {a.shape[0]}")
        elif deep:
            v = np.asarray(a[:])
            if np.any(np.diff(v) <= 0) or v[0] < 0 or v[-1] >= t.n_obs:
                rep.error(w, f"downsample/{n} must be strictly increasing cell indices")


# ---------------------------------------------------------------- images / labels
def _multiscale(
    rep: Report, w: str, g: zarr.Group, levels: list[list[int]], tile_axes: int
) -> None:
    ome = _attrs(g).get("ome")
    if not isinstance(ome, dict) or not str(ome.get("version", "")).startswith("0.5"):
        rep.error(w, "missing OME-NGFF 0.5 metadata (attrs.ome)")
        return
    ms = ome["multiscales"][0]
    paths = [d["path"] for d in ms["datasets"]]
    if len(paths) != len(levels):
        rep.error(w, f"{len(paths)} pyramid levels in metadata, {len(levels)} in manifest")
    for i, (p, shape) in enumerate(zip(paths, levels, strict=False)):
        a = _arr(g, p)
        if list(a.shape) != shape:
            rep.error(w, f"level {i} shape {list(a.shape)} != manifest {shape}")
        ch = list(a.chunks)
        if ch[-tile_axes:] != [min(TILE, s) for s in a.shape[-tile_axes:]]:
            rep.error(w, f"level {i} chunks {ch} are not {TILE}x{TILE} tiles")
        if i > 0:
            prev = levels[i - 1]
            for ax in (-2, -1):
                if shape[ax] != (prev[ax] + 1) // 2:
                    rep.error(w, f"level {i} is not half of level {i - 1}")
                    break
    if levels and max(levels[-1][-2:]) > TILE:
        rep.error(w, f"last level {levels[-1]} larger than {TILE} px")


def _image(rep: Report, root: zarr.Group, img: ImageEntry, name: str) -> None:
    w = f"images/{name}"
    g = _grp(root, img.path)
    if img.axes != ["c", "y", "x"]:
        rep.error(w, f"axes {img.axes} not supported in v0 (c, y, x)")
    _multiscale(rep, w, g, img.levels, tile_axes=2)
    if img.levels:
        c = img.levels[0][0]
        if len(img.channels) != c:
            rep.error(w, f"{len(img.channels)} channels in manifest, {c} in array")
        a = _arr(g, _attrs(g)["ome"]["multiscales"][0]["datasets"][0]["path"])
        if _dtype(a) != img.dtype:
            rep.error(w, f"dtype {_dtype(a)} != manifest {img.dtype}")
        if a.chunks[0] != 1:
            rep.error(w, "chunks must hold one channel")
        omero = _attrs(g)["ome"].get("omero", {})
        if len(omero.get("channels", [])) != c:
            rep.error(w, "omero.channels missing or wrong length")
    if img.rgb and img.levels and img.levels[0][0] not in (3, 4):
        rep.error(w, "rgb image must have 3 or 4 channels")


def _labels(
    rep: Report, root: zarr.Group, m: Manifest, lab: LabelsEntry, deep: bool, name: str
) -> None:
    w = f"labels/{name}"
    g = _grp(root, lab.path)
    _multiscale(rep, w, g, lab.levels, tile_axes=2)
    a0 = _arr(g, _attrs(g)["ome"]["multiscales"][0]["datasets"][0]["path"])
    if _dtype(a0) != "uint32":
        rep.error(w, f"dtype {_dtype(a0)} != uint32")
    if (lab.table is None) != (lab.lookup is None):
        rep.error(w, "table and lookup must be set together")
    if lab.table is not None and lab.lookup is not None:
        if lab.table not in m.tables:
            rep.error(w, f"unknown table {lab.table!r}")
            return
        lk = _arr(root, lab.lookup)
        if lk.shape != (lab.max_label + 1,) or _dtype(lk) != "int32":
            rep.error(w, "lookup must be int32 of length max_label + 1")
        elif deep:
            v = np.asarray(lk[:])
            if v.min() < -1 or v.max() >= m.tables[lab.table].n_obs:
                rep.error(w, "lookup rows out of range")
    if deep:
        mx = int(np.asarray(a0[:]).max())
        if mx > lab.max_label:
            rep.error(w, f"label value {mx} > max_label {lab.max_label}")


# ---------------------------------------------------------------- shapes / points
def _row_range(
    rep: Report, w: str, root: zarr.Group, m: Manifest, path: str, n: int, table: str | None
) -> None:
    v = np.asarray(_arr(root, path)[:])
    limit = m.tables[table].n_obs if table in m.tables else n
    if v.size and (v.min() < -1 or v.max() >= limit):
        rep.error(w, f"{path.rsplit('/', 1)[-1]} values out of range")


def _shapes(
    rep: Report, root: zarr.Group, m: Manifest, sh: ShapesEntry, deep: bool, name: str
) -> None:
    w = f"shapes/{name}"
    if sh.table is not None and sh.table not in m.tables:
        rep.error(w, f"unknown table {sh.table!r}")
    p = sh.path
    if sh.kind == "polygons":
        verts, starts, row, bbox = (
            _arr(root, f"{p}/{k}") for k in ("vertices", "starts", "row", "bbox")
        )
        if verts.ndim != 2 or verts.shape[1] != 2 or _dtype(verts) != "float32":
            rep.error(w, "vertices must be float32 (V, 2)")
        if starts.shape != (sh.n + 1,) or _dtype(starts) != "int64":
            rep.error(w, "starts must be int64 of length n + 1")
        else:
            s = np.asarray(starts[:])
            if s[0] != 0 or s[-1] != verts.shape[0] or np.any(np.diff(s) < 3):
                rep.error(w, "starts must run 0..V with ≥3 vertices per polygon")
        if row.shape != (sh.n,) or _dtype(row) != "int32":
            rep.error(w, "row must be int32 of length n")
        if bbox.shape != (sh.n, 4):
            rep.error(w, "bbox must be (n, 4)")
    else:
        center, radius, row = (_arr(root, f"{p}/{k}") for k in ("center", "radius", "row"))
        if center.shape != (sh.n, 2) or radius.shape != (sh.n,) or row.shape != (sh.n,):
            rep.error(w, "circles arrays must have n rows")
    if deep:
        _row_range(rep, w, root, m, f"{p}/row", sh.n, sh.table)


def _points(
    rep: Report, root: zarr.Group, m: Manifest, pt: PointsEntry, deep: bool, name: str
) -> None:
    w = f"points/{name}"
    p = pt.path
    xy, feature, offsets, overview = (
        _arr(root, f"{p}/{k}") for k in ("xy", "feature", "offsets", "overview")
    )
    features = (_attrs(_grp(root, p)).get("dbx") or {}).get("features")
    if xy.shape != (pt.n, 2) or _dtype(xy) != "float32":
        rep.error(w, "xy must be float32 (n, 2)")
    if feature.shape != (pt.n,) or _dtype(feature) != "int32":
        rep.error(w, "feature must be int32 of length n")
    if not isinstance(features, list) or len(features) != pt.n_features:
        rep.error(w, "attrs dbx.features must list n_features names")
    expected_idx = "int32" if pt.n < 2**31 else "int64"
    if _dtype(overview) != expected_idx:
        rep.error(w, f"overview dtype {_dtype(overview)} != {expected_idx}")
    if offsets.shape != (pt.n_features + 1,) or _dtype(offsets) != "int64":
        rep.error(w, "offsets must be int64 of length n_features + 1")
    else:
        o = np.asarray(offsets[:])
        if o[0] != 0 or o[-1] != pt.n or np.any(np.diff(o) < 0):
            rep.error(w, "offsets must run 0..n and be non-decreasing")
    if overview.shape[0] > min(pt.n, 1_000_000):
        rep.error(w, "overview larger than allowed")
    if pt.has_cell:
        cell = _arr(root, f"{p}/cell")
        if cell.shape != (pt.n,) or _dtype(cell) != "int32":
            rep.error(w, "cell must be int32 of length n")
    if pt.has_z:
        z = _arr(root, f"{p}/z")
        if z.shape != (pt.n,) or _dtype(z) != "float32":
            rep.error(w, "z must be float32 of length n")
        if pt.z_range is None:
            rep.error(w, "has_z requires z_range")
        elif deep and pt.n:
            zv = np.asarray(z[:])
            if zv.min() < pt.z_range[0] or zv.max() > pt.z_range[1]:
                rep.error(w, "z values outside z_range")
            if pt.z_planes is not None and not np.isin(zv, pt.z_planes).all():
                rep.error(w, "z values not in z_planes")
    elif pt.z_range is not None or pt.z_planes is not None:
        rep.error(w, "z_range/z_planes set without has_z")
    if pt.z_planes is not None and len(pt.z_planes) > 256:
        rep.error(w, "z_planes lists more than 256 values")
    if pt.table is not None and pt.table not in m.tables:
        rep.error(w, f"unknown table {pt.table!r}")
    if deep and pt.n:
        f = np.asarray(feature[:])
        if np.any(np.diff(f) < 0):
            rep.error(w, "points are not sorted by feature")
        o = np.asarray(offsets[:])
        counts = np.bincount(f, minlength=pt.n_features)
        if counts.shape[0] != pt.n_features or not np.array_equal(np.diff(o), counts):
            rep.error(w, "offsets do not match feature counts")
        ov = np.asarray(overview[:])
        if ov.size and (ov.min() < 0 or ov.max() >= pt.n):
            rep.error(w, "overview indices out of range")
        if pt.has_cell:
            _row_range(rep, w, root, m, f"{p}/cell", pt.n, pt.table)
