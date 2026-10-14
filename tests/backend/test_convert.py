"""The AnnData converter (h5ad / AnnData-zarr → canopy store) and the job system that runs it."""

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

from canopy.catalog import entry_id
from canopy.config import Config
from canopy.contract import validate_store
from canopy.convert.anndata import (
    ConvertOptions,
    convert_anndata,
    downsample_indices,
    has_store,
    store_dir,
)
from canopy.convert.matrix import ConversionError
from canopy.data.genes import decode
from canopy.inspect import inspect_path
from canopy.jobs.db import DB_FILE, JobsDB

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
    stats = b.uns["canopy"]["gene_stats"]
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
    # X from the store; the source's layer stays available as before (served from the file)
    assert [(m["name"], m["encoding"], m["access"], m["converted"]) for m in after["matrices"]] == [
        ("X", "csc", "direct", True),
        ("counts", "csr", "memory", False),
    ]
    rc = c.get(f"/api/datasets/{id}/gene", params={"i": 4, "matrix": "counts"})
    np.testing.assert_array_equal(
        decode(rc.content, dict(rc.headers)), dense(a.layers["counts"])[:, 4]
    )
    r = c.get(f"/api/datasets/{id}/gene", params={"i": 9})
    np.testing.assert_array_equal(decode(r.content, dict(r.headers)), dense(a.X)[:, 9])
    obs = c.get(f"/api/datasets/{id}/obs", params={"column": "cluster"})
    assert obs.status_code == 200
    events = [e["event"] for e in log_events()]
    assert "job_started" in events and "dataset_converted" in events


