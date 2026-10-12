"""Validate a dbx store against the contract (SPEC.md): manifest, array layout and content.

Cheap checks (manifest, metadata, chunk layout, small index arrays) always run. `deep=True`
also checks array contents (sort orders, ranges, ring orientation, …), reading every large array
chunk by chunk so memory stays bounded for 100+ GB stores (SPEC §12).
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from pydantic import ValidationError
from zarr.codecs import ZstdCodec

from dbx.contract.models import (
    CONTRACT_MAJOR,
    CONTRACT_MINOR,
    DOWNSAMPLE_THRESHOLD,
    IMAGE_SHARD,
    IMAGE_SHARD_ABOVE,
    INDPTR_CHUNK,
    NAME_PATTERN,
    OBSM_CHUNK,
    POINT_CHUNK,
    POLY_BLOCK,
    SHARD_CHUNKS,
    TILE,
    VERTEX_CHUNK,
    X_CHUNK,
    ZSTD_LEVEL,
    ImageEntry,
    LabelsEntry,
    Manifest,
    PointsEntry,
    ShapesEntry,
    TableEntry,
)
from dbx.contract.write import morton_order

MANIFEST = "manifest.json"
# Rows read at a time by deep checks of arrays without a natural block size.
SCAN_ROWS = 1_048_576


# Names inside validator messages: quoted values and the part after a group prefix (obs/<col>).
_NAMES_IN = re.compile(
    r"'[^']*'|\"[^\"]*\"|\b(obs|var|obsm|uns|layers|downsample|gene_stats)/[^\s:,]+"
)


def _name_free(m: re.Match[str]) -> str:
    return f"{m.group(1)}/<name>" if m.group(1) else "<name>"


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

    def problems(self, n: int = 20) -> list[dict[str, str]]:
        """The first errors for logs: `element` (the where, a name field: tokenised at strict)
        and `problem` (the message with the names in it replaced by `<name>`)."""
        out = []
        for e in self.errors[:n]:
            where, _, msg = e.partition(": ")
            out.append({"element": where, "problem": _NAMES_IN.sub(_name_free, msg)})
        return out

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def read_manifest(store: Path) -> Manifest:
    """Reader view: unknown fields are ignored (SPEC §9)."""
    return Manifest.model_validate_json((store / MANIFEST).read_text())


def unknown_fields(raw: Any, parsed: Any, prefix: str = "") -> list[str]:
    """Paths of keys present in the raw manifest but not understood by the models."""
    out: list[str] = []
    if isinstance(raw, dict) and isinstance(parsed, dict):
        for k, v in raw.items():
            p = f"{prefix}.{k}" if prefix else k
            if k not in parsed:
                out.append(p)
            else:
                out += unknown_fields(v, parsed[k], p)
    return out


def validate_store(store: Path, deep: bool = False) -> Report:
    store = Path(store)
    rep = Report(store=str(store))
    if not (store / MANIFEST).is_file():
        rep.error("store", "manifest.json missing (incomplete store)")
        return rep
    non_finite: list[str] = []

    def constant(c: str) -> float:
        non_finite.append(c)
        return float(c)

    try:
        raw = json.loads((store / MANIFEST).read_text(), parse_constant=constant)
        m = Manifest.model_validate(raw)
    except (ValidationError, json.JSONDecodeError) as e:
        rep.error("manifest", str(e).replace("\n", " | "))
        return rep
    rep.manifest = m
    if non_finite:
        # JSON has no NaN or Infinity: browsers cannot parse such a manifest
        rep.error("manifest", f"{len(non_finite)} non-finite numbers (NaN or Infinity)")
    if m.major != CONTRACT_MAJOR:
        rep.error("manifest", f"contract major {m.major} != supported {CONTRACT_MAJOR}")
        return rep
    extra = unknown_fields(raw, m.model_dump(mode="json"))
    if m.minor > CONTRACT_MINOR:
        rep.warn("manifest", f"store contract {m.contract_version} is newer than this validator")
        for p in extra:
            rep.warn("manifest", f"field {p} not known to this validator")
    else:
        for p in extra:
            rep.error("manifest", f"unknown field {p}")
    try:
        # Read nodes as they are on disk; consolidated metadata is checked separately.
        root = zarr.open_group(store, mode="r", use_consolidated=False)
    except Exception as e:  # noqa: BLE001 - any failure to open is a store error
        rep.error("store", f"not a zarr group: {e}")
        return rep

    _names(rep, m)
    systems = set(m.coordinate_systems)
    if set(m.units) - systems:
        rep.error("manifest", f"units for undeclared systems {sorted(set(m.units) - systems)}")
    for kind in ("images", "labels", "shapes", "points"):
        for name, el in getattr(m, kind).items():
            unknown = set(el.transforms) - systems
            if unknown:
                rep.error(f"{kind}/{name}", f"transforms to undeclared systems {sorted(unknown)}")
            if not el.transforms:
                rep.warn(f"{kind}/{name}", "no transforms: element cannot be placed")

    _check(rep, "store", lambda: _root_consolidated(rep, store, root))
    for name, t in m.tables.items():
        _check(rep, f"tables/{name}", lambda: _table(rep, root, m, t, deep, name))  # noqa: B023
    for name, img in m.images.items():
        _check(rep, f"images/{name}", lambda: _image(rep, root, img, name))  # noqa: B023
    for name, lab in m.labels.items():
        _check(rep, f"labels/{name}", lambda: _labels(rep, root, m, lab, deep, name))  # noqa: B023
    for name, sh in m.shapes.items():
        _check(rep, f"shapes/{name}", lambda: _shapes(rep, root, m, sh, deep, name))  # noqa: B023
    for name, pt in m.points.items():
        _check(rep, f"points/{name}", lambda: _points(rep, root, m, pt, deep, name))  # noqa: B023
    return rep


def _check(rep: Report, where: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except KeyError as e:
        rep.error(where, f"missing node {e}")
    except Exception as e:  # noqa: BLE001 - report, keep validating other elements
        rep.error(where, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- helpers
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


def _index_dtype(n: int) -> str:
    return "uint32" if n < 2**32 else "uint64"


def _codec(rep: Report, w: str, a: zarr.Array[Any], name: str) -> None:
    comps = a.compressors
    if len(comps) != 1 or not isinstance(comps[0], ZstdCodec) or comps[0].level != ZSTD_LEVEL:
        rep.error(w, f"{name}: codec {comps} is not zstd level {ZSTD_LEVEL}")


def _layout(
    rep: Report,
    w: str,
    a: zarr.Array[Any],
    name: str,
    rows: int,
    *,
    sharded: bool = False,
    codec: bool = True,
) -> None:
    """Chunking along rows (all other axes whole), optional sharding, and codec."""
    rest = tuple(a.shape[1:])
    want = (rows, *rest)
    if tuple(a.chunks) != want:
        rep.error(w, f"{name}: chunks {tuple(a.chunks)} != {want}")
    if sharded and tuple(a.shards or ()) != (rows * SHARD_CHUNKS, *rest):
        rep.error(w, f"{name}: shards {a.shards} != {(rows * SHARD_CHUNKS, *rest)}")
    if codec:
        _codec(rep, w, a, name)


def _blocks(a: zarr.Array[Any], rows: int = SCAN_ROWS) -> Iterator[tuple[int, np.ndarray]]:
    """(start, block) along the first axis; bounded memory."""
    for s in range(0, a.shape[0], rows):
        yield s, np.asarray(a[s : s + rows])


def _names(rep: Report, m: Manifest) -> None:
    pattern = re.compile(NAME_PATTERN)
    for kind in ("tables", "images", "labels", "shapes", "points"):
        for name, el in getattr(m, kind).items():
            if not pattern.match(name):
                rep.error(f"{kind}/{name}", f"name must match {NAME_PATTERN}")
            if el.path != f"{kind}/{name}":
                rep.error(f"{kind}/{name}", f"path {el.path!r} != {kind}/{name!r}")


def _members(group: zarr.Group) -> set[str]:
    with warnings.catch_warnings():  # manifest.json is not a zarr node: expected
        warnings.filterwarnings("ignore", message="Object at", category=UserWarning)
        return {path for path, _ in group.members(max_depth=None)}


def _root_consolidated(rep: Report, store: Path, root: zarr.Group) -> None:
    try:
        cons = zarr.open_group(store, mode="r", use_consolidated=True)
    except ValueError:
        rep.error("store", "root consolidated metadata missing")
        return
    on_disk, listed = _members(root), _members(cons)
    if on_disk != listed:
        diff = sorted(on_disk ^ listed)[:5]
        rep.error("store", f"root consolidated metadata is stale (e.g. {diff})")


# ---------------------------------------------------------------- tables
def _table(
    rep: Report, root: zarr.Group, m: Manifest, t: TableEntry, deep: bool, name: str
) -> None:
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
    want_idx = "int32" if t.n_obs < 2**31 else "int64"
    if _dtype(indices) != want_idx:
        rep.error(w, f"X/indices dtype {_dtype(indices)} != {want_idx}")
    if _dtype(indptr) != "int64":
        rep.error(w, f"X/indptr dtype {_dtype(indptr)} != int64")
    for arr_name, a, rows in (("X/data", data, X_CHUNK), ("X/indices", indices, X_CHUNK)):
        if a.shards is None:
            _layout(rep, w, a, arr_name, rows)
        else:
            _layout(rep, w, a, arr_name, rows, sharded=True)
    _layout(rep, w, indptr, "X/indptr", INDPTR_CHUNK)
    ip = np.zeros(0, np.int64)
    if data.shape != indices.shape or data.shape[0] != t.nnz:
        rep.error(w, f"X data/indices length {data.shape}/{indices.shape} != nnz {t.nnz}")
    if indptr.shape[0] != t.n_vars + 1:
        rep.error(w, f"X/indptr length {indptr.shape[0]} != n_vars + 1")
    else:
        ip = np.asarray(indptr[:])
        if ip[0] != 0 or ip[-1] != t.nnz or np.any(np.diff(ip) < 0):
            rep.error(w, "X/indptr must start at 0, end at nnz and be non-decreasing")
            ip = np.zeros(0, np.int64)
    if deep and t.nnz and ip.size:
        _csc_rows(rep, w, indices, ip, t.n_obs)

    try:
        consolidated = zarr.open_group(root.store, path=t.path, mode="r", use_consolidated=True)
        if _members(consolidated) != _members(g):
            rep.error(w, "consolidated metadata is stale (re-consolidate after writing)")
    except ValueError:
        rep.error(w, "consolidated metadata missing")

    obs = _grp(root, f"{t.path}/obs")
    for col, info in t.categorical.items():
        node = obs[col]
        if not isinstance(node, zarr.Group) or _attrs(node).get("encoding-type") != "categorical":
            rep.error(w, f"obs/{col} is listed as categorical but is not")
            continue
        n = _arr(node, "categories").shape[0]
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
        num = obs.get(col)
        if num is None:
            rep.error(w, f"obs/{col} missing")
        elif isinstance(num, zarr.Group) and _attrs(num).get("encoding-type") not in (
            "nullable-integer",
            "nullable-boolean",
        ):
            rep.error(
                w, f"obs/{col} is listed as numeric but is {_attrs(num).get('encoding-type')}"
            )
        elif isinstance(num, zarr.Array) and np.dtype(num.dtype).kind not in "iufb":
            rep.error(w, f"obs/{col} is listed as numeric but has dtype {num.dtype}")
    for key in t.section_keys:
        if key not in t.categorical:
            rep.error(w, f"section key {key!r} is not a categorical obs column")
    if t.var_names_column is not None and t.var_names_column not in _grp(root, f"{t.path}/var"):
        rep.error(w, f"var_names_column {t.var_names_column!r} missing from var")
    for region in t.region:
        if not any(region in getattr(m, k) for k in ("shapes", "labels", "points")):
            rep.error(w, f"region {region!r} is not an element of the store")
    for key, dims in [*t.embeddings.items(), *((k, 2) for k in t.spatial_keys)]:
        a = _arr(root, f"{t.path}/obsm/{key}")
        if a.ndim != 2 or a.shape[0] != t.n_obs or (key in t.embeddings and a.shape[1] != dims):
            rep.error(w, f"obsm/{key} shape {a.shape} does not match n_obs/dims")
            continue
        if _dtype(a) != "float32":
            rep.error(w, f"obsm/{key} dtype {_dtype(a)} != float32")
        _layout(rep, w, a, f"obsm/{key}", OBSM_CHUNK)

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
    want_ds = "int32" if t.n_obs < 2**31 else "int64"
    for n in t.downsample:
        a = _arr(root, f"{t.path}/uns/dbx/downsample/{n}")
        if a.shape != (n,) or _dtype(a) != want_ds:
            rep.error(w, f"downsample/{n} must be {want_ds} of length {n}")
        elif deep:
            v = np.asarray(a[:])
            if np.any(np.diff(v) <= 0) or v[0] < 0 or v[-1] >= t.n_obs:
                rep.error(w, f"downsample/{n} must be strictly increasing cell indices")


def _csc_rows(rep: Report, w: str, indices: zarr.Array[Any], ip: np.ndarray, n_obs: int) -> None:
    """Row indices in range, and sorted and unique within each gene (column), chunk by chunk."""
    col_start = np.zeros(int(ip[-1]) + 1, dtype=bool)
    col_start[ip] = True  # position p starts a new column (p == ip[j] for some j)
    prev: int | None = None
    for s, blk in _blocks(indices):
        b = blk.astype(np.int64)
        if b.min() < 0 or b.max() >= n_obs:
            rep.error(w, "X/indices out of range")
            return
        seq = b if prev is None else np.concatenate([[prev], b])
        first = s if prev is None else s - 1
        bad = np.diff(seq) <= 0
        bad &= ~col_start[first + 1 : first + 1 + bad.size]
        if bad.any():
            rep.error(w, "X/indices not sorted and unique within a gene (not canonical CSC)")
            return
        prev = int(b[-1])


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
        if a.shards is not None and (
            max(a.shape[-2:]) <= IMAGE_SHARD_ABOVE
            or list(a.shards[-tile_axes:])
            != [IMAGE_SHARD if v >= TILE else v for v in a.shape[-tile_axes:]]
            or any(v != 1 for v in a.shards[:-tile_axes])
        ):
            rep.error(w, f"level {i} shards {a.shards} are not 1x{IMAGE_SHARD}x{IMAGE_SHARD}")
        _codec(rep, w, a, f"level {i}")
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
        dt = np.dtype(img.dtype)
        if dt.kind in "iu":
            lo, hi = np.iinfo(dt).min, np.iinfo(dt).max
            for ch in img.channels:
                if not (lo <= ch.window[0] <= ch.window[1] <= hi):
                    rep.error(w, f"channel {ch.name!r} window {ch.window} outside {img.dtype}")
    if img.rgb and img.levels and img.levels[0][0] not in (3, 4):
        rep.error(w, "rgb image must have 3 or 4 channels")


def _labels(
    rep: Report, root: zarr.Group, m: Manifest, lab: LabelsEntry, deep: bool, name: str
) -> None:
    w = f"labels/{name}"
    g = _grp(root, lab.path)
    _multiscale(rep, w, g, lab.levels, tile_axes=2)
    ome = _attrs(g).get("ome") or {}
    if "image-label" not in ome:
        rep.error(w, "missing NGFF image-label metadata")
    listed = (_attrs(_grp(root, "labels")).get("ome") or {}).get("labels") or []
    if name not in listed:
        rep.error(w, "not listed in labels/ group attrs ome.labels")
    a0 = _arr(g, ome["multiscales"][0]["datasets"][0]["path"])
    if _dtype(a0) != "uint32":
        rep.error(w, f"dtype {_dtype(a0)} != uint32")
    if lab.max_label >= 2**32:
        rep.error(w, "max_label must be below 2^32 (relabel)")
    if lab.n_labels > lab.max_label:
        rep.error(w, "n_labels > max_label")
    if lab.relabelled:
        orig = _arr(root, f"aux/labels/{name}/original_id")
        if orig.shape != (lab.n_labels + 1,) or _dtype(orig) != "uint64":
            rep.error(w, "original_id must be uint64 of length n_labels + 1")
        if lab.max_label != lab.n_labels:
            rep.error(w, "relabelled masks must use labels 1..n_labels")
    elif lab.max_label > 4 * lab.n_labels + 1024:
        rep.error(w, "sparse label IDs must be relabelled (SPEC §6)")
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
            n_obs = m.tables[lab.table].n_obs
            for _, v in _blocks(lk):
                if v.min() < -1 or v.max() >= n_obs:
                    rep.error(w, "lookup rows out of range")
                    break
    if deep:
        mx = 0
        for _, blk in _blocks(a0, rows=a0.chunks[0]):
            mx = max(mx, int(blk.max()) if blk.size else 0)
        if mx > lab.max_label:
            rep.error(w, f"label value {mx} > max_label {lab.max_label}")


# ---------------------------------------------------------------- shapes
def _row_range(rep: Report, w: str, a: zarr.Array[Any], limit: int) -> None:
    for _, v in _blocks(a):
        if v.size and (v.min() < -1 or v.max() >= limit):
            rep.error(w, f"{a.basename} values out of range")
            return


def _shapes(
    rep: Report, root: zarr.Group, m: Manifest, sh: ShapesEntry, deep: bool, name: str
) -> None:
    w = f"shapes/{name}"
    if sh.table is not None and sh.table not in m.tables:
        rep.error(w, f"unknown table {sh.table!r}")
    limit = m.tables[sh.table].n_obs if sh.table in m.tables else sh.n
    p = sh.path
    if sh.kind == "circles":
        center, radius, row = (_arr(root, f"{p}/{k}") for k in ("center", "radius", "row"))
        if center.shape != (sh.n, 2) or radius.shape != (sh.n,) or row.shape != (sh.n,):
            rep.error(w, "circles arrays must have n rows")
        if deep:
            _row_range(rep, w, row, limit)
        return

    verts = _arr(root, f"{p}/vertices")
    starts, row, bbox, cent = (
        _arr(root, f"{p}/{k}") for k in ("starts", "row", "bbox", "centroid")
    )
    block_bbox = _arr(root, f"{p}/block_bbox")
    n_blocks = -(-sh.n // POLY_BLOCK)
    if verts.ndim != 2 or verts.shape[1] != 2 or _dtype(verts) != "float32":
        rep.error(w, "vertices must be float32 (V, 2)")
    if sh.n_vertices is not None and verts.shape[0] != sh.n_vertices:
        rep.error(w, f"{verts.shape[0]} vertices != manifest n_vertices {sh.n_vertices}")
    want = _index_dtype(verts.shape[0])
    if starts.shape != (sh.n + 1,) or _dtype(starts) != want:
        rep.error(w, f"starts must be {want} of length n + 1")
        return
    for k, a, shape, dt in (
        ("row", row, (sh.n,), "int32"),
        ("bbox", bbox, (sh.n, 4), "float32"),
        ("centroid", cent, (sh.n, 2), "float32"),
        ("block_bbox", block_bbox, (n_blocks, 4), "float32"),
    ):
        if a.shape != shape or _dtype(a) != dt:
            rep.error(w, f"{k} must be {dt} {shape}")
            return
    _layout(rep, w, verts, "vertices", VERTEX_CHUNK, sharded=True)
    for k, a in (("starts", starts), ("row", row), ("bbox", bbox), ("centroid", cent)):
        _layout(rep, w, a, k, POLY_BLOCK, sharded=True)
    _codec(rep, w, block_bbox, "block_bbox")
    s = np.asarray(starts[:]).astype(np.int64)  # n + 1 integers: small
    if s[0] != 0 or s[-1] != verts.shape[0] or np.any(np.diff(s) < 4):
        rep.error(w, "starts must run 0..V with ≥4 vertices per closed ring")
        return
    if not deep:
        return
    _row_range(rep, w, row, limit)
    bb = np.asarray(block_bbox[:])
    lo_c, hi_c = np.full(2, np.inf), np.full(2, -np.inf)
    for _, c in _blocks(cent):
        lo_c, hi_c = np.minimum(lo_c, c.min(0)), np.maximum(hi_c, c.max(0))
    ext = (float(lo_c[0]), float(lo_c[1]), float(hi_c[0]), float(hi_c[1]))
    last_code = -1
    for b in range(n_blocks):
        lo, hi = b * POLY_BLOCK, min(sh.n, (b + 1) * POLY_BLOCK)
        st = s[lo : hi + 1]
        v = np.asarray(verts[int(st[0]) : int(st[-1])]).astype(np.float64)
        local = st - st[0]
        firsts, lasts = local[:-1], local[1:] - 1
        if not np.array_equal(v[firsts], v[lasts]):
            rep.error(w, f"block {b}: rings are not closed")
            return
        cross = v[:-1, 0] * v[1:, 1] - v[1:, 0] * v[:-1, 1]
        cross = np.append(cross, 0.0)
        cross[lasts] = 0.0  # no edge from a ring's last vertex to the next ring
        area = np.add.reduceat(cross, firsts) / 2
        if np.any(area <= 0):
            rep.error(w, f"block {b}: rings are not counter-clockwise")
            return
        box = np.asarray(bbox[lo:hi])
        vmin = np.minimum.reduceat(v, firsts)
        vmax = np.maximum.reduceat(v, firsts)
        if not (
            np.allclose(box[:, :2], vmin, atol=1e-3) and np.allclose(box[:, 2:], vmax, atol=1e-3)
        ):
            rep.error(w, f"block {b}: bbox does not match vertices")
            return
        if np.any(bb[b, :2] > vmin.min(0) + 1e-3) or np.any(bb[b, 2:] < vmax.max(0) - 1e-3):
            rep.error(w, f"block {b}: block_bbox does not contain its polygons")
            return
        c = np.asarray(cent[lo:hi])
        codes = morton_order(c[:, 0], c[:, 1], ext).astype(np.int64)
        if np.any(np.diff(codes) < 0) or (codes.size and codes[0] < last_code):
            rep.error(w, "polygons are not in Morton order of their centroids")
            return
        if codes.size:
            last_code = int(codes[-1])


# ---------------------------------------------------------------- points
def _feature_rows(offsets: np.ndarray, start: int, stop: int) -> np.ndarray:
    """Feature index of rows start..stop-1, from the offsets (no per-point feature array)."""
    return np.searchsorted(offsets, np.arange(start, stop), side="right") - 1


def _sorted_by_feature_morton(
    xy: zarr.Array[Any], offsets: np.ndarray, box: tuple[float, float, float, float]
) -> bool:
    prev: tuple[int, int] | None = None
    for s, blk in _blocks(xy, rows=POINT_CHUNK * SHARD_CHUNKS):
        f = _feature_rows(offsets, s, s + blk.shape[0])
        code = morton_order(blk[:, 0], blk[:, 1], box).astype(np.int64)
        if prev is not None:
            f = np.concatenate([[prev[0]], f])
            code = np.concatenate([[prev[1]], code])
        same = np.diff(f) == 0
        if np.any(np.diff(code)[same] < 0):
            return False
        prev = (int(f[-1]), int(code[-1]))
    return True


def _points(
    rep: Report, root: zarr.Group, m: Manifest, pt: PointsEntry, deep: bool, name: str
) -> None:
    w = f"points/{name}"
    p = pt.path
    xy, offsets, chunk_bbox = (_arr(root, f"{p}/{k}") for k in ("xy", "offsets", "chunk_bbox"))
    features = (_attrs(_grp(root, p)).get("dbx") or {}).get("features")
    if not isinstance(features, list) or len(features) != pt.n_features:
        rep.error(w, "attrs dbx.features must list n_features names")
    if "feature" in _grp(root, p):
        rep.error(w, "per-point feature array is not part of v0.2 (use offsets)")
    if xy.shape != (pt.n, 2) or _dtype(xy) != "float32":
        rep.error(w, "xy must be float32 (n, 2)")
        return
    _layout(rep, w, xy, "xy", POINT_CHUNK, sharded=True)
    off_dt = _index_dtype(pt.n)
    if offsets.shape != (pt.n_features + 1,) or _dtype(offsets) != off_dt:
        rep.error(w, f"offsets must be {off_dt} of length n_features + 1")
        return
    o = np.asarray(offsets[:]).astype(np.int64)
    if o[0] != 0 or o[-1] != pt.n or np.any(np.diff(o) < 0):
        rep.error(w, "offsets must run 0..n and be non-decreasing")
        return
    n_chunks = -(-pt.n // POINT_CHUNK)
    if chunk_bbox.shape != (n_chunks, 4) or _dtype(chunk_bbox) != "float32":
        rep.error(w, f"chunk_bbox must be float32 ({n_chunks}, 4)")
        return
    cols = ["xy"]
    if pt.has_cell:
        cols.append("cell")
        cell = _arr(root, f"{p}/cell")
        if cell.shape != (pt.n,) or _dtype(cell) != "int32":
            rep.error(w, "cell must be int32 of length n")
        else:
            _layout(rep, w, cell, "cell", POINT_CHUNK, sharded=True)
    if pt.has_z:
        cols.append("z")
        z = _arr(root, f"{p}/z")
        if z.shape != (pt.n,) or _dtype(z) != "float32":
            rep.error(w, "z must be float32 of length n")
        else:
            _layout(rep, w, z, "z", POINT_CHUNK, sharded=True)
        if pt.z_range is None:
            rep.error(w, "has_z requires z_range")
    elif pt.z_range is not None or pt.z_planes is not None:
        rep.error(w, "z_range/z_planes set without has_z")
    if pt.z_planes is not None and len(pt.z_planes) > 256:
        rep.error(w, "z_planes lists more than 256 values")
    if pt.table is not None and pt.table not in m.tables:
        rep.error(w, f"unknown table {pt.table!r}")

    counts = np.diff(o)
    ov_n = min(pt.n, pt.n_overview)
    _reduced(rep, w, root, f"{p}/overview", [*cols, "feature"], pt, ov_n, off_dt, deep)
    lod_counts = np.where(counts > pt.lod_cap, pt.lod_cap, 0)
    _reduced(rep, w, root, f"{p}/lod", cols, pt, int(lod_counts.sum()), off_dt, deep, lod_counts)

    if not deep or not pt.n:
        return
    if not _sorted_by_feature_morton(xy, o, pt.bbox):
        rep.error(w, "points are not sorted by feature, then Morton code")
    bb = np.asarray(chunk_bbox[:])
    for i, (_, blk) in enumerate(_blocks(xy, rows=POINT_CHUNK)):
        if np.any(blk.min(0) < bb[i, :2] - 1e-3) or np.any(blk.max(0) > bb[i, 2:] + 1e-3):
            rep.error(w, f"chunk {i}: points outside chunk_bbox")
            break
    if pt.has_cell and pt.table in m.tables:
        _row_range(rep, w, _arr(root, f"{p}/cell"), m.tables[pt.table].n_obs)
    if pt.has_z and pt.z_range is not None:
        for _, zv in _blocks(_arr(root, f"{p}/z")):
            if zv.min() < pt.z_range[0] or zv.max() > pt.z_range[1]:
                rep.error(w, "z values outside z_range")
                break
            if pt.z_planes is not None and not np.isin(zv, pt.z_planes).all():
                rep.error(w, "z values not in z_planes")
                break


def _reduced(
    rep: Report,
    w: str,
    root: zarr.Group,
    path: str,
    cols: list[str],
    pt: PointsEntry,
    n: int,
    off_dt: str,
    deep: bool,
    expected_counts: np.ndarray | None = None,
) -> None:
    """The overview/ and lod/ groups: same arrays as the full element, plus offsets."""
    sub = path.rsplit("/", 1)[-1]
    g = _grp(root, path)
    for c in cols:
        a = _arr(g, c)
        if a.shape[0] != n:
            rep.error(w, f"{sub}/{c} has {a.shape[0]} rows, expected {n}")
            return
        _layout(rep, w, a, f"{sub}/{c}", POINT_CHUNK, sharded=True)
    off = _arr(g, "offsets")
    if off.shape != (pt.n_features + 1,) or _dtype(off) != off_dt:
        rep.error(w, f"{sub}/offsets must be {off_dt} of length n_features + 1")
        return
    o = np.asarray(off[:]).astype(np.int64)
    if o[0] != 0 or o[-1] != n or np.any(np.diff(o) < 0):
        rep.error(w, f"{sub}/offsets must run 0..{n}")
        return
    if expected_counts is not None and not np.array_equal(np.diff(o), expected_counts):
        rep.error(w, f"{sub}: per-gene counts must be min(count, lod_cap) above the cap, else 0")
    if deep and n:
        if "feature" in cols:
            f = np.asarray(_arr(g, "feature")[:])
            if not np.array_equal(np.bincount(f, minlength=pt.n_features), np.diff(o)):
                rep.error(w, f"{sub}/feature does not match {sub}/offsets")
        if not _sorted_by_feature_morton(_arr(g, "xy"), o, pt.bbox):
            rep.error(w, f"{sub} is not sorted by feature, then Morton code")
