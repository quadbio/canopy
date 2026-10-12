"""Convert a SpatialData zarr store into a dbx store (contract/SPEC.md §2–§8).

Tables go through the AnnData path (`write_table`); `spatialdata` reads only the other
elements, so no table matrix is loaded whole. Images and labels are written block by block from
the lazy (dask) source, so a chunked image or mask larger than memory is converted in tiles
(labels are relabelled with IDs collected block by block). Shapes and points are read whole:
points only with the columns the store needs (x, y, z, gene, cell). `spatial_memory_estimate`
is calibrated on measured peaks (bench/spatial_memory.py). Every element gets one 3×3 affine per
coordinate system, composed by `spatialdata`.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import time
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dask.array as da
import numpy as np
import pandas as pd
import zarr
from anndata.io import read_elem
from dask import config as dask_config
from dask.base import compute as dask_compute
from dask.callbacks import Callback
from dask.delayed import delayed

import dbx
from dbx.contract import validate_store
from dbx.contract.models import (
    CONTRACT_VERSION,
    IMAGE_SHARD,
    IMAGE_SHARD_ABOVE,
    LOD_CAP,
    OVERVIEW_POINTS,
    POLY_BLOCK,
    TILE,
    ChannelInfo,
    ConverterInfo,
    ImageEntry,
    LabelsEntry,
    Manifest,
    PointsEntry,
    ShapesEntry,
    SourceInfo,
    TableEntry,
    Unit,
)
from dbx.contract.write import (
    array,
    codecs,
    consolidate,
    downsample_mean,
    image_label_attrs,
    label_ids,
    labels_container_attrs,
    needs_relabel,
    ome_multiscale_attrs,
    points_layout,
    polygon_layout_flat,
    pyramid_levels,
    relabel_block,
    sharded,
    write_points,
    write_polygons,
)
from dbx.convert.anndata import (
    ConvertOptions,
    Progress,
    Result,
    StageProgress,
    _matrix_info,
    _publish,
    check_disk,
    disk_need,
    log_invalid,
    peak_rss_gb,
    store_dir,
    table_memory_estimate,
    write_manifest,
    write_table,
)
from dbx.convert.matrix import ConversionError
from dbx.inspect.core import dataset_id, version_stamp
from dbx.inspect.models import Fingerprint
from dbx.logs import get_logger, timed

# Overall progress: share of the work each stage takes (roughly).
STAGES = {
    "tables": 0.3,
    "images": 0.3,
    "labels": 0.1,
    "shapes": 0.1,
    "points": 0.15,
    "finish": 0.05,
}
CHANNEL_COLOURS = ["FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF", "FFFFFF"]
_FEATURE_NAME = re.compile(r"gene|feature|target|probe|transcript", re.I)
_RGB_NAMES = (
    {"r", "g", "b"},
    {"r", "g", "b", "a"},
    {"red", "green", "blue"},
    {"red", "green", "blue", "alpha"},
)
WINDOW_SIDE = 2048  # channel windows from the first level at most this wide

# Memory of what is read whole or per block (for the job's memory guard), calibrated on measured
# peaks (bench/spatial_memory.py; bench/README.md, ADR 0011).
POINT_BYTES = 100  # compact columns, sort key and order, sorted copies, allocator slack
PARTITION_FACTOR = 5  # one partition read: arrow buffers and the data frame, per byte read
SHAPE_BYTES = 2600  # a geometry in geopandas, its parts and rings, the flat layout
BLOCK_FACTOR = 5  # a source block in flight: itself, rechunked parts, uint64 ids, uint32
ELEMENT_BASE = 256 * 2**20  # per element: dask scheduler, zarr and codec buffers
_ARROW_BYTES = {
    "double": 8, "float": 4, "float64": 8, "float32": 4, "int64": 8, "int32": 4, "uint64": 8,
    "uint32": 4, "int16": 2, "uint16": 2, "int8": 1, "uint8": 1, "bool": 1,
}  # fmt: skip
STRING_BYTES = 16  # a string value as read (arrow): measured close to an integer's cost


def safe_name(name: str) -> str:
    """Element names in the store: `[A-Za-z0-9._-]`, others replaced by `_`, never starting with
    a dot (SPEC §2)."""
    out = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"
    return "_" + out if out.startswith(".") else out


def unique_names(names: list[str], taken: set[str] | None = None) -> dict[str, str]:
    """Source name → store name; names that collide after `safe_name` get `_2`, `_3`, …"""
    used = set(taken or ())
    out: dict[str, str] = {}
    for n in names:
        base = safe_name(n)
        cand, k = base, 2
        while cand in used:
            cand, k = f"{base}_{k}", k + 1
        used.add(cand)
        out[n] = cand
    return out


def _column_bytes(arrow: str) -> int:
    t = arrow.lower()
    if t.startswith("dictionary"):
        return 4
    return _ARROW_BYTES.get(t, STRING_BYTES)


def _points_read_bytes(cols: dict[str, str], keys: list[str]) -> int:
    """Bytes per point of the columns the converter reads (x, y, z, the gene, the cell link:
    the first table instance key the points carry, as the converter links)."""
    per = sum(_column_bytes(cols[c]) for c in ("x", "y", "z") if c in cols)
    feats = [t for c, t in cols.items() if c not in keys and t.lower().startswith("dictionary")]
    per += 4 if feats else STRING_BYTES
    link = next((k for k in keys if k in cols), None)
    return per + (_column_bytes(cols[link]) if link else 0)


def _block_bytes(info: Any, threads: int) -> int:
    """Memory of a pyramid written block by block: `threads` source blocks in flight (a source
    stored as one chunk is read whole)."""
    if not info.levels:
        return 0
    lv = info.levels[0]
    item = np.dtype(lv.dtype).itemsize if lv.dtype else 2
    chunk = lv.chunks or lv.shape
    block = int(np.prod(chunk)) * max(item, 1)
    whole = int(np.prod(lv.shape)) * max(item, 1)
    return min(whole, block * threads) * BLOCK_FACTOR


def spatial_memory_estimate(fp: Fingerprint, threads: int = 4) -> int:
    """Memory of a SpatialData conversion: the tables' obs/var, shapes and points read whole,
    and the image and mask blocks in flight."""
    sd = fp.spatialdata
    if sd is None:
        return 0
    total = sum(table_memory_estimate(t) for t in sd.tables.values())
    total += sum(s.n_rows * SHAPE_BYTES for s in sd.shapes.values())
    keys = [r.instance_key for r in sd.table_refs.values() if r.instance_key]
    # elements are converted one after the other: the largest of them counts
    per_element = [0]
    for pt in sd.points.values():
        part_rows = pt.n_rows / max(pt.n_files, 1)
        part = part_rows * PARTITION_FACTOR * _points_read_bytes(pt.columns, keys)
        per_element.append(int(pt.n_rows * POINT_BYTES + part))
    for img in sd.images.values():
        per_element.append(_block_bytes(img, threads))
    for lab in sd.labels.values():
        per_element.append(_block_bytes(lab, threads))
    return total + max(per_element) + ELEMENT_BASE


def spatial_disk_estimate(fp: Fingerprint, x_source: str) -> tuple[int, int]:
    """Bytes of (store, spill files) a SpatialData conversion writes (uncompressed bound for
    images and masks, so a margin)."""
    sd = fp.spatialdata
    if sd is None:
        return 0, 0
    store = spill = 0
    for t in sd.tables.values():
        try:
            st, sp = disk_need(_matrix_info(t, x_source))
        except ConversionError:
            st, sp = disk_need(_matrix_info(t, "X"))
        store, spill = store + st, max(spill, sp)
    for el in (*sd.images.values(), *sd.labels.values()):
        if el.levels:
            lv = el.levels[0]
            item = 4 if el in sd.labels.values() else np.dtype(lv.dtype or "uint16").itemsize
            store += int(np.prod(lv.shape) * item * 1.34)
    store += sum(pt.n_rows * 24 for pt in sd.points.values())
    store += sum(s.n_rows * 400 for s in sd.shapes.values())
    return store, spill


def _affines(element: Any) -> dict[str, list[list[float]]]:
    """Each coordinate system → 3×3 affine from the element's intrinsic x, y."""
    from spatialdata.transformations import get_transformation

    out: dict[str, list[list[float]]] = {}
    for cs, t in get_transformation(element, get_all=True).items():
        m = np.asarray(t.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y")))
        out[str(cs)] = [[float(v) for v in row] for row in m]
    return out


def _level0(element: Any) -> Any:
    """The full-resolution DataArray of an image or labels element (single or multiscale)."""
    if hasattr(element, "children") and element.children:
        node = element["scale0"]
        ds = node.ds if hasattr(node, "ds") else node
        return next(iter(ds.data_vars.values()))
    return element


def _region(attrs: dict[str, Any]) -> dict[str, Any]:
    """A table's region attributes; `region` is one name or an array of names (several regions)."""
    region = attrs.get("region")
    if region is None:
        names: list[str] = []
    elif isinstance(region, str):
        names = [region]
    else:
        names = [str(r) for r in np.atleast_1d(np.asarray(region, dtype=object))]
    return {
        "region": names,
        "region_key": attrs.get("region_key"),
        "instance_key": attrs.get("instance_key"),
    }


def _rows_of_regions(obs: pd.DataFrame, region: dict[str, Any], elements: list[str]) -> np.ndarray:
    """Table rows annotating the given source elements (all rows for a one-region table)."""
    rows = np.arange(len(obs))
    key = region.get("region_key")
    if key and key in obs and len(region.get("region", [])) > 1:
        rows = rows[obs[key].astype(str).isin(elements).to_numpy()]
    return rows


def _instance_rows(
    obs: pd.DataFrame, instance_key: str, region: dict[str, Any], elements: list[str]
) -> dict[Any, int]:
    """Table rows of the instances of the source `elements`: instance id → row."""
    rows = _rows_of_regions(obs, region, elements)
    ids = obs[instance_key].to_numpy()[rows]
    return {_plain(v): int(r) for v, r in zip(ids, rows, strict=True)}


def _plain(v: Any) -> Any:
    """Instance ids as plain Python values, so 7, np.int64(7) and "7"-free ints compare."""
    return v.item() if isinstance(v, np.generic) else v


class _Ticks(Callback):
    """Progress of a dask computation (`tick(fraction)`, at most twice a second). Callbacks run
    in the calling thread, so a cancel raised by `tick` stops the computation."""

    def __init__(self, tick: Any) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.tick = tick
        self.total = 1
        self.last = 0.0

    def _start_state(self, dsk: Any, state: Any) -> None:
        self.total = max(1, sum(len(state[k]) for k in ("ready", "waiting", "running")))

    def _posttask(self, key: Any, result: Any, dsk: Any, state: Any, worker_id: Any) -> None:
        now = time.monotonic()
        if now - self.last >= 0.5:
            self.last = now
            self.tick(min(1.0, len(state["finished"]) / self.total))


def _sub(tick: Any, lo: float, hi: float) -> Any:
    """A tick for the part lo..hi of a larger step."""
    return (lambda f: tick(lo + (hi - lo) * f)) if tick else None


# ---------------------------------------------------------------- images and labels
def write_pyramid_lazy(
    group: zarr.Group, level0: Any, nearest: bool, tick: Any = None
) -> list[list[int]]:
    """A pyramid written tile by tile from a dask array (c, y, x) or (y, x): level 0 as it is,
    each next level from the one just written (2×2 mean, or every second pixel). Levels larger
    than IMAGE_SHARD_ABOVE px are sharded (1 × 4096 × 4096); each task writes whole shards."""
    lead = (1,) * (level0.ndim - 2)
    levels = pyramid_levels((level0.shape[-2], level0.shape[-1]))
    # work per level ~ its pixels: level 0 is about 3/4 of the whole
    weights = np.array([h * w for h, w in levels], float)
    bounds_ = np.concatenate([[0], np.cumsum(weights)]) / weights.sum()
    shapes: list[list[int]] = []
    current = level0
    prev: zarr.Array[Any] | None = None
    for i, (h, w) in enumerate(levels):
        if prev is not None:
            src = da.from_zarr(prev).rechunk((*lead, 2 * TILE, 2 * TILE))  # type: ignore[no-untyped-call]
            if nearest:
                current = src[..., 0::2, 0::2]
            else:
                halved = tuple(tuple((c + 1) // 2 for c in cs) for cs in src.chunks[-2:])
                current = src.map_blocks(
                    downsample_mean, chunks=(*src.chunks[:-2], *halved), dtype=src.dtype
                )
        chunks = (*lead, min(TILE, h), min(TILE, w))
        shards = (
            (*lead, *(IMAGE_SHARD if v >= TILE else v for v in (h, w)))
            if max(h, w) > IMAGE_SHARD_ABOVE
            else None
        )
        a = group.create_array(
            str(i),
            shape=(*level0.shape[:-2], h, w),
            dtype=level0.dtype,
            chunks=chunks,
            shards=shards,
            compressors=codecs(),
            overwrite=True,
            config={"write_empty_chunks": True},  # no 404s for empty tiles in the browser
        )
        with _Ticks(_sub(tick, bounds_[i], bounds_[i + 1])) if tick else nullcontext():
            da.store(current.rechunk(shards or chunks), a, lock=False)  # type: ignore[arg-type]
        shapes.append([*level0.shape[:-2], h, w])
        prev = a
    return shapes


def _window(a: np.ndarray) -> tuple[float, float]:
    """0.5th–99.5th percentile of a channel."""
    v = a[np.isfinite(a)] if a.dtype.kind == "f" else a.ravel()
    if not v.size:
        return (0.0, 1.0)
    lo, hi = np.percentile(v, [0.5, 99.5])
    return (float(lo), float(hi) if hi > lo else float(lo) + 1.0)


def _dask(arr: Any) -> Any:
    return arr.data if isinstance(arr.data, da.Array) else da.from_array(np.asarray(arr.data))  # type: ignore[no-untyped-call]


def _write_image(
    store: zarr.Group, name: str, element: Any, warnings: list[str], tick: Any = None
) -> ImageEntry | None:
    log = get_logger("dbx.convert")
    arr = _level0(element)
    dims = [str(d) for d in arr.dims]
    if dims != ["c", "y", "x"]:
        warnings.append("image_not_cyx_skipped")
        log.warning("image_skipped", element=name, dims=dims)
        return None
    data = _dask(arr)
    g = store.require_group("images").require_group(name)
    levels = write_pyramid_lazy(g, data, nearest=False, tick=tick)
    # channel windows from a level small enough to read whole but not averaged too far
    k = next(i for i, lv in enumerate(levels) if max(lv[-2:]) <= WINDOW_SIDE)
    sample = np.asarray(g[str(k)][...])  # type: ignore[index]
    names = [str(c) for c in arr.coords["c"].values] if "c" in arr.coords else []
    n_c = int(arr.shape[0])
    names = names if len(names) == n_c else [f"ch{i}" for i in range(n_c)]
    dt = np.dtype(arr.dtype)
    # true colour only when the channels say so (3 uint8 channels are often stains)
    rgb = dt == np.uint8 and {n.lower() for n in names} in _RGB_NAMES
    full = (float(np.iinfo(dt).min), float(np.iinfo(dt).max)) if dt.kind in "iu" else None
    channels: list[ChannelInfo] = []
    omero: list[dict[str, Any]] = []
    for i in range(n_c):
        win = _window(sample[i])
        lo, hi = full or (float(np.nanmin(sample[i])), float(np.nanmax(sample[i])))
        colour = CHANNEL_COLOURS[i % len(CHANNEL_COLOURS)]
        channels.append(ChannelInfo(name=names[i], window=win, color=f"#{colour.lower()}"))
        omero.append(
            {
                "label": names[i],
                "color": colour,
                "active": i < 3,
                "window": {"start": win[0], "end": win[1], "min": lo, "max": hi},
            }
        )
    g.attrs.update(ome_multiscale_attrs(name, ["c", "y", "x"], len(levels), omero))
    return ImageEntry(
        path=f"images/{name}",
        axes=["c", "y", "x"],
        dtype=str(dt),
        levels=levels,
        channels=channels,
        rgb=bool(rgb),
        transforms=_affines(element),
    )


def _block_ids(b: np.ndarray) -> tuple[np.ndarray, bool]:
    """The label IDs of one block, and whether it has negative values (background)."""
    return np.unique(label_ids(b)), bool(b.dtype.kind == "i" and (b < 0).any())


def _write_labels(
    store: zarr.Group,
    name: str,
    source: str,
    element: Any,
    table: tuple[str, pd.DataFrame, dict[str, Any]] | None,
    source_image: str | None,
    warnings: list[str],
    extra: dict[str, Any],
    tick: Any = None,
) -> LabelsEntry | None:
    """A mask, block by block: the IDs are collected per block, then each block is relabelled
    (when IDs are sparse or ≥ 2³²) or cast to uint32 as level 0 is written."""
    arr = _level0(element)
    if [str(d) for d in arr.dims] != ["y", "x"]:
        warnings.append("labels_not_yx_skipped")
        get_logger("dbx.convert").warning("labels_skipped", element=name, dims=list(arr.dims))
        return None
    data = _dask(arr)
    with _Ticks(_sub(tick, 0, 0.3)) if tick else nullcontext():
        parts = dask_compute(*[delayed(_block_ids)(b) for b in data.to_delayed().ravel()])  # type: ignore[no-untyped-call]
    if any(neg for _, neg in parts):
        warnings.append("labels_negative_as_background")
    ids = np.unique(np.concatenate([u for u, _ in parts])) if parts else np.zeros(0, np.uint64)
    ids = ids[ids != 0]
    n_labels, max_label = int(ids.size), int(ids[-1]) if ids.size else 0
    relabelled = needs_relabel(max_label, n_labels)
    original = (
        np.concatenate([np.zeros(1, np.uint64), ids.astype(np.uint64)]) if relabelled else None
    )
    if relabelled:
        max_label = n_labels
    lab = data.map_blocks(relabel_block, original, dtype=np.uint32)
    g = store.require_group("labels").require_group(name)
    levels = write_pyramid_lazy(g, lab, nearest=True, tick=_sub(tick, 0.3, 0.95))
    extra.update(shape=levels[0], n_labels=n_labels, relabelled=relabelled)
    attrs = ome_multiscale_attrs(name, ["y", "x"], len(levels))
    attrs["ome"].update(image_label_attrs(source_image))
    g.attrs.update(attrs)
    aux = store.require_group("aux").require_group("labels").require_group(name)
    if original is not None:
        array(aux, "original_id", original)
    table_name = lookup = None
    if table is not None:
        tname, obs, region = table
        rows_of = _instance_rows(obs, region["instance_key"], region, [source])
        lk = np.full(max_label + 1, -1, dtype=np.int32)
        keys = np.array(list(rows_of.keys()), dtype=object)
        rows = np.array(list(rows_of.values()), dtype=np.int64)
        try:
            vals = pd.to_numeric(pd.Series(keys), errors="coerce").to_numpy(np.float64)
        except (TypeError, ValueError):
            vals = np.full(len(keys), np.nan)
        ok = np.isfinite(vals) & (vals > 0)
        v = vals[ok].astype(np.uint64)
        r = rows[ok]
        if original is not None:
            k = np.searchsorted(original, v)
            hit = (k < len(original)) & (original[np.minimum(k, len(original) - 1)] == v)
            lk[k[hit]] = r[hit]
        else:
            hit = v <= max_label
            lk[v[hit].astype(np.int64)] = r[hit]
        array(aux, "row", lk)
        table_name, lookup = tname, f"aux/labels/{name}/row"
        extra.update(linked_fraction=round(float((lk[1:] >= 0).mean()) if max_label else 0.0, 4))
    return LabelsEntry(
        path=f"labels/{name}",
        levels=levels,
        max_label=max_label,
        n_labels=n_labels,
        relabelled=relabelled,
        table=table_name,
        lookup=lookup,
        transforms=_affines(element),
    )


# ---------------------------------------------------------------- shapes
def _polygon_rings(
    geoms: np.ndarray, rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, int]]:
    """Outer rings of the polygon parts of each geometry (vectorised with shapely 2).

    Invalid polygons (self-intersecting, …) are repaired with `make_valid` and keep their
    polygon parts; rows that are not polygons are left out. Returns (vertices, ring starts,
    ring rows, counts of repaired / non-polygon / with-holes rows)."""
    import shapely

    tid = shapely.get_type_id(geoms)  # -1 for missing
    poly = (tid == 3) | (tid == 6)
    counts = {"non_polygon": int(((tid >= 0) & ~poly).sum()), "repaired": 0, "holes": 0}
    sel = np.flatnonzero(poly)
    g = geoms[sel]
    bad = ~shapely.is_valid(g)
    if bad.any():
        g = g.copy()
        g[bad] = shapely.make_valid(g[bad])
        counts["repaired"] = int(bad.sum())
    parts, idx = shapely.get_parts(g, return_index=True)
    for _ in range(2):  # repaired geometries can be collections holding multipolygons
        multi = np.isin(shapely.get_type_id(parts), (4, 5, 6, 7))
        if not multi.any():
            break
        sub, j = shapely.get_parts(parts[multi], return_index=True)
        parts = np.concatenate([parts[~multi], sub])
        idx = np.concatenate([idx[~multi], idx[multi][j]])
    keep = (shapely.get_type_id(parts) == 3) & ~shapely.is_empty(parts)
    parts, idx = parts[keep], idx[keep]
    holes = shapely.get_num_interior_rings(parts) > 0
    counts["holes"] = int(holes.sum())
    rings = shapely.get_exterior_ring(parts)
    coords, ring_idx = shapely.get_coordinates(rings, return_index=True)
    lengths = np.bincount(ring_idx, minlength=len(rings))
    starts = np.concatenate([[0], np.cumsum(lengths)]).astype(np.int64)
    return coords.astype(np.float32), starts, rows[sel][idx], counts


def _write_shapes(
    store: zarr.Group,
    name: str,
    source: str,
    gdf: Any,
    table: tuple[str, pd.DataFrame, dict[str, Any]] | None,
    warnings: list[str],
    extra: dict[str, Any],
) -> ShapesEntry | None:
    rows_of: dict[Any, int] | None = None
    if table is not None:
        tname, obs, region = table
        rows_of = _instance_rows(obs, region["instance_key"], region, [source])
    index = [_plain(v) for v in gdf.index]
    if rows_of is not None:
        rows = np.array([rows_of.get(v, -1) for v in index], dtype=np.int64)
        if (rows < 0).any():
            warnings.append("shapes_without_table_row")
        extra.update(linked_fraction=round(float((rows >= 0).mean()) if len(rows) else 0.0, 4))
    else:
        rows = np.arange(len(gdf), dtype=np.int64)
    geoms = np.asarray(gdf.geometry.values, dtype=object)
    import shapely

    tid = shapely.get_type_id(geoms)
    g = store.require_group("shapes").require_group(name)
    transforms = _affines(gdf)
    if len(tid) and (tid[tid >= 0] == 0).all():
        if "radius" not in gdf.columns:
            warnings.append("points_shapes_without_radius_skipped")
            return None
        xy = shapely.get_coordinates(geoms)
        radius = gdf["radius"].to_numpy(np.float32)
        ok = np.isfinite(xy).all(1) & np.isfinite(radius) & (tid >= 0)
        if not ok.all():
            warnings.append("shapes_non_finite_dropped")
            extra.update(dropped=int((~ok).sum()))
        center = xy[ok].astype(np.float32)
        sharded(g, "center", center, POLY_BLOCK)
        sharded(g, "radius", radius[ok], POLY_BLOCK)
        sharded(g, "row", rows[ok].astype(np.int32), POLY_BLOCK)
        return ShapesEntry(
            path=f"shapes/{name}",
            kind="circles",
            n=int(ok.sum()),
            table=table[0] if table else None,
            transforms=transforms,
        )
    verts, starts, ring_rows, counts = _polygon_rings(geoms, rows)
    if counts["non_polygon"]:
        warnings.append("shapes_non_polygon_dropped")
    if counts["repaired"]:
        warnings.append("shapes_invalid_repaired")
    if counts["holes"]:
        warnings.append("polygon_holes_dropped")
    lay = polygon_layout_flat(verts, starts, ring_rows)
    if lay.dropped:
        warnings.append("shapes_degenerate_dropped")
    extra.update(
        n_source=len(gdf),
        non_polygon=counts["non_polygon"],
        repaired=counts["repaired"],
        degenerate=lay.dropped,
        n_vertices=int(lay.vertices.shape[0]),
    )
    if lay.row.size == 0:
        warnings.append("shapes_geometry_skipped")
        return None
    write_polygons(g, lay)
    return ShapesEntry(
        path=f"shapes/{name}",
        kind="polygons",
        n=int(lay.row.size),
        n_vertices=int(lay.vertices.shape[0]),
        table=table[0] if table else None,
        transforms=transforms,
    )


# ---------------------------------------------------------------- points
def _is_text(dtype: Any) -> bool:
    return pd.api.types.is_object_dtype(dtype) or isinstance(dtype, pd.StringDtype)


def feature_column(
    df: pd.DataFrame, declared: str | None, exclude: set[str], chosen: str | None = None
) -> tuple[str, bool]:
    """The column holding gene names: the one chosen for the conversion, the declared
    `feature_key`, else the one categorical column (instance ids excluded), else the categorical
    one named like genes, else the text column named like genes."""
    for c in (chosen, declared):
        if c and c in df.columns:
            return c, c == chosen
    if chosen:
        raise ConversionError("feature_key_missing", "the chosen gene column is not in the points")
    cols = [str(c) for c in df.columns if str(c) not in exclude and str(c) not in ("x", "y", "z")]
    cats = [c for c in cols if isinstance(df[c].dtype, pd.CategoricalDtype)]
    if len(cats) == 1:
        return cats[0], True
    for group in (cats, [c for c in cols if _is_text(df[c].dtype)]):
        named = [c for c in group if _FEATURE_NAME.search(c)]
        if len(named) == 1:
            return named[0], True
        if len(named) > 1:
            break
    raise ConversionError(
        "feature_key_ambiguous",
        f"cannot tell which column holds the genes ({len(cats)} categorical candidates); "
        "choose it with the conversion's feature_key option",
    )


def _cell_link(
    columns: list[str],
    attrs: dict[str, Any],
    systems: set[str],
    tables: dict[str, tuple[pd.DataFrame, dict[str, Any]]],
    systems_of: dict[str, set[str]],
    warnings: list[str],
) -> tuple[str, str, dict[Any, int]] | None:
    """The table (and its instance column) the points' cell ids refer to, with id → row.

    For a table over several regions only the regions sharing a coordinate system with the
    points count; if the cell ids still repeat across those regions the link is ambiguous and
    left out (a warning), never guessed."""
    log = get_logger("dbx.convert")
    found: list[tuple[int, str, str, dict[Any, int]]] = []
    for tname, (obs, region) in tables.items():
        key = attrs.get("instance_key") or region.get("instance_key")
        if not key or key not in columns or key not in obs.columns:
            continue
        regs = region.get("region", [])
        near = [r for r in regs if systems_of.get(r, set()) & systems]
        use = near or regs
        rows = _rows_of_regions(obs, region, use)
        ids = obs[key].to_numpy()[rows]
        if len(use) > 1 and pd.Series(ids).duplicated().any():
            warnings.append("points_cell_link_ambiguous")
            log.warning("points_cell_link_ambiguous", table=tname, n_regions=len(use))
            continue
        rows_of = {_plain(v): int(r) for v, r in zip(ids, rows, strict=True)}
        found.append((0 if near else 1, tname, key, rows_of))
    if not found:
        return None
    found.sort(key=lambda f: f[0])
    _, tname, key, rows_of = found[0]
    return tname, key, rows_of


@dataclasses.dataclass
class _Points:
    xy: np.ndarray
    z: np.ndarray | None
    codes: np.ndarray
    cell: np.ndarray | None
    features: list[str]
    n_source: int = 0
    partitions: int = 0
    no_feature: int = 0
    non_finite: int = 0


def _read_points(
    ddf: Any,
    fkey: str,
    cell_key: str | None,
    rows_of: dict[Any, int] | None,
    meta: pd.DataFrame,
    tick: Any = None,
) -> _Points:
    """The needed columns, one partition at a time, as compact arrays: float32 x, y (z), int32
    gene codes and table rows. Only one partition is held as a data frame at any time (arrow
    and pandas keep several copies of what they read), and rows without a gene or with
    non-finite coordinates are dropped as each partition is read."""
    import pyarrow as pa

    glob: dict[str, int] = {}
    xs: list[np.ndarray] = []
    zs: list[np.ndarray] = []
    cs: list[np.ndarray] = []
    cells: list[np.ndarray] = []
    out = _Points(np.zeros((0, 2), np.float32), None, np.zeros(0, np.int32), None, [])
    parts = ddf.to_delayed()
    out.partitions = len(parts)
    for i, d in enumerate(parts):
        df: pd.DataFrame = d.compute()
        out.n_source += len(df)
        f = df[fkey]
        if isinstance(f.dtype, pd.CategoricalDtype):
            cats, c = f.cat.categories, f.cat.codes.to_numpy()
        else:
            c, cats = pd.factorize(f, use_na_sentinel=True)
        to_glob = np.array([glob.setdefault(str(v), len(glob)) for v in cats], np.int32)
        code = np.where(c >= 0, to_glob[np.maximum(c, 0)] if len(to_glob) else -1, -1).astype(
            np.int32
        )
        xy = np.c_[df["x"].to_numpy(np.float32), df["y"].to_numpy(np.float32)]
        keep = code >= 0
        out.no_feature += int((~keep).sum())
        finite = np.isfinite(xy).all(1)
        z = df["z"].to_numpy(np.float32) if "z" in df.columns else None
        if z is not None:
            finite &= ~np.isnan(z)
        out.non_finite += int((keep & ~finite).sum())
        keep &= finite
        xs.append(xy[keep])
        cs.append(code[keep])
        if z is not None:
            zs.append(z[keep])
        if cell_key is not None and rows_of is not None:
            ids = df[cell_key]
            if isinstance(ids.dtype, pd.CategoricalDtype):
                u, inv = ids.cat.categories.to_numpy(), ids.cat.codes.to_numpy()
            else:
                inv, u = pd.factorize(ids, use_na_sentinel=True)
                u = np.asarray(u)
            lut = np.array([rows_of.get(_plain(v), -1) for v in u] + [-1], np.int32)
            cells.append(lut[np.where(inv >= 0, inv, len(u))][keep])
        del df, f, xy, z
        if tick:
            tick((i + 1) / max(len(parts), 1))
    pa.default_memory_pool().release_unused()
    # genes in the dataset's order where it declares one, else sorted
    known = meta[fkey].cat.categories if isinstance(meta[fkey].dtype, pd.CategoricalDtype) else []
    order = [str(v) for v in known if str(v) in glob] or sorted(glob)
    order += sorted(set(glob) - set(order))
    remap = np.empty(max(len(glob), 1), np.int32)
    for k, g in enumerate(order):
        remap[glob[g]] = k
    out.features = order
    out.xy = np.concatenate(xs) if xs else np.zeros((0, 2), np.float32)
    del xs
    out.codes = remap[np.concatenate(cs)] if cs else np.zeros(0, np.int32)
    del cs
    out.z = np.concatenate(zs) if zs else None
    out.cell = np.concatenate(cells) if cells else None
    return out


def _write_points(
    store: zarr.Group,
    name: str,
    ddf: Any,
    tables: dict[str, tuple[pd.DataFrame, dict[str, Any]]],
    systems_of: dict[str, set[str]],
    opts: ConvertOptions,
    warnings: list[str],
    extra: dict[str, Any],
    tick: Any = None,
) -> PointsEntry:
    attrs = dict(ddf.attrs.get("spatialdata_attrs") or {})
    transforms = _affines(ddf)
    meta: pd.DataFrame = ddf._meta
    columns = [str(c) for c in meta.columns]
    link = _cell_link(columns, attrs, set(transforms), tables, systems_of, warnings)
    exclude = {link[1]} if link else set()
    if attrs.get("instance_key"):
        exclude.add(str(attrs["instance_key"]))
    fkey, inferred = feature_column(meta, attrs.get("feature_key"), exclude, opts.feature_key)
    has_z = "z" in columns
    need = ["x", "y", *(["z"] if has_z else []), fkey, *([link[1]] if link else [])]
    need = list(dict.fromkeys(need))
    rd = _read_points(
        ddf[need],
        fkey,
        link[1] if link else None,
        link[2] if link else None,
        meta,
        _sub(tick, 0, 0.4),
    )
    xy, z, codes, cell, features = rd.xy, rd.z, rd.codes, rd.cell, rd.features
    extra.update(
        n_source=rd.n_source,
        partitions=rd.partitions,
        xy_dtype=str(meta["x"].dtype),
        feature_dtype=str(meta[fkey].dtype),
        cell_dtype=str(meta[link[1]].dtype) if link else None,
        dropped=rd.n_source - int(xy.shape[0]),
    )
    if rd.no_feature:
        warnings.append("points_without_feature_dropped")
    if rd.non_finite:
        warnings.append("points_non_finite_dropped")
    if cell is not None:
        linked = float((cell >= 0).mean()) if cell.size else 0.0
        extra.update(linked_fraction=round(linked, 4))
        if linked == 0.0 and cell.size:
            warnings.append("points_cell_link_none")
    lay = points_layout(xy, codes, len(features), z=z, cell=cell, tick=_sub(tick, 0.4, 0.8))
    write_points(
        store.require_group("points").require_group(name), lay, features, _sub(tick, 0.8, 1.0)
    )
    z_planes = None
    if z is not None:
        u = np.unique(z)
        z_planes = [float(v) for v in u] if len(u) <= 256 else None
    return PointsEntry(
        path=f"points/{name}",
        n=int(xy.shape[0]),
        n_features=len(features),
        feature_key=fkey,
        feature_key_inferred=inferred,
        has_cell=cell is not None,
        has_z=z is not None,
        z_range=(float(z.min()), float(z.max())) if z is not None and z.size else None,
        z_planes=z_planes,
        bbox=lay.bbox,
        n_overview=min(int(xy.shape[0]), OVERVIEW_POINTS),
        lod_cap=LOD_CAP,
        table=link[0] if link else None,
        transforms=transforms,
    )


def _units(src_root: Any, systems: list[str]) -> dict[str, Unit]:
    """Units of the coordinate systems as the elements' NGFF metadata declare them (x axis)."""
    found: dict[str, str] = {}

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            out = v.get("output")
            if isinstance(out, dict) and isinstance(out.get("axes"), list):
                for ax in out["axes"]:
                    if isinstance(ax, dict) and ax.get("name") == "x" and ax.get("unit"):
                        found.setdefault(str(out.get("name")), str(ax["unit"]))
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for kind in ("images", "labels", "shapes", "points"):
        if kind in src_root:
            for _, el in src_root[kind].members():
                walk(dict(el.attrs))
    units: dict[str, Unit] = {
        "micrometer": "micrometer",
        "micron": "micrometer",
        "um": "micrometer",
        "µm": "micrometer",
        "pixel": "pixel",
    }
    return {s: units.get(found.get(s, "").lower(), "unknown") for s in systems}


# ---------------------------------------------------------------- the conversion
def convert_spatialdata(
    src: Path,
    fp: Fingerprint,
    cache_dir: Path,
    opts: ConvertOptions,
    progress: Progress,
) -> Result:
    sdi = fp.spatialdata
    if sdi is None or fp.kind != "spatialdata_zarr":
        raise ConversionError("not_spatialdata", "only SpatialData stores are converted here")
    log = get_logger("dbx.convert")
    t0 = time.monotonic()
    version = fp.dataset_id
    stamp = version_stamp(src)
    if dataset_id(src, stamp) != version:
        raise ConversionError("source_changed", "the store changed since it was inspected")
    final = store_dir(cache_dir, version)
    tmp = final.parent / f"{final.name}.tmp-j{opts.job}-{uuid.uuid4().hex[:8]}"
    spill_root = opts.tmp_dir or cache_dir / "tmp"
    work = spill_root / tmp.name
    prog = StageProgress(progress, STAGES)
    warnings: list[str] = []
    check_disk(final.parent, spill_root, *spatial_disk_estimate(fp, opts.x_source), version, log)
    try:
        with dask_config.set(scheduler="threads", num_workers=max(1, opts.threads)):
            return _convert(src, fp, sdi, opts, tmp, final, work, prog, warnings, t0, stamp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _convert(
    src: Path,
    fp: Fingerprint,
    sdi: Any,
    opts: ConvertOptions,
    tmp: Path,
    final: Path,
    work: Path,
    prog: StageProgress,
    warnings: list[str],
    t0: float,
    stamp: Any,
) -> Result:
    import spatialdata

    log = get_logger("dbx.convert")
    version = fp.dataset_id
    tmp.mkdir(parents=True)
    store = zarr.open_group(tmp, mode="w")
    store.attrs["dbx"] = {"contract_version": CONTRACT_VERSION}
    store.require_group("tables")
    prog("tables", 0.0)
    with timed("convert/read_spatialdata", version=version) as extra:
        # tables go through write_table: the reader must not load their matrices
        sdata = spatialdata.read_zarr(src, selection=("images", "labels", "points", "shapes"))
        extra.update(
            images=len(sdata.images),
            labels=len(sdata.labels),
            shapes=len(sdata.shapes),
            points=len(sdata.points),
            tables=len(sdi.tables),
        )
    src_root: Any = zarr.open_group(src, mode="r")
    systems_of: dict[str, set[str]] = {
        n: set(_affines(e)) for kind in (sdata.shapes, sdata.labels) for n, e in kind.items()
    }

    # ---- tables (AnnData path), with what they annotate
    tables: dict[str, TableEntry] = {}
    table_obs: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
    names = list(sdi.tables)
    table_names = unique_names(names)
    for i, tname in enumerate(names):
        troot: Any = src_root["tables"][tname]
        attrs: Any = (
            read_elem(troot["uns"]["spatialdata_attrs"])
            if "spatialdata_attrs" in troot["uns"]
            else {}
        )
        region = _region(dict(attrs))
        safe = table_names[tname]
        inner = StageProgress(
            lambda _s, f, i=i: prog("tables", (i + f) / max(len(names), 1))  # type: ignore[misc]
        )
        topts = opts
        tinfo = sdi.tables[tname]
        if opts.x_source.startswith("layers/") and opts.x_source[7:] not in tinfo.layers:
            # the chosen layer is in another table: this one keeps its X
            topts = dataclasses.replace(opts, x_source="X")
            warnings.append("layer_missing_used_X")
        entry = write_table(troot, tinfo, tmp, safe, topts, work, inner, warnings, version, region)
        obs: pd.DataFrame = read_elem(troot["obs"])
        key = region.get("region_key")
        if len(entry.region) > 1 and key:
            # one panel per region (SPEC §4.6): the region column is the section key
            if key in obs and isinstance(obs[key].dtype, pd.CategoricalDtype):
                entry.section_keys = [str(key)]
            else:
                warnings.append("region_key_not_categorical")
        if safe != tname:
            entry.source_name = tname
        tables[safe] = entry
        table_obs[safe] = (obs, region)

    element_names = unique_names(
        [*sdata.images, *sdata.labels, *sdata.shapes, *sdata.points]
    )  # one namespace, so a store path never collides
    for entry in tables.values():
        if any(r not in element_names for r in entry.region):
            warnings.append("table_region_missing")  # annotates an element the store lacks
        entry.region = [element_names[r] for r in entry.region if r in element_names]

    def annotating(element: str) -> tuple[str, pd.DataFrame, dict[str, Any]] | None:
        for tname, (obs, region) in table_obs.items():
            if element in region["region"] and region.get("instance_key") in obs.columns:
                return tname, obs, region
        return None

    def run(stage: str, items: dict[str, Any], write: Any) -> None:
        """One stage: progress before, during and after each element (cancel acts inside)."""
        n = max(len(items), 1)
        for i, (name, el) in enumerate(items.items()):
            prog(stage, i / n)

            def tick(f: float, i: int = i) -> None:
                prog(stage, (i + f) / n)

            write(name, element_names[name], el, tick)
        prog(stage, 1.0)

    # ---- images
    images: dict[str, ImageEntry] = {}

    def image(name: str, safe: str, el: Any, tick: Any) -> None:
        with timed("convert/image", version=version, element=safe) as extra:
            e = _write_image(store, safe, el, warnings, tick)
            if e is not None:
                extra.update(levels=len(e.levels), shape=e.levels[0], dtype=e.dtype)
                if safe != name:
                    e.source_name = name
                images[safe] = e

    run("images", dict(sdata.images), image)

    # ---- labels
    labels: dict[str, LabelsEntry] = {}

    def image_for(name: str) -> str | None:
        """The image of a mask: the first in the same coordinate systems."""
        mine = systems_of.get(name, set())
        for n, e in images.items():
            if set(e.transforms) & mine:
                return n
        return next(iter(images), None)

    def label(name: str, safe: str, el: Any, tick: Any) -> None:
        with timed("convert/labels", version=version, element=safe) as extra:
            e = _write_labels(
                store, safe, name, el, annotating(name), image_for(name), warnings, extra, tick
            )
            if e is not None:
                if safe != name:
                    e.source_name = name
                labels[safe] = e

    run("labels", dict(sdata.labels), label)
    if labels:
        store.require_group("labels").attrs.update(labels_container_attrs(list(labels)))

    # ---- shapes
    shapes: dict[str, ShapesEntry] = {}

    def shape(name: str, safe: str, gdf: Any, tick: Any) -> None:
        with timed("convert/shapes", version=version, element=safe) as extra:
            e = _write_shapes(store, safe, name, gdf, annotating(name), warnings, extra)
            if e is not None:
                extra.update(n=e.n, kind=e.kind)
                if safe != name:
                    e.source_name = name
                shapes[safe] = e

    run("shapes", dict(sdata.shapes), shape)

    # ---- points
    points: dict[str, PointsEntry] = {}

    def point(name: str, safe: str, ddf: Any, tick: Any) -> None:
        with timed("convert/points", version=version, element=safe) as extra:
            e = _write_points(store, safe, ddf, table_obs, systems_of, opts, warnings, extra, tick)
            extra.update(n=e.n, n_features=e.n_features)
            if safe != name:
                e.source_name = name
            points[safe] = e

    run("points", dict(sdata.points), point)

    # ---- manifest, consolidation, validation, publish
    systems = sorted(
        {
            cs
            for kind in (images, labels, shapes, points)
            for e in kind.values()
            for cs in e.transforms
        }
    )
    systems.sort(key=lambda s: s != "global")
    consolidate(tmp)
    manifest = Manifest(
        dataset_id=fp.dataset_id,
        source=SourceInfo(
            path=src.name,
            kind="spatialdata_zarr",
            size_bytes=int(fp.size_bytes or 0),
            mtime=float(fp.mtime or 0.0),
            fingerprint_version=fp.fingerprint_version,
        ),
        converter=ConverterInfo(
            dbx_version=dbx.__version__,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_s=round(time.monotonic() - t0, 1),
            peak_rss_gb=peak_rss_gb(),
            warnings=sorted(set(warnings)),
        ),
        coordinate_systems=systems,
        units=_units(src_root, systems),
        tables=tables,
        images=images,
        labels=labels,
        shapes=shapes,
        points=points,
    )
    write_manifest(tmp, manifest)
    prog("finish", 0.3)
    with timed("convert/validate", version=version) as extra:
        report = validate_store(tmp, deep=True)
        extra.update(errors=len(report.errors), warnings=len(report.warnings))
    if not report.ok:
        log_invalid(log, version, report)
    if version_stamp(src) != stamp:
        raise ConversionError("source_changed", "the store changed during the conversion")
    prog("finish", 0.9)
    _publish(tmp, final)
    prog("finish", 1.0)
    return Result(final, manifest, warnings)
