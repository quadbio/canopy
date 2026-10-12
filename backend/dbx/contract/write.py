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
        # every chunk is written, also all-zero ones: a missing chunk is a 404 in the browser
        config={"write_empty_chunks": True},
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


def ring_areas(v: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Signed area of each closed ring of a flat vertex array (rings at `starts`), computed as
    the validator does (float64 of the stored float32 vertices)."""
    v = np.asarray(v, np.float64)
    if len(starts) < 2:
        return np.zeros(0)
    cross = np.append(v[:-1, 0] * v[1:, 1] - v[1:, 0] * v[:-1, 1], 0.0)
    firsts, lasts = starts[:-1], starts[1:] - 1
    cross[lasts] = 0.0  # no edge from a ring's last vertex to the next ring
    out: np.ndarray = np.add.reduceat(cross, firsts) / 2
    return out


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
    dropped: int = 0  # rings without area (degenerate, or not finite) left out


def _ranges(starts: np.ndarray, which: np.ndarray) -> np.ndarray:
    """Vertex indices of the rings `which`, in that order (vectorised concatenation)."""
    lengths = (starts[1:] - starts[:-1])[which]
    total = int(lengths.sum())
    if total == 0:
        return np.zeros(0, np.int64)
    first = np.concatenate([[0], np.cumsum(lengths)[:-1]])
    return np.repeat(starts[:-1][which] - first, lengths) + np.arange(total)


def polygon_layout_flat(
    vertices: np.ndarray, starts: np.ndarray, rows: np.ndarray
) -> PolygonLayout:
    """Layout of closed outer rings given as one vertex array (ring i = starts[i]:starts[i+1]).

    Rings are made counter-clockwise (checked on the float32 vertices, as the validator does);
    rings without area or with non-finite vertices are dropped (counted in `dropped`).
    Polygons are sorted by the Morton code of their centroids.
    """
    v = np.asarray(vertices, np.float32).reshape(-1, 2)
    starts = np.asarray(starts, np.int64)
    rows = np.asarray(rows, np.int64)
    n = len(starts) - 1
    lengths = starts[1:] - starts[:-1]
    ring_of = np.repeat(np.arange(n), lengths)
    finite = np.isfinite(v).all(1)
    ok = np.logical_and.reduceat(finite, starts[:-1]) if n and len(v) else np.ones(n, bool)
    ok &= lengths >= 4
    area = ring_areas(v, starts) if n else np.zeros(0)
    ok &= np.isfinite(area) & (area != 0)
    # reverse clockwise rings in place (a reversed closed ring stays closed)
    flip = (area < 0)[ring_of] if n else np.zeros(0, bool)
    local = np.arange(len(v)) - starts[:-1][ring_of]
    src = np.where(flip, starts[:-1][ring_of] + lengths[ring_of] - 1 - local, np.arange(len(v)))
    v = v[src]
    keep = np.flatnonzero(ok)
    # centroids relative to each ring's first vertex (no cancellation for large coordinates)
    idx = _ranges(starts, keep)
    kv = v[idx].astype(np.float64)
    klen = lengths[keep]
    kstarts = np.concatenate([[0], np.cumsum(klen)])
    kring = np.repeat(np.arange(len(keep)), klen)
    origin = kv[kstarts[:-1]] if len(keep) else np.zeros((0, 2))
    rel = kv - origin[kring]
    cross = np.append(rel[:-1, 0] * rel[1:, 1] - rel[1:, 0] * rel[:-1, 1], 0.0)
    if len(keep):
        cross[kstarts[1:] - 1] = 0.0
        nxt = np.vstack([rel[1:], rel[:1]])
        a = np.add.reduceat(cross, kstarts[:-1]) / 2
        cx = np.add.reduceat((rel[:, 0] + nxt[:, 0]) * cross, kstarts[:-1]) / (6 * a)
        cy = np.add.reduceat((rel[:, 1] + nxt[:, 1]) * cross, kstarts[:-1]) / (6 * a)
        cent = (np.c_[cx, cy] + origin).astype(np.float32)
        lo = np.minimum.reduceat(kv, kstarts[:-1]).astype(np.float32)
        hi = np.maximum.reduceat(kv, kstarts[:-1]).astype(np.float32)
        bbox = np.concatenate([lo, hi], axis=1)
    else:
        cent, bbox = np.zeros((0, 2), np.float32), np.zeros((0, 4), np.float32)
    order = np.argsort(morton_order(cent[:, 0], cent[:, 1], bounds(cent)), kind="stable")
    verts = v[_ranges(starts, keep[order])]
    out_starts = np.concatenate([[0], np.cumsum(klen[order])])
    bbox = bbox[order]
    block = np.arange(0, len(keep), POLY_BLOCK)
    block_bbox = (
        np.concatenate(
            [np.minimum.reduceat(bbox[:, :2], block), np.maximum.reduceat(bbox[:, 2:], block)],
            axis=1,
        )
        if len(keep)
        else np.zeros((0, 4), np.float32)
    )
    return PolygonLayout(
        vertices=verts.astype(np.float32),
        starts=out_starts.astype(index_dtype(int(out_starts[-1]))),
        row=rows[keep][order].astype(np.int32),
        bbox=bbox.astype(np.float32),
        centroid=cent[order],
        block_bbox=block_bbox.astype(np.float32),
        dropped=int(n - len(keep)),
    )


def polygon_layout(rings: list[np.ndarray], rows: np.ndarray) -> PolygonLayout:
    """Normalise outer rings and sort polygons by the Morton code of their centroid."""
    closed = []
    for r in rings:
        r = np.asarray(r, np.float32).reshape(-1, 2)
        closed.append(r if len(r) > 1 and np.array_equal(r[0], r[-1]) else np.vstack([r, r[:1]]))
    lengths = np.array([len(r) for r in closed], np.int64)
    starts = np.concatenate([[0], np.cumsum(lengths)])
    verts = np.concatenate(closed) if closed else np.zeros((0, 2), np.float32)
    return polygon_layout_flat(verts, starts, np.asarray(rows, np.int64))


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


def sort_keys(
    xy: np.ndarray, feature: np.ndarray, box: tuple[float, float, float, float]
) -> np.ndarray:
    """One uint64 key per point: feature in the high 32 bits, Morton code (32 bits) below.
    Computed in blocks, so the temporaries stay small."""
    keys = np.empty(xy.shape[0], np.uint64)
    step = 1 << 22
    for s in range(0, xy.shape[0], step):
        b = xy[s : s + step]
        keys[s : s + step] = (
            np.asarray(feature[s : s + step]).astype(np.uint64) << np.uint64(32)
        ) | (morton_order(b[:, 0], b[:, 1], box))
    return keys


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
    tick: Any = None,
) -> PointsLayout:
    """Sort points by feature then Morton code; derive offsets, chunk boxes and reduced views.

    Memory: the inputs plus about 32 bytes per point (sort key and order, sorted copies);
    `tick(fraction)` reports progress between the steps."""
    xy = np.asarray(xy, np.float32)
    feature = np.asarray(feature)
    box = bounds(xy)
    keys = sort_keys(xy, feature, box)
    if tick:
        tick(0.2)
    # not stable, and need not be: the key holds the order (equal keys are the same gene in the
    # same Morton cell); 3–4 times faster than a stable sort, deterministic for the same input
    order = np.argsort(keys)
    del keys
    if tick:
        tick(0.5)
    n = xy.shape[0]
    offsets = _feature_offsets(feature.astype(np.int64, copy=False), n_features)
    cols: dict[str, np.ndarray] = {"xy": xy[order]}
    if tick:
        tick(0.65)
    if z is not None:
        cols["z"] = np.asarray(z, np.float32)[order]
    if cell is not None:
        cols["cell"] = np.asarray(cell, np.int32)[order]
    if tick:
        tick(0.8)
    off_dtype = index_dtype(n)

    # Overview: uniform random sample; sorted indices keep the feature/Morton order.
    rng = np.random.default_rng(seed)
    ov_idx = np.sort(rng.choice(n, min(n, overview), replace=False)) if n else np.zeros(0, int)
    ov = {k: v[ov_idx] for k, v in cols.items()}
    ov_feat = feature[order[ov_idx]].astype(np.int64)
    ov["feature"] = ov_feat.astype(np.int32)
    ov["offsets"] = _feature_offsets(ov_feat, n_features).astype(off_dtype)
    del order

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


def write_points(
    group: zarr.Group, lay: PointsLayout, features: list[str], tick: Any = None
) -> None:
    group.attrs["dbx"] = {"features": features}
    for i, (name, v) in enumerate(lay.columns.items()):
        sharded(group, name, v, POINT_CHUNK)
        if tick:
            tick((i + 1) / (len(lay.columns) + 1))
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


def label_ids(block: np.ndarray) -> np.ndarray:
    """Label IDs of a block as uint64, negative values as background (0). uint64 sources keep
    IDs ≥ 2⁶³."""
    b = np.asarray(block)
    if b.dtype.kind == "i":
        b = np.where(b < 0, 0, b)
    elif b.dtype.kind != "u":
        b = np.where(np.isfinite(b) & (b > 0), b, 0)
    return b.astype(np.uint64, copy=False)


def relabel_block(block: np.ndarray, original: np.ndarray | None) -> np.ndarray:
    """uint32 labels of a block: mapped to 1..N through the sorted `original` IDs, or cast."""
    ids = label_ids(block)
    if original is None:
        return ids.astype(np.uint32)
    out: np.ndarray = np.searchsorted(original, ids).astype(np.uint32)
    return out


def relabel(labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Map label IDs to 1..N in order of the original IDs (0 stays background).

    Returns (uint32 labels, uint64 original IDs of length N + 1 with entry 0 = 0). Negative
    values are background. Whole-array version for small masks; the converter maps block by
    block with `relabel_block` and the same original IDs.
    """
    ids = np.unique(label_ids(labels))
    ids = ids[ids != 0]
    original = np.concatenate([np.zeros(1, np.uint64), ids.astype(np.uint64)])
    return relabel_block(labels, original), original


def labels_container_attrs(names: list[str]) -> dict[str, Any]:
    """Attributes of the `labels/` group: NGFF lists the label images it contains."""
    return {"ome": {"version": "0.5", "labels": sorted(names)}}


def image_label_attrs(source_image: str | None) -> dict[str, Any]:
    """NGFF `image-label` block for a labels multiscale (added under attrs.ome)."""
    meta: dict[str, Any] = {}
    if source_image is not None:
        meta["source"] = {"image": f"../../images/{source_image}"}
    return {"image-label": meta}
