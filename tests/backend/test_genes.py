"""Gene expression on the preview path: gene list, values per gene for CSC / CSR / dense
matrices in h5ad and zarr, the encoding, the conversion limit, and golden tests against anndata
on the example datasets."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from conftest import CACHE, EXAMPLES, log_events
from fastapi.testclient import TestClient
from scipy import sparse
from test_datasets import _examples_cfg, client_for

from dbx.catalog import entry_id
from dbx.config import Config, MemoryConfig
from dbx.data.genes import GeneValues, decode, encode, gene_list

N, G = 400, 30


def matrix_file(path: Path, encoding: str) -> ad.AnnData:
    rng = np.random.default_rng(1)
    dense = rng.random((N, G), dtype=np.float32)
    dense[dense < 0.8] = 0  # 80 % zeros
    dense[:, 0] = 0  # a gene expressed nowhere
    dense[5, 1] = np.nan  # a missing value
    x = {"csr": sparse.csr_matrix, "csc": sparse.csc_matrix, "dense": np.asarray}[encoding](dense)
    a = ad.AnnData(X=x)
    a.var_names = [f"ENSG{i:011d}" for i in range(G)]
    a.var["gene_symbols"] = [f"Gene{i}" for i in range(G)]
    a.layers["counts"] = sparse.csr_matrix(np.rint(np.nan_to_num(dense) * 10))
    a.obsm["X_umap"] = rng.random((N, 2), dtype=np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    a.write_zarr(path) if path.suffix == ".zarr" else a.write_h5ad(path)
    return a


def get_gene(c: TestClient, id: str, i: int, **params: object) -> np.ndarray:
    r = c.get(f"/api/datasets/{id}/gene", params={"i": i, **params})
    assert r.status_code == 200, r.text
    return decode(r.content, dict(r.headers))


def dense_x(a: ad.AnnData, matrix: str = "X") -> np.ndarray:
    m = a.X if matrix == "X" else a.layers[matrix]
    return np.asarray(m.toarray() if sparse.issparse(m) else m, dtype=np.float32)


@pytest.mark.parametrize("suffix", [".h5ad", ".zarr"])
@pytest.mark.parametrize("encoding", ["csc", "csr", "dense"])
def test_gene_values_match_anndata(cfg: Config, encoding: str, suffix: str) -> None:
    name = f"m_{encoding}{suffix}"
    a = matrix_file(cfg.data_roots[0].path / name, encoding)
    c = client_for(cfg)
    id = entry_id("data", name)
    g = c.get(f"/api/datasets/{id}/genes").json()
    assert g["names"][3] == "ENSG00000000003" and g["symbols"][3] == "Gene3"
    assert g["symbol_column"] == "gene_symbols"
    acc = {m["name"]: m["access"] for m in g["matrices"]}
    assert acc == {"X": "memory" if encoding == "csr" else "direct", "counts": "memory"}
    x = dense_x(a)
    for i in (0, 1, 7, G - 1):
        np.testing.assert_array_equal(get_gene(c, id, i), x[:, i])
    np.testing.assert_array_equal(get_gene(c, id, 4, matrix="counts"), dense_x(a, "counts")[:, 4])
    # quantised: within half a step of the true value
    q = get_gene(c, id, 7, q=16)
    np.testing.assert_allclose(q, x[:, 7], atol=float(np.nanmax(x[:, 7])) / 65534)


def test_gene_reply_headers_and_errors(cfg: Config) -> None:
    a = matrix_file(cfg.data_roots[0].path / "m.h5ad", "csc")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    r = c.get(f"/api/datasets/{id}/gene", params={"i": 7})
    h = r.headers
    assert h["x-dbx-encoding"] == "sparse"  # 80 % zeros: indices + values are smaller
    assert int(h["x-dbx-nnz"]) == int(np.count_nonzero(dense_x(a)[:, 7]))
    assert float(h["x-dbx-p99"]) <= float(h["x-dbx-max"]) <= 1
    assert h["content-encoding"] == "identity"
    assert (
        c.get(
            f"/api/datasets/{id}/gene", params={"i": 7}, headers={"if-none-match": h["etag"]}
        ).status_code
        == 304
    )
    zero = c.get(f"/api/datasets/{id}/gene", params={"i": 0}).headers
    assert (zero["x-dbx-nnz"], zero["x-dbx-max"]) == ("0", "0.0")
    for params, status, code in [
        ({"i": G}, 404, "unknown_element"),
        ({"i": 1, "matrix": "nope"}, 404, "unknown_element"),
        ({"i": 1, "q": 8}, 400, "invalid_q"),
    ]:
        r = c.get(f"/api/datasets/{id}/gene", params=params)
        assert r.status_code == status and r.json()["detail"]["code"] == code


def test_large_csr_needs_conversion_but_csc_reads_directly(cfg: Config) -> None:
    cfg.memory = MemoryConfig(small_file_gb=1e-6)  # ~1 KB: every CSR matrix is "large"
    matrix_file(cfg.data_roots[0].path / "csr.h5ad", "csr")
    a = matrix_file(cfg.data_roots[0].path / "csc.h5ad", "csc")
    c = client_for(cfg)
    r = c.get(f"/api/datasets/{entry_id('data', 'csr.h5ad')}/gene", params={"i": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "needs_conversion:x_csr"
    meta = c.get(f"/api/datasets/{entry_id('data', 'csr.h5ad')}/meta").json()
    assert {m["name"]: m["access"] for m in meta["matrices"]}["X"] == "needs_conversion"
    np.testing.assert_array_equal(get_gene(c, entry_id("data", "csc.h5ad"), 2), dense_x(a)[:, 2])


def test_csr_is_loaded_once_and_logged(cfg: Config) -> None:
    matrix_file(cfg.data_roots[0].path / "csr.h5ad", "csr")
    c = client_for(cfg)
    id = entry_id("data", "csr.h5ad")
    for i in range(5):
        get_gene(c, id, i)
    loads = [
        e
        for e in log_events()
        if str(e.get("stage", "")).endswith("load_array") and e.get("what") == "matrix_csc"
    ]
    assert len(loads) == 1 and loads[0]["encoding"] == "csr" and loads[0]["nbytes"] > 0


def test_gene_list_symbols_only_for_id_like_names() -> None:
    var = pd.DataFrame({"gene_symbols": ["A", "B"]}, index=["CD3E", "MS4A1"])
    assert gene_list(var).symbols is None  # the index already holds symbols
    var.index = ["ENSG00000010610", "ENSG00000156738"]
    assert gene_list(var).symbols == ["A", "B"]
    var = pd.DataFrame({"n_cells": [1, 2]}, index=["ENSG00000010610", "ENSG00000156738"])
    assert gene_list(var).symbols is None  # no text column to use


def test_encoding_round_trips_sparse_dense_and_missing() -> None:
    n = 1000
    idx = np.array([3, 10, 999])
    g = GeneValues(n, idx, np.array([1.5, np.nan, -2.0], dtype=np.float32))
    for q in (False, True):
        e = encode(g, quantise=q)
        assert e.headers["x-dbx-encoding"] == "sparse"
        out = decode(e.body, e.headers)
        assert np.isnan(out[10]) and out[0] == 0
        np.testing.assert_allclose(out[[3, 999]], [1.5, -2.0], atol=4 / 65534)
    full = GeneValues(4, np.arange(4), np.ones(4, dtype=np.float32))
    assert encode(full, quantise=False).headers["x-dbx-encoding"] == "dense"


# ---------------------------------------------------------------- golden: example datasets


@pytest.mark.parametrize("name", ["spatial.h5ad", "dissociated.h5ad"])
def test_example_h5ad_genes_golden(cfg: Config, example: Callable[[str], Path], name: str) -> None:
    c = client_for(_examples_cfg(cfg, example))
    id = entry_id("examples", name)
    a = ad.read_h5ad(EXAMPLES / name)
    g = c.get(f"/api/datasets/{id}/genes").json()
    assert g["names"] == [str(x) for x in a.var_names]
    x = a.X.tocsc() if sparse.issparse(a.X) else np.asarray(a.X)
    for i in (0, a.n_vars // 2, a.n_vars - 1):
        col = x[:, i].toarray().ravel() if sparse.issparse(x) else x[:, i]
        np.testing.assert_array_equal(get_gene(c, id, i), np.asarray(col, dtype=np.float32))


def test_example_spatialdata_table_genes_golden(
    cfg: Config, example: Callable[[str], Path]
) -> None:
    c = client_for(_examples_cfg(cfg, example))
    store = CACHE / "202302171205_region_1_scwf.zarr"
    id = entry_id("extracted", store.name)
    a = ad.read_zarr(store / "tables" / "table_cells")
    g = c.get(f"/api/datasets/{id}/genes", params={"table": "table_cells"}).json()
    assert len(g["names"]) == a.n_vars
    assert {m["name"] for m in g["matrices"]} == {
        "X",
        *(k for k in a.layers if k is not None),
    }  # anndata lists X as layer None
    for matrix in ("X", "counts"):
        m = a.X if matrix == "X" else a.layers[matrix]
        col = m.tocsc()[:, 5].toarray().ravel()
        got = get_gene(c, id, 5, matrix=matrix, table="table_cells")
        np.testing.assert_array_equal(got, col.astype(np.float32))
