"""Write synthetic AnnData files (h5ad or zarr), streaming X so files can exceed RAM."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Literal

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import zarr
from anndata.io import sparse_dataset, write_elem

from canopy.logs import timed
from canopy.synth.matrix import SynthMatrix
from canopy.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec

# Target number of stored values per write block (bounds memory while writing).
_BLOCK_VALUES = 20_000_000


def _palette(n: int, rng: np.random.Generator) -> list[str]:
    return [f"#{int(x):06x}" for x in rng.integers(0, 0xFFFFFF, n)]


def _column(spec: ColumnSpec, n: int, rng: np.random.Generator) -> Any:
    if spec.kind == "categorical":
        k = max(1, min(spec.n_categories, n)) if n else max(1, spec.n_categories)
        cats = [f"{spec.name}_{i}" for i in range(k)]
        codes = rng.integers(0, k, n)
        return pd.Categorical.from_codes(codes, categories=cats)
    if spec.kind == "numeric":
        if np.dtype(spec.dtype).kind in "iu":
            return rng.integers(0, 1000, n).astype(spec.dtype)
        return rng.random(n).astype(spec.dtype)
    if spec.kind == "boolean":
        return rng.random(n) < 0.5
    return np.array([f"{spec.name}_{i}" for i in range(n)], dtype=object)


def _frame(
    cols: list[ColumnSpec], n: int, prefix: str, index_name: str | None, rng: np.random.Generator
) -> pd.DataFrame:
    index = pd.Index([f"{prefix}{i}" for i in range(n)], name=index_name)
    return pd.DataFrame({c.name: _column(c, n, rng) for c in cols}, index=index)


def _dataset_kwargs(m: MatrixSpec, is_zarr: bool) -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if m.chunk:
        kw["chunks"] = (m.chunk,)
    if is_zarr:
        if m.compression == "none":
            kw["compressors"] = None
        return kw
    if m.compression in ("gzip", "lzf"):
        kw["compression"] = m.compression
    return kw


def _write_matrix(
    group: Any, key: str, m: MatrixSpec, n_obs: int, n_vars: int, is_zarr: bool
) -> None:
    mat = SynthMatrix(n_obs, n_vars, m.density, seed=m.seed, dtype=m.dtype)
    kw = _dataset_kwargs(m, is_zarr)
    expected_per_row = max(1.0, n_vars * max(m.density, 1e-9))
    if m.encoding == "csc":
        per_block = max(1, int(_BLOCK_VALUES / max(1.0, n_obs * max(m.density, 1e-9))))
        blocks = mat.col_blocks(per_block)
    else:
        per_block = max(1, int(_BLOCK_VALUES / expected_per_row))
        blocks = mat.row_blocks(per_block)
    if m.encoding == "dense":
        # The full-size array first (anndata's write_elem would fix its shape to the first
        # block), then the blocks into it: X is streamed, never whole in memory.
        chunks = (min(m.chunk, n_obs), n_vars) if m.chunk else None
        if is_zarr:
            arr = group.create_array(
                key,
                shape=(n_obs, n_vars),
                dtype=m.dtype,
                chunks=chunks or (min(n_obs, 65_536), n_vars),
                **({"compressors": None} if m.compression == "none" else {}),
            )
        else:
            arr = group.create_dataset(
                key,
                shape=(n_obs, n_vars),
                dtype=m.dtype,
                chunks=chunks,
                compression=kw.get("compression"),
            )
        arr.attrs["encoding-type"] = "array"
        arr.attrs["encoding-version"] = "0.2.0"
        start = 0
        for blk in blocks:
            arr[start : start + blk.shape[0]] = blk.toarray()
            start += blk.shape[0]
        return
    first = next(blocks)
    write_elem(group, key, first if m.encoding == "csr" else first.tocsc(), dataset_kwargs=kw)
    ds: Any = sparse_dataset(group[key])  # CSR and CSC datasets both support append
    for blk in blocks:
        ds.append(blk)


def write_anndata(
    spec: AnnDataSpec, path: Path, fmt: Literal["h5ad", "zarr"] | None = None
) -> Path:
    """Create the file described by `spec`; returns its path."""
    path = Path(path)
    fmt = fmt or ("zarr" if path.suffix == ".zarr" else "h5ad")
    rng = np.random.default_rng(spec.seed)
    with timed("synth_anndata", n_obs=spec.n_obs, n_vars=spec.n_vars, fmt=fmt):
        obs = _frame(spec.obs, spec.n_obs, "cell", spec.obs_index_name, rng)
        var = _frame(spec.var, spec.n_vars, "gene", spec.var_index_name, rng)
        adata = ad.AnnData(obs=obs, var=var)
        # Embeddings: one blob per category of the first categorical column, so a plot of them
        # looks like clusters (and the colours line up with them).
        groups = next(
            (obs[c.name].cat.codes.to_numpy() for c in spec.obs if c.kind == "categorical"), None
        )
        for name, dims in spec.obsm.items():
            emb = rng.normal(size=(spec.n_obs, dims)).astype("float32")
            if groups is not None and len(groups):
                centres = rng.normal(scale=6.0, size=(int(groups.max()) + 1, dims)).astype(
                    "float32"
                )
                emb += centres[groups]
            adata.obsm[name] = emb
        for c in spec.obs:
            if c.kind == "categorical" and c.colors:
                adata.uns[f"{c.name}_colors"] = _palette(len(obs[c.name].cat.categories), rng)
        for key, n in spec.extra_colors.items():
            adata.uns[f"{key}_colors"] = _palette(n, rng)
        if fmt == "h5ad":
            adata.write_h5ad(path)
            with h5py.File(path, "a") as f:
                _write_all_matrices(f, spec, is_zarr=False)
        else:
            adata.write_zarr(path)
            g = zarr.open_group(path, mode="a", use_consolidated=False)
            _write_all_matrices(g, spec, is_zarr=True)
            consolidate(path)
    return path


def _write_all_matrices(root: Any, spec: AnnDataSpec, is_zarr: bool) -> None:
    if spec.X is not None:
        if "X" in root:
            del root["X"]
        _write_matrix(root, "X", spec.X, spec.n_obs, spec.n_vars, is_zarr)
    if spec.layers:
        layers = root["layers"] if "layers" in root else root.create_group("layers")
        for name, m in spec.layers.items():
            _write_matrix(layers, name, m, spec.n_obs, spec.n_vars, is_zarr)


def consolidate(path: Path) -> None:
    """Consolidate zarr metadata (what anndata and spatialdata expect), without the v3 warning."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Consolidated metadata", category=UserWarning)
        zarr.consolidate_metadata(path)
