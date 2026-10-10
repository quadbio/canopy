"""Hand-built h5ad files in the layouts of old anndata versions (tests only).

`write_pre07`: anndata < 0.7 — obs/var/obsm as HDF5 compound datasets, categories in
`uns/<col>_categories`, sparse X marked with `h5sparse_format`, raw as `raw.X`/`raw.var`.
`write_v07`: anndata 0.7 — dataframe groups, categorical codes with a `categories` object
reference to `<frame>/__categories/<col>`.
"""

from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import scipy.sparse as sp

N_OBS, N_VARS = 5, 3
STR = h5py.special_dtype(vlen=str)


def _x() -> sp.csr_matrix:
    return sp.csr_matrix(np.arange(N_OBS * N_VARS, dtype=np.float32).reshape(N_OBS, N_VARS) % 4)


def _sparse(group: h5py.Group, name: str, m: sp.csr_matrix, legacy: bool) -> None:
    g = group.create_group(name)
    g.create_dataset("data", data=m.data)
    g.create_dataset("indices", data=m.indices)
    g.create_dataset("indptr", data=m.indptr)
    if legacy:
        g.attrs["h5sparse_format"] = "csr"
        g.attrs["h5sparse_shape"] = np.array(m.shape)
    else:
        g.attrs["encoding-type"] = "csr_matrix"
        g.attrs["encoding-version"] = "0.1.0"
        g.attrs["shape"] = np.array(m.shape)


def write_pre07(path: Path) -> Path:
    with h5py.File(path, "w") as f:
        _sparse(f, "X", _x(), legacy=True)
        obs_t = np.dtype([("index", STR), ("leiden", np.int8), ("n_genes", np.int64)])
        obs = np.array([(f"cell{i}", i % 2, 10 * i) for i in range(N_OBS)], dtype=obs_t)
        f.create_dataset("obs", data=obs)
        var_t = np.dtype([("index", STR), ("highly_variable", np.bool_)])
        f.create_dataset(
            "var", data=np.array([(f"g{i}", i == 0) for i in range(N_VARS)], dtype=var_t)
        )
        obsm_t = np.dtype([("X_umap", np.float32, (2,)), ("X_pca", np.float32, (4,))])
        f.create_dataset("obsm", data=np.zeros(N_OBS, dtype=obsm_t))
        uns = f.create_group("uns")
        uns.create_dataset("leiden_categories", data=np.array(["a", "b"], dtype=object), dtype=STR)
        uns.create_dataset(
            "leiden_colors", data=np.array(["#ff0000", "#00ff00"], dtype=object), dtype=STR
        )
        _sparse(f, "raw.X", _x(), legacy=True)
        f.create_dataset(
            "raw.var", data=np.array([(f"g{i}", False) for i in range(N_VARS)], dtype=var_t)
        )
    return path


def _frame07(f: h5py.File, name: str, index: list[str], cats: dict[str, list[str]]) -> h5py.Group:
    g = f.create_group(name)
    g.attrs["encoding-type"] = "dataframe"
    g.attrs["encoding-version"] = "0.1.0"
    g.attrs["_index"] = "index"
    g.create_dataset("index", data=np.array(index, dtype=object), dtype=STR)
    order = []
    if cats:
        cg = g.create_group("__categories")
        for col, values in cats.items():
            c = cg.create_dataset(col, data=np.array(values, dtype=object), dtype=STR)
            codes = g.create_dataset(col, data=np.arange(len(index), dtype=np.int8) % len(values))
            codes.attrs["categories"] = c.ref
            order.append(col)
    g.attrs.create("column-order", order, dtype=STR, shape=(len(order),))
    return g


def write_v07(path: Path) -> Path:
    with h5py.File(path, "w") as f:
        f.attrs["encoding-type"] = "anndata"
        f.attrs["encoding-version"] = "0.1.0"
        _sparse(f, "X", _x(), legacy=False)
        obs = _frame07(f, "obs", [f"cell{i}" for i in range(N_OBS)], {"leiden": ["a", "b"]})
        obs.create_dataset("label", data=np.array([b"x"] * N_OBS, dtype="S16"))
        obs.attrs.create("column-order", ["leiden", "label"], dtype=STR, shape=(2,))
        _frame07(f, "var", [f"g{i}" for i in range(N_VARS)], {})
        f.create_group("obsm").create_dataset("X_umap", data=np.zeros((N_OBS, 2), np.float32))
        uns = f.create_group("uns")
        uns.create_dataset(
            "leiden_colors", data=np.array(["#ff0000", "#00ff00"], dtype=object), dtype=STR
        )
    return path
