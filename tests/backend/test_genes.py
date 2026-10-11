"""Gene expression on the preview path: gene list, values per gene for CSC / CSR / dense
matrices in h5ad and zarr, the access decision, the in-memory copies (block-wise, single-flight,
reserved), the encoding, the log line, and golden tests against anndata on the examples."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import pytest
from conftest import CACHE, EXAMPLES, log_events
from fastapi.testclient import TestClient
from scipy import sparse
from test_datasets import _examples_cfg, client_for

from dbx.catalog import entry_id
from dbx.config import Config, MemoryConfig
from dbx.data import genes as genes_mod
from dbx.data import reader
from dbx.data.cache import DatasetCache, MemoryBusyError
from dbx.data.genes import (
    GeneValues,
    access,
    column_read_bytes,
    decode,
    encode,
    from_column,
    gene_list,
    quantise,
    unique_names,
)
from dbx.inspect.models import ArrayInfo, MatrixInfo

N, G = 400, 30


def matrix_file(path: Path, encoding: str, *, names: list[str] | None = None) -> ad.AnnData:
    rng = np.random.default_rng(1)
    dense = rng.random((N, G), dtype=np.float32)
    dense[dense < 0.8] = 0  # 80 % zeros
    dense[:, 0] = 0  # a gene expressed nowhere
    dense[5, 1] = np.nan  # a missing value
    x = {"csr": sparse.csr_matrix, "csc": sparse.csc_matrix, "dense": np.asarray}[encoding](dense)
    a = ad.AnnData(X=x)
    a.var_names = names or [f"ENSG{i:011d}" for i in range(G)]
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


# ---------------------------------------------------------------- values through the API


@pytest.mark.parametrize("suffix", [".h5ad", ".zarr"])
@pytest.mark.parametrize("encoding", ["csc", "csr", "dense"])
def test_gene_values_match_anndata(cfg: Config, encoding: str, suffix: str) -> None:
    name = f"m_{encoding}{suffix}"
    a = matrix_file(cfg.data_roots[0].path / name, encoding)
    c = client_for(cfg)
    id = entry_id("data", name)
    g = c.get(f"/api/datasets/{id}/genes").json()
    assert g["names"][3] == "ENSG00000000003" and g["symbols"][3] == "Gene3"
    assert (g["symbol_column"], g["duplicates"]) == ("gene_symbols", 0)
    acc = {m["name"]: m["access"] for m in g["matrices"]}
    assert acc == {"X": "memory" if encoding == "csr" else "direct", "counts": "memory"}
    x = dense_x(a)
    for i in (0, 1, 7, G - 1):
        np.testing.assert_array_equal(get_gene(c, id, i), x[:, i])
    counts = dense_x(a, "counts")
    np.testing.assert_array_equal(get_gene(c, id, 4, matrix="counts"), counts[:, 4])
    # quantised counts are exact (integers), and so are the zeros
    np.testing.assert_array_equal(get_gene(c, id, 4, matrix="counts", q=16), counts[:, 4])
    q = get_gene(c, id, 7, q=16)
    np.testing.assert_allclose(q, x[:, 7], atol=float(np.nanmax(x[:, 7])) / 65532)
    assert np.all(q[x[:, 7] == 0] == 0)


def test_gene_reply_headers_errors_and_etags(cfg: Config) -> None:
    a = matrix_file(cfg.data_roots[0].path / "m.h5ad", "csc")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    r = c.get(f"/api/datasets/{id}/gene", params={"i": 7})
    h = r.headers
    col = dense_x(a)[:, 7]
    assert h["x-dbx-encoding"] == "sparse"  # 80 % zeros: indices + values are smaller
    assert int(h["x-dbx-n-expressing"]) == int(np.count_nonzero(col))
    assert float(h["x-dbx-min"]) == 0.0 and float(h["x-dbx-max"]) == pytest.approx(col.max())
    assert h["content-encoding"] == "identity"
    again = c.get(f"/api/datasets/{id}/gene", params={"i": 7}, headers={"if-none-match": h["etag"]})
    assert again.status_code == 304
    genes = c.get(f"/api/datasets/{id}/genes")
    assert (
        c.get(
            f"/api/datasets/{id}/genes", headers={"if-none-match": genes.headers["etag"]}
        ).status_code
        == 304
    )
    zero = c.get(f"/api/datasets/{id}/gene", params={"i": 0}).headers
    assert (zero["x-dbx-n-expressing"], zero["x-dbx-max"]) == ("0", "0.0")
    for params, status, code in [
        ({"i": G}, 404, "unknown_gene"),
        ({"i": 1, "matrix": "nope"}, 404, "unknown_matrix"),
        ({"i": 1, "q": 8}, 400, "invalid_q"),
    ]:
        r = c.get(f"/api/datasets/{id}/gene", params=params)
        assert r.status_code == status and r.json()["detail"]["code"] == code


def test_gene_log_line_explains_the_request(cfg: Config) -> None:
    matrix_file(cfg.data_roots[0].path / "m.h5ad", "csr")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    get_gene(c, id, 3, q=16)
    get_gene(c, id, 4, matrix="counts")
    get_gene(c, id, 3, q=16)  # same gene again: a cache hit
    served = [e for e in log_events() if e["event"] == "gene_served"]
    first, layer, again = served[-3:]
    assert first["element"] == "X" and first["gene_index"] == 3 and first["layout"] == "csr"
    assert (first["access"], first["cache"], first["matrix_cache"]) == ("memory", "miss", "miss")
    assert first["q"] == 16 and first["encoding"] in ("sparse", "dense")
    assert first["n_cells"] == N and first["reply_bytes"] > 0 and "n_expressing" in first
    assert layer["element"] == "counts"
    assert again["cache"] == "hit"


def test_large_csr_needs_conversion_but_csc_reads_directly(cfg: Config) -> None:
    cfg.memory = MemoryConfig(small_file_gb=1e-6)  # ~1 KB: every CSR matrix is "large"
    matrix_file(cfg.data_roots[0].path / "csr.h5ad", "csr")
    a = matrix_file(cfg.data_roots[0].path / "csc.h5ad", "csc")
    c = client_for(cfg)
    r = c.get(f"/api/datasets/{entry_id('data', 'csr.h5ad')}/gene", params={"i": 1})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "needs_conversion:matrix_csr"
    meta = c.get(f"/api/datasets/{entry_id('data', 'csr.h5ad')}/meta").json()
    assert {m["name"]: m["access"] for m in meta["matrices"]}["X"] == "needs_conversion"
    np.testing.assert_array_equal(get_gene(c, entry_id("data", "csc.h5ad"), 2), dense_x(a)[:, 2])
    assert any(e["event"] == "gene_refused" for e in log_events())


def test_row_chunked_dense_is_loaded_whole_not_read_per_gene(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(genes_mod, "DIRECT_BYTES", 1000)  # the tiny file's chunks "too wide"
    path = cfg.data_roots[0].path / "dense.h5ad"
    a = matrix_file(path, "dense")
    with h5py.File(path, "a") as f:  # rewrite X with row-shaped chunks
        del f["X"]
        f.create_dataset("X", data=dense_x(a), chunks=(50, G), compression="gzip")
    c = client_for(cfg)
    id = entry_id("data", "dense.h5ad")
    acc = {m["name"]: m["access"] for m in c.get(f"/api/datasets/{id}/meta").json()["matrices"]}
    assert acc["X"] == "memory"
    for i in (1, 2, 9):
        np.testing.assert_array_equal(get_gene(c, id, i), dense_x(a)[:, i])
    loads = [
        e
        for e in log_events()
        if str(e.get("stage", "")).endswith("load_array") and e.get("what") == "matrix_columns"
    ]
    assert len(loads) == 1 and loads[0]["layout"] == "dense"


def test_duplicate_gene_names_are_made_unique(cfg: Config) -> None:
    names = [f"G{i}" for i in range(G)]
    names[5] = names[6] = "Dup"
    a = matrix_file(cfg.data_roots[0].path / "dup.h5ad", "csc", names=names)
    c = client_for(cfg)
    id = entry_id("data", "dup.h5ad")
    g = c.get(f"/api/datasets/{id}/genes").json()
    assert g["names"][5:7] == ["Dup", "Dup-1"] and g["duplicates"] == 1
    np.testing.assert_array_equal(get_gene(c, id, 6), dense_x(a)[:, 6])


# ---------------------------------------------------------------- access decision


def _dense(shape: list[int], chunks: list[int] | None) -> MatrixInfo:
    return MatrixInfo(
        encoding="dense",
        shape=shape,
        dtype="float32",
        storage=ArrayInfo(shape=shape, dtype="float32", chunks=chunks),
    )


def test_dense_access_depends_on_the_bytes_one_gene_touches() -> None:
    big = 64 * 2**30
    # narrow column chunks: one gene reads 1 M x 16 values = 64 MB
    assert access(_dense([1_000_000, 30_000], [10_000, 16]), big) == "direct"
    # row chunks: one gene decompresses the whole matrix
    assert column_read_bytes(_dense([1_000_000, 30_000], [1_000, 30_000])) == 1_000_000 * 30_000 * 4
    assert access(_dense([1_000_000, 30_000], [1_000, 30_000]), big) == "needs_conversion"
    # contiguous: one page per cell
    assert access(_dense([1_000_000, 30_000], None), big) == "needs_conversion"
    assert access(_dense([200_000, 2_000], None), 2 * 2**30) == "memory"
    assert access(_dense([2_000, 50], None), 0) == "direct"  # small: read whole anyway


# ---------------------------------------------------------------- in-memory copies


@pytest.mark.parametrize("seed", [0, 1])
def test_block_wise_csr_to_csc_matches_scipy(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    monkeypatch.setattr(reader, "BLOCK_VALUES", 37)  # many small blocks
    rng = np.random.default_rng(seed)
    d = sparse.random(250, 40, density=0.1, random_state=rng, dtype=np.float64).toarray()
    d[10:20, :] = 0  # empty rows
    d[:, 7] = 0  # an empty column
    m = sparse.csr_matrix(d)
    reads: list[tuple[int, int]] = []

    def read(lo: int, hi: int, *, values: bool) -> tuple[np.ndarray, np.ndarray | None]:
        reads.append((lo, hi))
        return m.indices[lo:hi], m.data[lo:hi] if values else None

    got = reader.csr_to_csc_blocks(m.indptr, 40, read)
    want = m.tocsc()
    want.sort_indices()
    assert got.dtype == np.float32 and len(reads) > 4
    np.testing.assert_array_equal(got.indptr, want.indptr)
    np.testing.assert_array_equal(got.indices, want.indices)
    np.testing.assert_allclose(got.data, want.data.astype(np.float32))


def test_concurrent_loads_of_the_same_array_run_once(cfg: Config) -> None:
    cache = DatasetCache(cfg)
    calls = []

    def load() -> np.ndarray:
        calls.append(1)
        time.sleep(0.2)
        return np.ones(10)

    results: list[np.ndarray] = []
    outcomes: list[dict[str, object]] = [{} for _ in range(8)]
    threads = [
        threading.Thread(
            target=lambda k=k: results.append(
                cache.array(("x",), load, lambda a: a.nbytes, outcome=outcomes[k])
            )
        )
        for k in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(results) == 8
    assert sorted(o["cache"] for o in outcomes).count("miss") == 1


def test_a_failed_load_fails_its_waiters_and_can_be_retried(cfg: Config) -> None:
    cache = DatasetCache(cfg)
    attempts = []

    def load() -> np.ndarray:
        attempts.append(1)
        time.sleep(0.1)
        if len(attempts) == 1:
            raise OSError(5, "Input/output error")
        return np.ones(3)

    errors: list[BaseException] = []

    def run() -> None:
        try:
            cache.array(("y",), load, lambda a: a.nbytes)
        except OSError as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 3 and len(attempts) == 1
    assert cache.array(("y",), load, lambda a: a.nbytes).sum() == 3


def test_reservations_respect_the_budget(cfg: Config) -> None:
    cfg.memory = MemoryConfig(budget_gb=0.001, cache_gb=0.0005)  # ~1 MB budget
    cache = DatasetCache(cfg)
    cache.array(("small",), lambda: np.ones(50_000), lambda a: a.nbytes)  # 400 KB cached
    with pytest.raises(MemoryBusyError):
        cache.array(("big",), lambda: np.ones(1), lambda a: a.nbytes, reserve=2 * 2**20)
    # a reservation that fits evicts older entries from the cache to make room
    cache.array(("mid",), lambda: np.ones(10), lambda a: a.nbytes, reserve=500_000)
    assert cache.stats()["reserved"] == 0
    assert ("small",) not in [k[1:] for k in cache._lru]
    assert any(e["event"] == "memory_busy" for e in log_events())


def test_memory_busy_is_a_503(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    matrix_file(cfg.data_roots[0].path / "csr.h5ad", "csr")
    c = client_for(cfg)

    def busy(*a: object, **k: object) -> None:
        raise MemoryBusyError

    monkeypatch.setattr(DatasetCache, "reserve", busy)
    r = c.get(f"/api/datasets/{entry_id('data', 'csr.h5ad')}/gene", params={"i": 1})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "memory_busy"


# ---------------------------------------------------------------- encoding


def test_quantisation_is_exact_for_integers_and_zero() -> None:
    codes, offset, scale = quantise(np.array([0, 2, 3, 5000], dtype=np.float32))
    assert (offset, scale) == (0.0, 1.0) and list(codes) == [0, 2, 3, 5000]
    vals = np.array([-1.3, 0, 0.7, np.inf, -np.inf, np.nan], dtype=np.float32)
    codes, offset, scale = quantise(vals)
    back = offset + codes.astype(np.float64) * scale
    assert back[1] == 0.0  # zero exact even with negative values
    assert abs(back[0] + 1.3) <= scale / 2 and abs(back[2] - 0.7) <= scale / 2
    assert back[3] == pytest.approx(0.7, abs=scale) and back[4] == pytest.approx(-1.3, abs=scale)
    assert codes[5] == genes_mod.MISSING_CODE


def test_encoding_round_trips_sparse_dense_and_missing() -> None:
    n = 1000
    g = GeneValues(n, np.array([3, 10, 999]), np.array([1.5, np.nan, -2.0], dtype=np.float32))
    for q in (False, True):
        e = encode(g, quantise_values=q)
        assert e.headers["x-dbx-encoding"] == "sparse"
        out = decode(e.body, e.headers)
        assert np.isnan(out[10]) and out[0] == 0
        np.testing.assert_allclose(out[[3, 999]], [1.5, -2.0], atol=4 / 65532)
    # dense, with negative values: every value within half a step, zeros exactly zero
    vals = np.linspace(-3, 5, 400).astype(np.float32)
    vals[::3] = 0
    full = GeneValues(400, np.arange(400), vals)
    for q in (False, True):
        e = encode(full, quantise_values=q)
        assert e.headers["x-dbx-encoding"] == "dense"
        out = decode(e.body, e.headers)
        np.testing.assert_allclose(out, vals, atol=8 / 65532)
        assert np.all(out[::3] == 0)
    st = encode(GeneValues(5, np.arange(5), np.array([0.5, 1, 2, 3, 4], dtype=np.float32)), False)
    assert float(st.headers["x-dbx-min"]) == 0.5  # all cells stored: the true minimum
    neg = encode(GeneValues(3, np.arange(3), np.array([-3, -2, -1], dtype=np.float32)), False)
    assert float(neg.headers["x-dbx-max"]) == -1.0


def test_csc_columns_with_repeated_rows_are_summed() -> None:
    g = from_column(np.array([4, 1, 4]), np.array([1.0, 2.0, 3.0]), 10)
    assert list(g.indices) == [1, 4] and list(g.values) == [2.0, 4.0]


def test_gene_list_symbols_only_for_id_like_names() -> None:
    var = pd.DataFrame({"gene_symbols": ["A", "B"]}, index=["CD3E", "MS4A1"])
    assert gene_list(var).symbols is None  # the index already holds symbols
    for ids in (
        ["ENSG00000010610", "ENSG00000156738"],
        ["ENSMUSG00000032093.5", "ENSMUSG00000024673"],
        ["FBgn0000490", "FBgn0003996"],
    ):
        var.index = ids
        assert gene_list(var).symbols == ["A", "B"], ids
    var = pd.DataFrame({"n_cells": [1, 2]}, index=["ENSG00000010610", "ENSG00000156738"])
    assert gene_list(var).symbols is None  # no text column to use


def test_unique_names_follow_anndata() -> None:
    assert unique_names(["A", "A", "B", "A", "A-1"]) == (["A", "A-2", "B", "A-3", "A-1"], 2)


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


@pytest.mark.parametrize(
    "store", ["202302171205_region_1_scwf.zarr", "202302171205_region_3_scwf.zarr"]
)
def test_example_spatialdata_table_genes_golden(
    cfg: Config, example: Callable[[str], Path], store: str
) -> None:
    example(store)
    c = client_for(_examples_cfg(cfg, example))
    id = entry_id("extracted", store)
    a = ad.read_zarr(CACHE / store / "tables" / "table_cells")
    g = c.get(f"/api/datasets/{id}/genes", params={"table": "table_cells"}).json()
    assert len(g["names"]) == a.n_vars
    layers = {k for k in a.layers if k is not None}  # anndata lists X as layer None
    assert {m["name"] for m in g["matrices"]} == {"X", *layers}
    for matrix in ("X", "counts"):
        m = a.X if matrix == "X" else a.layers[matrix]
        col = m.tocsc()[:, 5].toarray().ravel()
        got = get_gene(c, id, 5, matrix=matrix, table="table_cells", q=16)
        np.testing.assert_allclose(got, col.astype(np.float32), atol=float(col.max()) / 65532)
        if matrix == "counts":
            np.testing.assert_array_equal(got, col)  # integer counts are exact
    # the default table and its explicit name share one cache entry (and ETag)
    default = c.get(f"/api/datasets/{id}/gene", params={"i": 5})
    named = c.get(f"/api/datasets/{id}/gene", params={"i": 5, "table": "table_cells"})
    assert default.headers["etag"] == named.headers["etag"]
