"""The AnnData converter (h5ad / AnnData-zarr → dbx store) and the job system that runs it."""

from __future__ import annotations

import json
import time
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from conftest import log_events
from fastapi.testclient import TestClient
from scipy import sparse
from test_datasets import client_for

from dbx.catalog import entry_id
from dbx.config import Config, JobsConfig
from dbx.contract import validate_store
from dbx.convert.anndata import (
    ConvertOptions,
    convert_anndata,
    downsample_indices,
    has_store,
    store_dir,
)
from dbx.convert.matrix import ConversionError
from dbx.data.genes import decode
from dbx.inspect import inspect_path
from dbx.jobs.db import DB_FILE, JobsDB

N, G = 500, 40


def source(path: Path, encoding: str = "csr") -> ad.AnnData:
    rng = np.random.default_rng(3)
    dense = rng.random((N, G), dtype=np.float32)
    dense[dense < 0.85] = 0
    dense[:, 2] = 0  # a gene expressed nowhere
    dense[7, 5] = np.nan
    x = {"csr": sparse.csr_matrix, "csc": sparse.csc_matrix, "dense": np.asarray}[encoding](dense)
    a = ad.AnnData(X=x)
    a.var_names = [f"ENSG{i:011d}" for i in range(G)]
    a.var["gene_symbols"] = [f"Gene{i}" for i in range(G)]
    a.obs["cluster"] = pd.Categorical(rng.choice(["b", "a", "c"], N), categories=["b", "a", "c"])
    a.uns["cluster_colors"] = ["red", "#00ff00", "#0000ffaa"]  # names and alpha: normalised
    a.obs["score"] = rng.random(N)
    a.obs["text"] = [["y", "x"][i % 2] for i in range(N)]
    a.uns["text_colors"] = ["#111111", "#222222"]
    a.layers["counts"] = sparse.csr_matrix(np.rint(np.nan_to_num(dense) * 10))
    a.obsm["X_umap"] = rng.random((N, 2)).astype(np.float64)
    a.obsm["X_pca"] = rng.random((N, 10), dtype=np.float32)
    a.obsm["spatial"] = rng.random((N, 2), dtype=np.float32)
    a.obsm["one_col"] = rng.random((N, 1), dtype=np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    a.write_zarr(path) if path.suffix == ".zarr" else a.write_h5ad(path)
    return a


def dense(m: object) -> np.ndarray:
    return np.asarray(m.toarray() if sparse.issparse(m) else m, dtype=np.float32)  # type: ignore[attr-defined]


@pytest.mark.parametrize("suffix", [".h5ad", ".zarr"])
@pytest.mark.parametrize("encoding", ["csr", "csc", "dense"])
def test_conversion_is_valid_and_equal_to_the_source(
    tmp_path: Path, encoding: str, suffix: str
) -> None:
    src = tmp_path / f"src{suffix}"
    a = source(src, encoding)
    fp = inspect_path(src)
    stages: list[str] = []
    # a tiny memory budget: many gene buckets and row blocks
    opts = ConvertOptions(memory_bytes=200_000, tmp_dir=tmp_path / "tmp")
    r = convert_anndata(src, fp, tmp_path / "cache", opts, lambda s, f: stages.append(s))
    assert r.store == store_dir(tmp_path / "cache", fp.dataset_id) and has_store(
        tmp_path / "cache", fp.dataset_id
    )
    assert validate_store(r.store, deep=True).ok
    assert {"write", "finish"} <= set(stages)
    assert not list((tmp_path / "tmp").glob("*/bucket-*"))  # temporary files removed
    b = ad.read_zarr(r.store / "tables" / "table")
    assert b.X.format == "csc" and b.X.dtype == np.float32
    np.testing.assert_array_equal(dense(b.X), dense(a.X))
    pd.testing.assert_frame_equal(b.obs, a.obs, check_dtype=False)
    assert list(b.var_names) == list(a.var_names)
    assert set(b.obsm) == {"X_umap", "X_pca", "spatial"}  # one-column obsm left out
    np.testing.assert_array_equal(b.obsm["X_umap"], a.obsm["X_umap"].astype(np.float32))
    assert list(b.uns["cluster_colors"]) == ["#ff0000", "#00ff00", "#0000ff"]
    assert list(b.uns["text_colors"]) == ["#111111", "#222222"]
    t = r.manifest.tables["table"]
    assert (t.n_obs, t.n_vars, t.x_source, t.var_names_column) == (N, G, "X", "gene_symbols")
    assert t.embeddings == {"X_pca": 10, "X_umap": 2} and t.spatial_keys == ["spatial"]
    assert t.categorical["cluster"].has_colors and "score" in t.numeric
    # gene statistics
    x = dense(a.X)
    stats = b.uns["dbx"]["gene_stats"]
    finite = np.where(np.isfinite(x), x, 0)
    np.testing.assert_array_equal(stats["nnz"], (finite != 0).sum(0))
    np.testing.assert_allclose(stats["mean"], finite.sum(0) / N, rtol=1e-5)
    np.testing.assert_allclose(stats["max"], finite.max(0), rtol=1e-6)
    j = 9
    nz = x[:, j][np.isfinite(x[:, j]) & (x[:, j] != 0)]
    assert stats["p99"][j] == pytest.approx(np.percentile(nz, 99), rel=1e-5)


def test_a_layer_can_become_x(tmp_path: Path) -> None:
    src = tmp_path / "src.h5ad"
    a = source(src)
    fp = inspect_path(src)
    r = convert_anndata(
        src, fp, tmp_path / "cache", ConvertOptions(x_source="layers/counts"), lambda s, f: None
    )
    b = ad.read_zarr(r.store / "tables" / "table")
    np.testing.assert_array_equal(dense(b.X), dense(a.layers["counts"]))
    assert r.manifest.tables["table"].x_source == "layers/counts"


def test_duplicate_entries_are_refused_and_nothing_is_left(tmp_path: Path) -> None:
    src = tmp_path / "dup.h5ad"
    m = sparse.csc_matrix(
        (np.ones(3, np.float32), np.array([1, 1, 0]), np.array([0, 2, 3])), shape=(4, 2)
    )
    ad.AnnData(X=m).write_h5ad(src)
    fp = inspect_path(src)
    with pytest.raises(ConversionError) as e:
        convert_anndata(src, fp, tmp_path / "cache", ConvertOptions(), lambda s, f: None)
    assert e.value.code == "duplicate_entries"
    assert not any((tmp_path / "cache" / fp.dataset_id).iterdir())  # the tmp store is gone


def test_a_stop_during_conversion_removes_the_partial_store(tmp_path: Path) -> None:
    src = tmp_path / "src.h5ad"
    source(src)
    fp = inspect_path(src)

    class Stop(BaseException):
        pass

    def progress(stage: str, f: float) -> None:
        if stage == "write":
            raise Stop

    with pytest.raises(Stop):
        convert_anndata(src, fp, tmp_path / "cache", ConvertOptions(memory_bytes=200_000), progress)
    assert not any((tmp_path / "cache" / fp.dataset_id).iterdir())
    assert not has_store(tmp_path / "cache", fp.dataset_id)


def test_downsampling_keeps_every_group_and_the_exact_size() -> None:
    groups = np.array([0] * 9000 + [1] * 990 + [2] * 10 + [-1] * 3)
    idx = downsample_indices(len(groups), 500, groups)
    assert len(idx) == 500 and len(np.unique(idx)) == 500 and np.all(np.diff(idx) > 0)
    picked = groups[idx]
    assert {0, 1, 2, -1} <= set(picked.tolist())  # even the 3 unlabelled cells are represented
    assert 420 < (picked == 0).sum() < 480  # roughly proportional
    assert len(downsample_indices(1000, 100, None)) == 100


# ---------------------------------------------------------------- jobs through the API


def _wait(c: TestClient, job: int, app: object, timeout: float = 90) -> dict[str, object]:
    jm = app.state.jobs  # type: ignore[attr-defined]
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        jm.tick()
        j = c.get(f"/api/jobs/{job}").json()
        if j["state"] not in ("queued", "running"):
            while jm.procs and time.monotonic() - t0 < timeout:  # until the worker has exited
                time.sleep(0.05)
                jm.tick()
            return j  # type: ignore[no-any-return]
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_convert_job_end_to_end(cfg: Config) -> None:
    a = source(cfg.data_roots[0].path / "big.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "big.h5ad")
    before = c.get(f"/api/datasets/{id}/meta").json()
    assert before["mode"] == "lazy" and before["matrices"][0]["access"] == "memory"
    job = c.post(f"/api/datasets/{id}/convert", json={}).json()
    assert job["state"] == "queued" and "source" not in job
    # the same request again returns the same job
    assert c.post(f"/api/datasets/{id}/convert", json={}).json()["id"] == job["id"]
    done = _wait(c, job["id"], c.app)
    assert done["state"] == "done", done
    assert done["result"]["nnz"] > 0 and done["result"]["peak_rss_gb"] > 0
    detail = c.get(f"/api/catalog/datasets/{id}").json()
    assert detail["conversion"]["converted"] and detail["conversion"]["job"]["id"] == job["id"]
    after = c.get(f"/api/datasets/{id}/meta").json()
    assert after["mode"] == "converted"
    assert [(m["name"], m["encoding"], m["access"]) for m in after["matrices"]] == [
        ("X", "csc", "direct")
    ]
    r = c.get(f"/api/datasets/{id}/gene", params={"i": 9})
    np.testing.assert_array_equal(decode(r.content, dict(r.headers)), dense(a.X)[:, 9])
    obs = c.get(f"/api/datasets/{id}/obs", params={"column": "cluster"})
    assert obs.status_code == 200
    events = [e["event"] for e in log_events()]
    assert "job_started" in events and "dataset_converted" in events


def test_queued_jobs_can_be_cancelled_and_wait_for_memory(cfg: Config) -> None:
    cfg.jobs = JobsConfig(memory_gb=1e6)  # more than any host has: the guard keeps it queued
    source(cfg.data_roots[0].path / "m.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    job = c.post(f"/api/datasets/{id}/convert", json={"x_source": "layers/counts"}).json()
    c.app.state.jobs.tick()  # type: ignore[attr-defined]
    assert c.get(f"/api/jobs/{job['id']}").json()["stage"] == "waiting_for_memory"
    assert any(e["event"] == "job_waiting_for_memory" for e in log_events())
    cancelled = c.post(f"/api/jobs/{job['id']}/cancel", json={}).json()
    assert cancelled["state"] == "cancelled"
    assert c.post(f"/api/datasets/{id}/convert", json={"x_source": "bad"}).status_code == 422


def test_jobs_of_a_vanished_process_are_marked_interrupted(cfg: Config, tmp_path: Path) -> None:
    db = JobsDB(cfg.cache_dir / DB_FILE, wal=False)
    id = db.add("convert", "d", "v", {}, "/x", "{}")
    db.update(id, state="running", pid=2**22 + 12345)  # no such process
    client_for(cfg)  # a new server starts
    j = db.get(id)
    assert j is not None and j.state == "interrupted"


def test_a_failing_job_records_its_reason(cfg: Config) -> None:
    src = cfg.data_roots[0].path / "dup.h5ad"
    m = sparse.csc_matrix(
        (np.ones(3, np.float32), np.array([1, 1, 0]), np.array([0, 2, 3])), shape=(4, 2)
    )
    ad.AnnData(X=m).write_h5ad(src)
    c = client_for(cfg)
    job = c.post(f"/api/datasets/{entry_id('data', 'dup.h5ad')}/convert", json={}).json()
    done = _wait(c, job["id"], c.app)
    assert (done["state"], done["error"]) == ("failed", "duplicate_entries")
    # the worker wrote its own log file, with the reason
    worker_logs = list((cfg.logging.dir or Path()).glob("dbx-worker-*.jsonl"))
    assert worker_logs
    events = [json.loads(line) for f in worker_logs for line in f.read_text().splitlines()]
    assert any(e["event"] == "job_failed" and e["code"] == "duplicate_entries" for e in events)
