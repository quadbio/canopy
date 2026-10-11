"""Low-level writers for contract arrays (shared by the example store and the converter)."""

from __future__ import annotations

import json
import shutil
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from zarr.codecs import ZstdCodec

from dbx.contract.models import (
    CONTRACT,
    LOD_CAP,
    OVERVIEW_POINTS,
    POINT_CHUNK,
    POLY_BLOCK,
    ROW_CHUNK,
    SHARD_CHUNKS,
    TILE,
    VERTEX_CHUNK,
    ZSTD_LEVEL,
)

ANNDATA_ARRAY = {"encoding-type": "array", "encoding-version": "0.2.0"}
ANNDATA_DICT = {"encoding-type": "dict", "encoding-version": "0.1.0"}


def consolidate(path: Path) -> None:
    """Consolidate zarr metadata (what anndata and spatialdata expect), without the v3 warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Consolidated metadata", category=UserWarning)
        zarr.consolidate_metadata(path)


def is_store(path: Path) -> bool:
    """True if `path` holds a dbx store (its manifest names the contract)."""
    try:
        return bool(json.loads((path / "manifest.json").read_text()).get("contract") == CONTRACT)
    except (OSError, ValueError):
        return False


def prepare_output_dir(out: Path, *, force: bool = False) -> None:
    """Clear `out` for a new store. Only an empty folder or an existing dbx store is replaced;
    anything else is refused unless `force`, so a mistyped path never deletes user data."""
    if not out.exists():
        return
    if not out.is_dir():
        raise FileExistsError(f"{out} exists and is not a folder")
    if force or is_store(out) or not any(out.iterdir()):
        shutil.rmtree(out)
        return
    raise FileExistsError(f"{out} exists and is not a dbx store; refusing to replace it")


def codecs() -> list[Any]:
    return [ZstdCodec(level=ZSTD_LEVEL)]


def array(
    group: zarr.Group,
    name: str,
    data: np.ndarray,
    chunks: tuple[int, ...] | None = None,
    attrs: dict[str, Any] | None = None,
    shards: tuple[int, ...] | None = None,
) -> zarr.Array[Any]:
    """Write a numeric array with contract defaults (zstd, row chunks)."""
    if chunks is None:
        chunks = (min(ROW_CHUNK, max(1, data.shape[0])), *data.shape[1:])
    a = group.create_array(
        name,
        shape=data.shape,
        dtype=data.dtype,
        chunks=chunks,
        shards=shards,
        compressors=codecs(),
        overwrite=True,
    )
    if data.size:
        a[...] = data
    if attrs:
        a.attrs.update(attrs)
    return a


def sharded(group: zarr.Group, name: str, data: np.ndarray, rows: int) -> zarr.Array[Any]:
    """Row-chunked array (`rows` per chunk) grouped into shards of SHARD_CHUNKS chunks."""
    rest = tuple(data.shape[1:])
    return array(group, name, data, chunks=(rows, *rest), shards=(rows * SHARD_CHUNKS, *rest))


def index_dtype(n: int) -> type[np.unsignedinteger[Any]]:
    """uint32 for offsets/starts while they fit, else uint64 (SPEC §1.6)."""
    return np.uint32 if n < 2**32 else np.uint64


def anndata_dict_group(parent: zarr.Group, name: str) -> zarr.Group:
    g = parent.require_group(name)
    g.attrs.update(ANNDATA_DICT)
    return g


def csc_x(
    table: zarr.Group,
    data: np.ndarray,
    indices: np.ndarray,
    indptr: np.ndarray,
    shape: tuple[int, int],
    x_chunk: int,
    indptr_chunk: int,
) -> None:
    """Write X as anndata csc_matrix with contract dtypes and chunks."""
    if "X" in table:
        del table["X"]
    x = table.create_group("X")
    x.attrs.update(
        {
            "encoding-type": "csc_matrix",
            "encoding-version": "0.1.0",
            "shape": [int(shape[0]), int(shape[1])],
        }
    )
    idx_dtype = np.int32 if shape[0] < 2**31 else np.int64
    array(x, "data", data.astype(np.float32, copy=False), chunks=(x_chunk,))
    array(x, "indices", indices.astype(idx_dtype, copy=False), chunks=(x_chunk,))
    array(x, "indptr", indptr.astype(np.int64, copy=False), chunks=(indptr_chunk,))


def pyramid_levels(shape_yx: tuple[int, int]) -> list[tuple[int, int]]:
    """Level shapes: halve until both sides are ≤ TILE."""
    levels = [shape_yx]
    while max(levels[-1]) > TILE:
        y, x = levels[-1]
        levels.append(((y + 1) // 2, (x + 1) // 2))
    return levels


def downsample_mean(a: np.ndarray) -> np.ndarray:
    """Halve the last two axes by 2x2 mean (odd edges padded by edge values)."""
    y, x = a.shape[-2:]
    pad = [(0, 0)] * (a.ndim - 2) + [(0, y % 2), (0, x % 2)]
    p = np.pad(a, pad, mode="edge").astype(np.float64)
    out = (p[..., 0::2, 0::2] + p[..., 1::2, 0::2] + p[..., 0::2, 1::2] + p[..., 1::2, 1::2]) / 4
    if np.issubdtype(a.dtype, np.integer):
        out = np.rint(out)
    return out.astype(a.dtype)


def downsample_nearest(a: np.ndarray) -> np.ndarray:
    return a[..., 0::2, 0::2]


def ome_multiscale_attrs(
    name: str, axes: list[str], n_levels: int, channels: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    axis_meta = [{"name": a, "type": "channel" if a == "c" else "space"} for a in axes]
    datasets = []
    for i in range(n_levels):
        s = float(2**i)
        scale = [1.0 if a == "c" else s for a in axes]
        datasets.append(
            {"path": str(i), "coordinateTransformations": [{"type": "scale", "scale": scale}]}
        )
    ome: dict[str, Any] = {
        "version": "0.5",
        "multiscales": [{"name": name, "axes": axis_meta, "datasets": datasets}],
    }
    if channels is not None:
        ome["omero"] = {"channels": channels}
    return {"ome": ome}


def write_pyramid(group: zarr.Group, level0: np.ndarray, nearest: bool) -> list[list[int]]:
    """Write level 0..n under group as '0', '1', … with TILE chunks; returns level shapes."""
    shapes: list[list[int]] = []
    current = level0
    for i, _ in enumerate(pyramid_levels((level0.shape[-2], level0.shape[-1]))):
        if i > 0:
            current = downsample_nearest(current) if nearest else downsample_mean(current)
        lead = (1,) * (current.ndim - 2)
        chunks = (*lead, min(TILE, current.shape[-2]), min(TILE, current.shape[-1]))
        array(group, str(i), current, chunks=chunks)
        shapes.append(list(current.shape))
    return shapes


def morton_order(
    x: np.ndarray, y: np.ndarray, bbox: tuple[float, float, float, float]
) -> np.ndarray:
    """Z-order codes of points quantised to 16 bits per axis within bbox."""
    x0, y0, x1, y1 = bbox
    qx = np.clip((x - x0) / max(x1 - x0, 1e-12) * 65535, 0, 65535).astype(np.uint64)
    qy = np.clip((y - y0) / max(y1 - y0, 1e-12) * 65535, 0, 65535).astype(np.uint64)

    def spread(v: np.ndarray) -> np.ndarray:
        v = (v | (v << np.uint64(8))) & np.uint64(0x00FF00FF)
        v = (v | (v << np.uint64(4))) & np.uint64(0x0F0F0F0F)
        v = (v | (v << np.uint64(2))) & np.uint64(0x33333333)
        out: np.ndarray = (v | (v << np.uint64(1))) & np.uint64(0x55555555)
        return out

    codes: np.ndarray = spread(qx) | (spread(qy) << np.uint64(1))
    return codes


def row_boxes(xy: np.ndarray, rows: int) -> np.ndarray:
    """Bounding box (xmin, ymin, xmax, ymax) of each block of `rows` consecutive points."""
    n = xy.shape[0]
    starts = np.arange(0, n, rows)
    if n == 0:
        return np.zeros((0, 4), np.float32)
    lo = np.minimum.reduceat(xy, starts, axis=0)
    hi = np.maximum.reduceat(xy, starts, axis=0)
    return np.concatenate([lo, hi], axis=1).astype(np.float32)


def bounds(xy: np.ndarray) -> tuple[float, float, float, float]:
    if xy.shape[0] == 0:
        return (0.0, 0.0, 0.0, 0.0)
    lo, hi = xy.min(0), xy.max(0)
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


# ---------------------------------------------------------------- polygons (SPEC §7)
def signed_area(ring: np.ndarray) -> float:
    """Shoelace area of an open or closed ring; positive = counter-clockwise in x, y."""
    x, y = ring[:, 0].astype(np.float64), ring[:, 1].astype(np.float64)
    return float(0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def normalise_ring(ring: np.ndarray) -> np.ndarray:
    """Closed (last = first), counter-clockwise copy of an outer ring."""
    r = np.asarray(ring, dtype=np.float32)
    if len(r) > 1 and np.array_equal(r[0], r[-1]):
        r = r[:-1]
    if signed_area(r) < 0:
        r = r[::-1]
    return np.concatenate([r, r[:1]])


def ring_centroid(ring: np.ndarray) -> tuple[float, float]:
    """Area centroid of a closed ring (vertex mean for degenerate rings)."""
    x, y = ring[:-1, 0].astype(np.float64), ring[:-1, 1].astype(np.float64)
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    cross = x * yn - xn * y
    a = cross.sum() / 2
    if abs(a) < 1e-12:
        return float(x.mean()), float(y.mean())
    return float(((x + xn) * cross).sum() / (6 * a)), float(((y + yn) * cross).sum() / (6 * a))


@dataclass
class PolygonLayout:
    vertices: np.ndarray
    starts: np.ndarray
    row: np.ndarray
    bbox: np.ndarray
    centroid: np.ndarray
    block_bbox: np.ndarray


def polygon_layout(rings: list[np.ndarray], rows: np.ndarray) -> PolygonLayout:
    """Normalise outer rings and sort polygons by the Morton code of their centroid."""
    closed = [normalise_ring(r) for r in rings]
    cent = np.array([ring_centroid(r) for r in closed], np.float32).reshape(-1, 2)
    order = np.argsort(morton_order(cent[:, 0], cent[:, 1], bounds(cent)), kind="stable")
    closed = [closed[i] for i in order]
    lengths = np.array([len(r) for r in closed], np.int64)
    starts = np.concatenate([[0], np.cumsum(lengths)])
    verts = np.concatenate(closed) if closed else np.zeros((0, 2), np.float32)
    bbox = np.array([[*r.min(0), *r.max(0)] for r in closed], np.float32).reshape(-1, 4)
    block = np.arange(0, len(closed), POLY_BLOCK)
    block_bbox = (
        np.concatenate(
            [np.minimum.reduceat(bbox[:, :2], block), np.maximum.reduceat(bbox[:, 2:], block)],
            axis=1,
        )
        if len(closed)
        else np.zeros((0, 4), np.float32)
    )
    return PolygonLayout(
        vertices=verts.astype(np.float32),
        starts=starts.astype(index_dtype(int(starts[-1]))),
        row=np.asarray(rows, np.int32)[order],
        bbox=bbox,
        centroid=cent[order],
        block_bbox=block_bbox.astype(np.float32),
    )


def write_polygons(group: zarr.Group, lay: PolygonLayout) -> None:
    sharded(group, "vertices", lay.vertices, VERTEX_CHUNK)
    for name in ("starts", "row", "bbox", "centroid"):
        sharded(group, name, getattr(lay, name), POLY_BLOCK)
    array(group, "block_bbox", lay.block_bbox)


# ---------------------------------------------------------------- points (SPEC §8)
@dataclass
class PointsLayout:
    bbox: tuple[float, float, float, float]
    offsets: np.ndarray
    columns: dict[str, np.ndarray]  # xy, and z / cell when present (sorted)
    chunk_bbox: np.ndarray
    overview: dict[str, np.ndarray] = field(default_factory=dict)  # + feature, offsets
    lod: dict[str, np.ndarray] = field(default_factory=dict)  # + offsets


def _feature_offsets(feature: np.ndarray, n_features: int) -> np.ndarray:
    counts = np.bincount(feature, minlength=n_features)
    return np.concatenate([[0], np.cumsum(counts)])


def points_layout(
    xy: np.ndarray,
    feature: np.ndarray,
    n_features: int,
    *,
    z: np.ndarray | None = None,
    cell: np.ndarray | None = None,
    overview: int = OVERVIEW_POINTS,
    lod_cap: int = LOD_CAP,
    seed: int = 0,
) -> PointsLayout:
    """Sort points by feature then Morton code; derive offsets, chunk boxes and reduced views."""
    xy = np.asarray(xy, np.float32)
    box = bounds(xy)
    order = np.lexsort((morton_order(xy[:, 0], xy[:, 1], box), feature))
    feat = np.asarray(feature, np.int64)[order]
    cols: dict[str, np.ndarray] = {"xy": xy[order]}
    if z is not None:
        cols["z"] = np.asarray(z, np.float32)[order]
    if cell is not None:
        cols["cell"] = np.asarray(cell, np.int32)[order]
    n = xy.shape[0]
    offsets = _feature_offsets(feat, n_features)
    off_dtype = index_dtype(n)

    # Overview: uniform random sample; sorted indices keep the feature/Morton order.
    rng = np.random.default_rng(seed)
    ov_idx = np.sort(rng.choice(n, min(n, overview), replace=False)) if n else np.zeros(0, int)
    ov = {k: v[ov_idx] for k, v in cols.items()}
    ov["feature"] = feat[ov_idx].astype(np.int32)
    ov["offsets"] = _feature_offsets(feat[ov_idx], n_features).astype(off_dtype)

    # LOD: genes above the cap keep `lod_cap` evenly spaced points of their Morton-sorted range,
    # which is an even spatial sample.
    picks: list[np.ndarray] = []
    lod_counts = np.zeros(n_features, np.int64)
    for f in range(n_features):
        lo, hi = int(offsets[f]), int(offsets[f + 1])
        if hi - lo > lod_cap:
            picks.append(np.linspace(lo, hi - 1, lod_cap).round().astype(np.int64))
            lod_counts[f] = lod_cap
    lod_idx = np.concatenate(picks) if picks else np.zeros(0, np.int64)
    lod = {k: v[lod_idx] for k, v in cols.items()}
    lod["offsets"] = np.concatenate([[0], np.cumsum(lod_counts)]).astype(off_dtype)

    return PointsLayout(
        bbox=box,
        offsets=offsets.astype(off_dtype),
        columns=cols,
        chunk_bbox=row_boxes(cols["xy"], POINT_CHUNK),
        overview=ov,
        lod=lod,
    )


def write_points(group: zarr.Group, lay: PointsLayout, features: list[str]) -> None:
    group.attrs["dbx"] = {"features": features}
    for name, v in lay.columns.items():
        sharded(group, name, v, POINT_CHUNK)
    array(group, "offsets", lay.offsets)
    array(group, "chunk_bbox", lay.chunk_bbox)
    for sub, cols in (("overview", lay.overview), ("lod", lay.lod)):
        g = group.require_group(sub)
        for name, v in cols.items():
            if name == "offsets":
                array(g, name, v)
            else:
                sharded(g, name, v, POINT_CHUNK)


# ---------------------------------------------------------------- labels (SPEC §6)
def needs_relabel(max_label: int, n_labels: int) -> bool:
    return max_label >= 2**32 or max_label > 4 * n_labels + 1024


def relabel(labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Map label IDs to 1..N in order of the original IDs (0 stays background).

    Returns (uint32 labels, uint64 original IDs of length N + 1 with entry 0 = 0). Negative
    values are background. Whole-array version for small masks; the converter streams blocks
    with the same mapping (np.searchsorted on the sorted original IDs).
    """
    flat = np.asarray(labels).astype(np.int64, copy=False)
    flat = np.where(flat < 0, 0, flat)
    ids = np.unique(flat)
    ids = ids[ids != 0]
    original = np.concatenate([[0], ids]).astype(np.uint64)
    new = np.searchsorted(original, flat.astype(np.uint64)).astype(np.uint32)
    return new, original


def labels_container_attrs(names: list[str]) -> dict[str, Any]:
    """Attributes of the `labels/` group: NGFF lists the label images it contains."""
    return {"ome": {"version": "0.5", "labels": sorted(names)}}


def image_label_attrs(source_image: str | None) -> dict[str, Any]:
    """NGFF `image-label` block for a labels multiscale (added under attrs.ome)."""
    meta: dict[str, Any] = {}
    if source_image is not None:
        meta["source"] = {"image": f"../../images/{source_image}"}
    return {"image-label": meta}
