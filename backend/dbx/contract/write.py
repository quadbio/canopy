"""Low-level writers for contract arrays (shared by the example store and the converter)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from zarr.codecs import ZstdCodec

from dbx.contract.models import CONTRACT, ROW_CHUNK, TILE, ZSTD_LEVEL

ANNDATA_ARRAY = {"encoding-type": "array", "encoding-version": "0.2.0"}
ANNDATA_DICT = {"encoding-type": "dict", "encoding-version": "0.1.0"}


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
) -> zarr.Array[Any]:
    """Write a numeric array with contract defaults (zstd, row chunks)."""
    if chunks is None:
        chunks = (min(ROW_CHUNK, max(1, data.shape[0])), *data.shape[1:])
    a = group.create_array(
        name,
        shape=data.shape,
        dtype=data.dtype,
        chunks=chunks,
        compressors=codecs(),
        overwrite=True,
    )
    if data.size:
        a[...] = data
    if attrs:
        a.attrs.update(attrs)
    return a


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