def test_queued_jobs_can_be_cancelled_and_wait_for_memory(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canopy.jobs import manager

    monkeypatch.setattr(manager, "memory_headroom", lambda: 2**20)  # 1 MB free just now
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
    worker_logs = list((cfg.logging.dir or Path()).glob("canopy-worker-*.jsonl"))
    assert worker_logs
    events = [json.loads(line) for f in worker_logs for line in f.read_text().splitlines()]
    assert any(e["event"] == "job_failed" and e["code"] == "duplicate_entries" for e in events)


def test_subset_endpoint_is_stratified_cached_and_sorted(cfg: Config) -> None:
    rng = np.random.default_rng(0)
    n = 5000
    a = ad.AnnData(X=sparse.csr_matrix((n, 3), dtype=np.float32))
    a.obs["type"] = pd.Categorical(np.where(np.arange(n) < 4990, "common", "rare"))
    a.uns["type_colors"] = ["#111111", "#222222"]
    a.obsm["X_umap"] = rng.random((n, 2), dtype=np.float32)
    a.write_h5ad(cfg.data_roots[0].path / "s.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "s.h5ad")
    r = c.get(f"/api/datasets/{id}/subset", params={"n": 1000})
    idx = np.frombuffer(r.content, dtype="<i4")
    assert r.headers["x-canopy-dtype"] == "int32" and len(idx) == 1000
    assert np.all(np.diff(idx) > 0) and idx[-1] < n
    assert (idx >= 4990).any()  # the 10 rare cells keep at least one
    again = c.get(
        f"/api/datasets/{id}/subset",
        params={"n": 1000},
        headers={"if-none-match": r.headers["etag"]},
    )
    assert again.status_code == 304
    assert (
        len(np.frombuffer(c.get(f"/api/datasets/{id}/subset", params={"n": 9000}).content, "<i4"))
        == n
    )


def test_a_store_subset_is_read_from_the_store(tmp_path: Path) -> None:
    import zarr

    from canopy.data.reader import LazySource

    g = zarr.open_group(tmp_path / "t.zarr", mode="w")
    g.require_group("uns").require_group("canopy").require_group("downsample").create_array(
        "1000", data=np.arange(0, 2000, 2, dtype=np.int32)
    )
    src = LazySource(inspect_path, g)  # type: ignore[arg-type]  # info is not used here
    got = src.stored_subset(1000)
    assert got is not None and got[1] == 2
    assert src.stored_subset(5) is None


# ---------------------------------------------------------------- review S5b fixes (R5)


def test_strict_job_logs_name_no_layer(cfg: Config) -> None:
    from canopy.config import LoggingConfig
    from canopy.logs import setup_logging

    cfg.logging = LoggingConfig(dir=cfg.logging.dir, privacy="strict", memory_sample_interval=0)
    setup_logging(cfg.logging, role="test", salt="s", to_console=False)
    a = source(cfg.data_roots[0].path / "m.h5ad")
    a.layers["SECRETLAYER"] = a.layers["counts"]
    a.write_h5ad(cfg.data_roots[0].path / "m.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    job = c.post(f"/api/datasets/{id}/convert", json={"x_source": "layers/SECRETLAYER"}).json()
    assert _wait(c, job["id"], c.app)["state"] == "done"
    text = "".join(f.read_text() for f in (cfg.logging.dir or Path()).glob("*.jsonl"))
    assert "SECRETLAYER" not in text and "job_done" in text


def test_leftovers_of_dead_jobs_are_removed_and_disk_is_checked(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from canopy.convert import anndata as conv
    from canopy.jobs.manager import JobManager

    stale = cfg.cache_dir / "v1" / "c0.tmp-j99-abcd"
    spill = cfg.cache_dir / "tmp" / "c0.tmp-j99-abcd"
    for d in (stale, spill):
        d.mkdir(parents=True)
        (d / "bucket-00000").write_bytes(b"x" * 1000)
    db = JobsDB(cfg.cache_dir / DB_FILE, wal=False)
    live = db.add("convert", "d", "v2", {}, "/x", "{}")
    db.claim(live, os.getpid())  # this test process plays a running worker
    kept = cfg.cache_dir / "v2" / f"c0.tmp-j{live}-ffff"
    kept.mkdir(parents=True)
    JobManager(cfg, wal=False)
    assert not stale.exists() and not spill.exists() and kept.exists()
    assert any(e["event"] == "tmp_removed" for e in log_events())
    # not enough disk: refused before anything is written
    src = tmp_path / "src.h5ad"
    source(src)
    monkeypatch.setattr(conv.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(ConversionError) as e:
        convert_anndata(
            src, inspect_path(src), tmp_path / "cache", ConvertOptions(), lambda s, f: None
        )
    assert e.value.code == "not_enough_disk"


def test_dead_or_silent_workers_are_interrupted_and_stopped_jobs_requeued(cfg: Config) -> None:
    import os

    db = JobsDB(cfg.cache_dir / DB_FILE, wal=False)
    a = db.add("convert", "d", "v", {}, "/x", "{}")
    db.claim(a, os.getpid())
    db.update(a, heartbeat=time.time() - 3600)  # alive but silent for an hour
    b = db.add("convert", "d2", "v2", {}, "/x", "{}")
    db.claim(b, os.getpid())
    db.update(b, pid_start=1.0)  # the pid now belongs to another process
    c = db.add("convert", "d3", "v3", {}, "/x", "{}")
    db.update(c, state="interrupted", error="server_stopped")
    # the silent one lives: it is not marked (its files stay), the manager stops it instead
    assert db.mark_interrupted() == [b] and db.get(b).error == "interrupted"  # type: ignore[union-attr]
    assert [j.id for j in db.silent()] == [a] and db.get(a).state == "running"  # type: ignore[union-attr]
    assert db.requeue_stopped() == [c] and db.get(c).state == "queued"  # type: ignore[union-attr]


def test_silent_workers_are_stopped_then_marked(cfg: Config) -> None:
    import subprocess
    import sys

    from canopy.jobs.manager import JobManager

    w = subprocess.Popen(  # a "worker" that hangs
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        jm = JobManager(cfg, wal=False)
        a = jm.db.add("convert", "d", "v", {}, "/x", "{}")
        jm.db.claim(a, w.pid)
        jm.db.update(a, heartbeat=time.time() - 3600)
        jm.adopted.clear()  # (claimed after start: not adopted)
        jm.tick()
        assert jm.db.get(a).stop_reason == "no_heartbeat"  # type: ignore[union-attr]
        w.wait(timeout=10)  # SIGTERM to its process group
        jm.tick()
        assert jm.db.get(a).state == "interrupted" and jm.db.get(a).error == "no_heartbeat"  # type: ignore[union-attr]
    finally:
        w.kill()


def test_workers_of_a_crashed_server_are_adopted(cfg: Config) -> None:
    import subprocess
    import sys

    from canopy.jobs.manager import _LOCKS, JobManager

    w = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        db = JobsDB(cfg.state / DB_FILE, wal=False)
        a = db.add("convert", "d", "v", {}, "/x", "{}")
        db.claim(a, w.pid)  # running, heartbeat now: a worker of an earlier server
        b = db.add("convert", "d2", "v2", {}, "/x", "{}")
        _LOCKS.clear()
        jm = JobManager(cfg, wal=False)  # the restarted server
        assert a in jm.adopted and jm.reserved_bytes() > 0
        assert any(e["event"] == "job_adopted" for e in log_events())
        jm.tick()  # max_running = 1: the adopted job counts, b waits
        assert jm.db.get(b).state == "queued" and not jm.procs  # type: ignore[union-attr]
        w.kill()
        w.wait()
        jm.tick()
        assert a not in jm.adopted
    finally:
        w.kill()


def test_cache_and_jobs_share_one_memory_budget(cfg: Config) -> None:
    from canopy.config import MemoryConfig
    from canopy.jobs.manager import JobManager

    cfg.memory = MemoryConfig(budget_gb=1.0, cache_gb=0.5)
    jm = JobManager(cfg, wal=False)
    source(cfg.data_roots[0].path / "m.h5ad")
    a = jm.db.add("convert", "d", "v", {}, "/x", "{}")
    jm.cache_bytes = lambda: int(0.9 * 2**30)  # the cache holds most of the budget
    jm.tick()
    assert jm.db.get(a).stage == "waiting_for_memory"  # type: ignore[union-attr]
    cfg.jobs.memory_gb = 2.0  # 1.5 × 2 GB can never fit a 1 GB budget
    jm.tick()
    assert jm.db.get(a).state == "failed" and jm.db.get(a).error == "not_enough_memory"  # type: ignore[union-attr]


def test_one_server_per_cache(cfg: Config) -> None:
    import subprocess
    import sys

    from canopy.jobs.manager import lock_cache

    lock_cache(cfg.cache_dir)
    code = (
        "import sys; from pathlib import Path\n"
        "from canopy.jobs.manager import lock_cache, CacheInUseError\n"
        "try:\n lock_cache(Path(sys.argv[1]))\nexcept CacheInUseError:\n sys.exit(7)"
    )
    r = subprocess.run([sys.executable, "-c", code, str(cfg.cache_dir)], check=False)
    assert r.returncode == 7


def test_a_converted_dataset_is_not_converted_again_unless_forced(cfg: Config) -> None:
    source(cfg.data_roots[0].path / "m.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    first = c.post(f"/api/datasets/{id}/convert", json={}).json()
    other = c.post(f"/api/datasets/{id}/convert", json={"x_source": "layers/counts"})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "conversion_running"
    assert _wait(c, first["id"], c.app)["state"] == "done"
    again = c.post(f"/api/datasets/{id}/convert", json={})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_converted"
    forced = c.post(
        f"/api/datasets/{id}/convert", json={"x_source": "layers/counts", "force": True}
    ).json()
    assert _wait(c, forced["id"], c.app)["state"] == "done"
    detail = c.get(f"/api/catalog/datasets/{id}").json()["conversion"]
    assert detail["converted"] and detail["x_source"] == "layers/counts"
    # the new store is described (counts converted; X from the source file)
    names = {
        m["name"]: m["converted"] for m in c.get(f"/api/datasets/{id}/meta").json()["matrices"]
    }
    assert names == {"counts": True, "X": False}


def test_raw_x_uses_raw_var_and_strings_stay_strings(tmp_path: Path) -> None:
    n, g = 50, 8
    rng = np.random.default_rng(0)
    a = ad.AnnData(X=sparse.csr_matrix(rng.random((n, g), dtype=np.float32)))
    a.var_names = [f"g{i}" for i in range(g)]
    a.raw = ad.AnnData(
        X=sparse.csr_matrix(rng.random((n, g), dtype=np.float32)),
        var=pd.DataFrame(index=[f"g{i}" for i in reversed(range(g))]),
    )
    a.obs["text"] = [["y", "x"][i % 2] for i in range(n)]
    src = tmp_path / "raw.h5ad"
    a.write_h5ad(src, convert_strings_to_categoricals=False)
    r = convert_anndata(
        src,
        inspect_path(src),
        tmp_path / "cache",
        ConvertOptions(x_source="raw/X"),
        lambda s, f: None,
    )
    b = ad.read_zarr(r.store / "tables" / "table")
    assert list(b.var_names) == [f"g{i}" for i in reversed(range(g))]
    np.testing.assert_array_equal(dense(b.X), dense(a.raw.X))
    assert not isinstance(b.obs["text"].dtype, pd.CategoricalDtype)
    assert "text" not in r.manifest.tables["table"].categorical


def test_conversion_warns_about_non_finite_and_large_integers(tmp_path: Path) -> None:
    x = np.zeros((40, 3), dtype=np.int64)
    x[0, 0] = 123_456_789
    a = ad.AnnData(X=sparse.csr_matrix(x))
    src = tmp_path / "big.h5ad"
    a.write_h5ad(src)
    r = convert_anndata(
        src, inspect_path(src), tmp_path / "cache", ConvertOptions(), lambda s, f: None
    )
    assert "integers_above_2_24" in r.warnings
    y = np.zeros((40, 3), dtype=np.float32)
    y[1, 1] = np.nan
    src2 = tmp_path / "nan.h5ad"
    ad.AnnData(X=sparse.csr_matrix(y)).write_h5ad(src2)
    r2 = convert_anndata(
        src2, inspect_path(src2), tmp_path / "cache", ConvertOptions(), lambda s, f: None
    )
    assert "non_finite_values:1" in r2.warnings


def test_a_source_changed_since_inspection_is_refused(tmp_path: Path) -> None:
    import os

    src = tmp_path / "src.h5ad"
    source(src)
    fp = inspect_path(src)
    os.utime(src, (time.time() + 5, time.time() + 5))
    with pytest.raises(ConversionError) as e:
        convert_anndata(src, fp, tmp_path / "cache", ConvertOptions(), lambda s, f: None)
    assert e.value.code == "source_changed"


def test_downsampling_with_more_groups_than_cells_to_keep() -> None:
    groups = np.repeat(np.arange(100), 3)
    idx = downsample_indices(len(groups), 64, groups)
    assert len(idx) == 64 and len(np.unique(groups[idx])) == 64
    sparse_codes = np.concatenate([np.zeros(10, int), np.full(5, 7)])  # categories 1–6 unused
    assert len(downsample_indices(len(sparse_codes), 1, sparse_codes)) == 1
    assert len(downsample_indices(len(sparse_codes), 12, sparse_codes)) == 12


def test_cache_prune_removes_stores_of_unknown_versions(cfg: Config) -> None:
    from typer.testing import CliRunner

    from canopy.cli import app as cli

    source(cfg.data_roots[0].path / "m.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "m.h5ad")
    job = c.post(f"/api/datasets/{id}/convert", json={}).json()
    assert _wait(c, job["id"], c.app)["state"] == "done"
    old = cfg.cache_dir / "0123456789abcdef" / "c0"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{}")
    conf = cfg.cache_dir.parent / "canopy.toml"
    conf.write_text(
        f'cache_dir = "{cfg.cache_dir}"\n[[data_roots]]\nname = "data"\n'
        f'path = "{cfg.data_roots[0].path}"\n[logging]\ndir = "{cfg.logging.dir}"\n'
    )
    # the test's own server holds the cache lock in this process, so prune may run here
    version = c.get(f"/api/catalog/datasets/{id}").json()["version"]
    # a dry run lists every copy: kept (and for which dataset) or to remove (host test 3)
    out = CliRunner().invoke(cli, ["cache", "prune", "--dry-run", "--config", str(conf)])
    assert out.exit_code == 0, out.output
    assert str(cfg.cache_dir) in out.output
    assert f"keep         {version}" in out.output and "data/m.h5ad" in out.output
    assert "would remove 0123456789abcdef" in out.output
    assert "1 converted copies kept" in out.output and "1 to remove" in out.output
    assert old.parent.exists()
    out = CliRunner().invoke(cli, ["cache", "prune", "--config", str(conf)])
    assert out.exit_code == 0, out.output
    assert not old.parent.exists()
    assert has_store(cfg.cache_dir, version)


def test_cache_prune_and_scan_need_the_installations_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a config they would look at a `cache` folder in the current directory, made on the
    spot, and report "would free 0.00 GB" (host test 3): they refuse instead."""
    from typer.testing import CliRunner

    from canopy.cli import app as cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CANOPY_CONFIG", raising=False)
    for args in (["cache", "prune", "--dry-run"], ["catalog", "scan"]):
        out = CliRunner().invoke(cli, args)
        assert out.exit_code == 2 and "--config" in out.output
    assert not (tmp_path / "cache").exists()
    conf = tmp_path / "c.toml"
    conf.write_text('cache_dir = "nowhere"\n')
    out = CliRunner().invoke(cli, ["cache", "prune", "--dry-run", "--config", str(conf)])
    assert out.exit_code == 1 and "does not exist" in out.output
    assert not (tmp_path / "nowhere").exists()


def test_a_job_larger_than_the_host_fails_and_does_not_block_others(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canopy.config import MemoryConfig
    from canopy.jobs import manager

    cfg.memory = MemoryConfig(budget_gb=1000, cache_gb=4)  # the host's memory decides here

    source(cfg.data_roots[0].path / "big.h5ad")
    source(cfg.data_roots[0].path / "small.h5ad")
    c = client_for(cfg)
    sizes = {entry_id("data", "big.h5ad"): 10**15, entry_id("data", "small.h5ad"): 0}
    monkeypatch.setattr(manager, "table_bytes", lambda job, threads=4: sizes[job.dataset])
    big = c.post(f"/api/datasets/{entry_id('data', 'big.h5ad')}/convert", json={}).json()
    small = c.post(f"/api/datasets/{entry_id('data', 'small.h5ad')}/convert", json={}).json()
    done = _wait(c, small["id"], c.app)
    assert done["state"] == "done"
    b = c.get(f"/api/jobs/{big['id']}").json()
    assert b["state"] == "failed" and b["error"] == "not_enough_memory"
    assert any(e["event"] == "job_needs_more_memory_than_allowed" for e in log_events())

    # one waiting for memory (too big just now) does not hold back a smaller one behind it
    sizes[entry_id("data", "big.h5ad")] = 2**33
    monkeypatch.setattr(manager, "memory_headroom", lambda: 2**32)
    monkeypatch.setattr(manager, "memory_total", lambda: 2**40)
    again = c.post(
        f"/api/datasets/{entry_id('data', 'big.h5ad')}/convert", json={"x_source": "layers/counts"}
    ).json()
    sizes[entry_id("data", "small.h5ad")] = 0
    small2 = c.post(
        f"/api/datasets/{entry_id('data', 'small.h5ad')}/convert",
        json={"x_source": "layers/counts", "force": True},
    ).json()
    assert _wait(c, small2["id"], c.app)["state"] == "done"
    assert c.get(f"/api/jobs/{again['id']}").json()["stage"] == "waiting_for_memory"


def test_queue_limit_and_stores_cap(cfg: Config) -> None:
    from canopy.jobs.manager import JobManager, QueueFullError

    cfg.jobs.max_queued = 1
    jm = JobManager(cfg, wal=False)
    jm.submit("convert", "d1", "v1", {}, Path("/x"), "{}")
    with pytest.raises(QueueFullError):
        jm.submit("convert", "d2", "v2", {}, Path("/x"), "{}")

    # converted copies already fill the cap: the next conversion fails with stores_full
    cfg.jobs.max_queued = 10
    cfg.jobs.stores_max_gb = 1e-6
    big = cfg.cache_dir / "0123456789abcdef" / "c0"
    big.mkdir(parents=True)
    (big / "x.bin").write_bytes(b"0" * 10_000)
    source(cfg.data_roots[0].path / "m.h5ad")
    c = client_for(cfg)
    job = c.post(f"/api/datasets/{entry_id('data', 'm.h5ad')}/convert", json={}).json()
    c.app.state.jobs.tick()  # type: ignore[attr-defined]
    j = c.get(f"/api/jobs/{job['id']}").json()
    assert j["state"] == "failed" and j["error"] == "stores_full"
