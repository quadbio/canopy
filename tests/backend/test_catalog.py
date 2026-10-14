from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import anndata as ad
import numpy as np
import pytest
from conftest import CACHE, EXAMPLES, log_events, missing_example
from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.catalog import Catalog, CatalogDB, PathEscapeError, entry_id, scan
from canopy.catalog.db import DB_FILE
from canopy.config import CatalogConfig, Config, DataRoot


def tiny_h5ad(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    a = ad.AnnData(X=np.arange(15, dtype=np.float32).reshape(5, 3))
    a.obsm["X_umap"] = np.zeros((5, 2), np.float32)
    a.write_h5ad(path)
    return path


def _db(cfg: Config) -> CatalogDB:
    return CatalogDB(cfg.cache_dir / DB_FILE)


def _scan(cfg: Config, **kw: object) -> dict[str, object]:
    return scan(_db(cfg), cfg.data_roots, cfg.catalog, **kw).as_dict()  # type: ignore[arg-type]


def _paths(cfg: Config) -> dict[str, str]:
    with _db(cfg).connect() as con:
        return {
            r["relpath"]: r["state"] for r in con.execute("SELECT relpath, state FROM datasets")
        }


@pytest.fixture
def data(cfg: Config) -> Path:
    return cfg.data_roots[0].path


# ---------------------------------------------------------------- walking rules


def test_finds_files_and_stores_but_not_inside_stores(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "a.h5ad")
    tiny_h5ad(data / "sub" / "deeper" / "B.H5AD")
    store = data / "sub" / "s.zarr"
    ad.AnnData(X=np.ones((4, 2), np.float32)).write_zarr(store)
    tiny_h5ad(store / "inside.h5ad")  # never descended into
    tiny_h5ad(data / ".hidden" / "h.h5ad")
    tiny_h5ad(data / "tmp_work" / "t.h5ad")
    (data / "notes.txt").write_text("x")
    (data / "fake.h5ad").write_text("not hdf5")
    cfg.catalog = CatalogConfig(ignore=["tmp_*"])
    counts = _scan(cfg)
    assert _paths(cfg) == {
        "a.h5ad": "inspected",
        "sub/deeper/B.H5AD": "inspected",
        "sub/s.zarr": "inspected",
        "fake.h5ad": "unsupported",
    }
    assert counts["new"] == 4 and counts["walk"]["ignored"] == 1  # type: ignore[index]
    row = _db(cfg).get(entry_id("data", "sub/s.zarr"))
    assert row is not None and row.kind == "anndata_zarr" and row.folder == "sub"
    assert row.summary["n_obs"] == 4


def test_symlinks_outside_root_and_loops_are_skipped(
    cfg: Config, data: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    tiny_h5ad(outside / "secret.h5ad")
    tiny_h5ad(data / "real" / "r.h5ad")
    (data / "escape_dir").symlink_to(outside)
    (data / "escape_file.h5ad").symlink_to(outside / "secret.h5ad")
    (data / "real" / "loop").symlink_to(data)  # inside the root: followed once, no loop
    (data / "alias.h5ad").symlink_to(data / "real" / "r.h5ad")  # inside: indexed
    counts = _scan(cfg)
    assert set(_paths(cfg)) == {"real/r.h5ad", "alias.h5ad"}
    assert counts["walk"]["links_outside"] == 2  # type: ignore[index]


def test_failed_inspection_is_recorded_without_names(cfg: Config, data: Path) -> None:
    (data / "broken_patient7.h5ad").write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 100)
    counts = _scan(cfg)
    row = _db(cfg).get(entry_id("data", "broken_patient7.h5ad"))
    assert row is not None and row.state == "failed" and row.reason == "error:OSError"
    assert counts["failed"] == 1
    ev = next(e for e in log_events() if e["event"] == "inspect_failed")
    assert ev["exc_type"] == "OSError" and ev["reason"] == "error:OSError"


# ---------------------------------------------------------------- incremental scans


def test_rescans_are_incremental(cfg: Config, data: Path) -> None:
    for i in range(5):
        tiny_h5ad(data / f"d{i}" / f"f{i}.h5ad")
    store = data / "s.zarr"
    ad.AnnData(X=np.ones((4, 2), np.float32)).write_zarr(store)
    assert _scan(cfg)["new"] == 6

    before = len(log_events())
    second = _scan(cfg)
    assert second["unchanged"] == 6 and second["new"] == second["changed"] == 0
    assert not [e for e in log_events()[before:] if e["event"] == "dataset_inspected"]

    later = time.time() + 5
    os.utime(data / "d0" / "f0.h5ad", (later, later))
    (data / "d1" / "f1.h5ad").unlink()
    tiny_h5ad(data / "new.h5ad")
    ad.AnnData(X=np.ones((6, 2), np.float32)).write_zarr(store)  # rewritten store
    third = _scan(cfg)
    assert (third["changed"], third["removed"], third["new"], third["unchanged"]) == (2, 1, 1, 3)
    row = _db(cfg).get(entry_id("data", "s.zarr"))
    assert row is not None and row.summary["n_obs"] == 6

    assert _scan(cfg, full=True)["changed"] == 6


def test_a_moved_root_keeps_its_versions(cfg: Config, data: Path, tmp_path: Path) -> None:
    """A dataset's version is its root's name, its path in the root and its stamp (owner, host
    test 3): moving or remounting the whole data folder keeps every version, so no conversion
    is lost. (Before, the real path was part of it, and the S7b rename made every copy stale.)"""
    store = data / "s.zarr"
    ad.AnnData(X=np.ones((4, 2), np.float32)).write_zarr(store)
    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    before = _versions(cfg)
    moved = tmp_path / "moved_root"
    shutil.move(str(data), moved)
    cfg.data_roots[0].path = moved
    assert _scan(cfg)["unchanged"] == 2
    assert _versions(cfg) == before


def _versions(cfg: Config) -> dict[str, str]:
    with _db(cfg).connect() as con:
        return {
            x["relpath"]: x["version"] for x in con.execute("SELECT relpath, version FROM datasets")
        }


def test_an_index_of_an_earlier_release_keeps_its_converted_copies(cfg: Config, data: Path) -> None:
    """Versions written by an earlier release (real paths) are inspected once more; a dataset whose
    content did not change takes its converted copy along to the new version."""
    from canopy.inspect.core import dataset_id, version_stamp

    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    new = _versions(cfg)["a.h5ad"]
    old = dataset_id(data / "a.h5ad", version_stamp(data / "a.h5ad"))  # the earlier rule
    assert old != new
    with _db(cfg).connect() as con:
        con.execute("UPDATE datasets SET version=?", (old,))
    copy = cfg.cache_dir / old / "c0"
    copy.mkdir(parents=True)
    (copy / "manifest.json").write_text(json.dumps({"dataset_id": old}))
    db = _db(cfg)
    counts = scan(db, cfg.data_roots, cfg.catalog, cache_dir=cfg.cache_dir)
    assert counts.changed == 1 and counts.adopted == 1
    assert _versions(cfg)["a.h5ad"] == new
    assert not (cfg.cache_dir / old).exists()
    manifest = json.loads((cfg.cache_dir / new / "c0" / "manifest.json").read_text())
    assert manifest["dataset_id"] == new
    assert _scan(cfg)["unchanged"] == 1  # and then it is settled


def test_unavailable_root_keeps_its_entries(cfg: Config, data: Path, tmp_path: Path) -> None:
    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    moved = tmp_path / "moved"
    data.rename(moved)  # e.g. the share is not mounted
    counts = _scan(cfg)
    assert counts["roots_unavailable"] == 1 and counts["removed"] == 0
    assert set(_paths(cfg)) == {"a.h5ad"}
    moved.rename(data)


def test_removed_root_is_dropped(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    cfg.data_roots = []
    assert _scan(cfg)["removed"] == 1 and _paths(cfg) == {}


def test_synthetic_tree_of_10k_files(cfg: Config, data: Path) -> None:
    proto = tiny_h5ad(cfg.cache_dir.parent / "proto.h5ad")
    n_dirs, junk_per_dir, h5_every = 100, 97, 1
    for d in range(n_dirs):
        folder = data / f"project_{d // 10}" / f"run_{d}"
        folder.mkdir(parents=True)
        for j in range(junk_per_dir):
            (folder / f"file_{j}.csv").write_text("")
        for k in range(3 * h5_every):
            shutil.copyfile(proto, folder / f"sample_{k}.h5ad")
    n_files = sum(len(f) for _, _, f in os.walk(data))
    assert n_files >= 10_000

    t0 = time.perf_counter()
    first = _scan(cfg)
    t1 = time.perf_counter()
    second = _scan(cfg)
    t2 = time.perf_counter()
    assert first["new"] == 300 and first["failed"] == 0
    assert second["unchanged"] == 300
    assert t2 - t1 < (t1 - t0) / 2  # no inspection on the rescan
    rows, sub = _db(cfg).folder("data", "project_3")
    assert rows == [] and len(sub) == 10 and set(sub.values()) == {3}


# ---------------------------------------------------------------- resolve and path safety


def test_resolve_rechecks_the_root(cfg: Config, data: Path, tmp_path: Path) -> None:
    tiny_h5ad(data / "inner" / "a.h5ad")
    cat = Catalog(cfg)
    cat.scan_now()
    row = cat.get(entry_id("data", "inner/a.h5ad"))
    assert row is not None
    assert cat.resolve(row) == (data / "inner" / "a.h5ad").resolve()
    # The folder is swapped for a symlink to a look-alike outside the root after indexing.
    outside = tmp_path / "elsewhere"
    tiny_h5ad(outside / "a.h5ad")
    shutil.rmtree(data / "inner")
    (data / "inner").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        cat.resolve(row)
    assert any(e["event"] == "path_escape" for e in log_events())


# ---------------------------------------------------------------- API


@pytest.fixture
def client(cfg: Config, data: Path) -> TestClient:
    tiny_h5ad(data / "top.h5ad")
    tiny_h5ad(data / "proj" / "x" / "one.h5ad")
    tiny_h5ad(data / "proj" / "x" / "two_100%_.h5ad")
    tiny_h5ad(data / "proj" / "y" / "three.h5ad")
    app = create_app(cfg, background=False)
    app.state.catalog.scan_now()
    return TestClient(app)


def test_api_roots_and_tree(client: TestClient) -> None:
    roots = client.get("/api/catalog/roots").json()
    assert roots == [{"name": "data", "n_datasets": 4, "states": {"inspected": 4}}]
    top = client.get("/api/catalog/tree", params={"root": "data"}).json()
    assert [d["name"] for d in top["datasets"]] == ["top.h5ad"]
    assert top["folders"] == [{"name": "proj", "path": "proj", "n_datasets": 3}]
    proj = client.get("/api/catalog/tree", params={"root": "data", "path": "proj/"}).json()
    assert [(f["path"], f["n_datasets"]) for f in proj["folders"]] == [("proj/x", 2), ("proj/y", 1)]
    x = client.get("/api/catalog/tree", params={"root": "data", "path": "proj/x"}).json()
    d = x["datasets"][0]
    assert d["path"] == "proj/x/one.h5ad" and d["kind"] == "h5ad" and d["state"] == "inspected"
    assert d["summary"]["n_obs"] == 5 and d["summary"]["embeddings"] == ["X_umap"]
    assert "fingerprint" not in d


@pytest.mark.parametrize("path", ["..", "proj/../..", "proj/./x", "a\\b", "x\0y"])
def test_api_rejects_unsafe_folder_paths(client: TestClient, path: str) -> None:
    r = client.get("/api/catalog/tree", params={"root": "data", "path": path})
    assert r.status_code == 400


def test_api_unknown_root_folder_and_dataset(client: TestClient) -> None:
    assert client.get("/api/catalog/tree", params={"root": "nope"}).status_code == 404
    r = client.get("/api/catalog/tree", params={"root": "data", "path": "not/here"})
    assert r.status_code == 404
    assert client.get("/api/catalog/datasets/0123456789abcdef").status_code == 404


def test_api_search_escapes_wildcards(client: TestClient) -> None:
    names = [d["name"] for d in client.get("/api/catalog/search", params={"q": "100%_"}).json()]
    assert names == ["two_100%_.h5ad"]
    hits = client.get("/api/catalog/search", params={"q": "PROJ"}).json()
    assert len(hits) == 3
    assert client.get("/api/catalog/search", params={"q": ""}).status_code == 422


def test_api_dataset_detail_has_no_host_path(client: TestClient, data: Path) -> None:
    id = entry_id("data", "proj/x/one.h5ad")
    body = client.get(f"/api/catalog/datasets/{id}").json()
    assert body["fingerprint"]["path"] == "proj/x/one.h5ad"
    assert str(data) not in str(body)
    assert body["fingerprint"]["anndata"]["n_obs"] == 5


def test_api_status_and_refresh(client: TestClient) -> None:
    st = client.get("/api/catalog/status").json()
    assert st["scanning"] is False and st["last_scan"]["status"] == "done"
    assert st["last_scan"]["counts"]["new"] == 4
    r = client.post("/api/catalog/refresh", json={})
    assert r.status_code == 202 and r.json() == {"started": True}


def test_server_scans_in_background(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "a.h5ad")
    with TestClient(create_app(cfg)) as c:
        for _ in range(100):
            last = c.get("/api/catalog/status").json()["last_scan"]
            if last and last["status"] == "done":
                break
            time.sleep(0.05)
        assert last["trigger"] == "startup" and last["counts"]["new"] == 1
        assert c.post("/api/catalog/refresh", params={"full": True}, json={}).json()["started"]
        for _ in range(100):
            last = c.get("/api/catalog/status").json()["last_scan"]
            if last["trigger"] == "refresh" and last["status"] == "done":
                break
            time.sleep(0.05)
        assert last["full"] is True and last["counts"]["changed"] == 1


# ---------------------------------------------------------------- example data


def test_examples_are_indexed(cfg: Config, example: Callable[[str], Path]) -> None:
    if not (EXAMPLES / "spatial.h5ad").exists():
        missing_example("spatial.h5ad")
    for name in ("202302171205_region_1_scwf.zarr", "202302171205_region_3_scwf.zarr"):
        example(name)
    cfg.data_roots = [
        DataRoot(name="examples", path=EXAMPLES),
        DataRoot(name="extracted", path=CACHE),
    ]
    counts = _scan(cfg)
    assert counts["failed"] == 0
    states = _paths(cfg)
    assert states == {
        "dissociated.h5ad": "inspected",
        "spatial.h5ad": "inspected",
        "202302171205_region_1_scwf.zarr": "inspected",
        "202302171205_region_3_scwf.zarr": "inspected",
    }
    row = _db(cfg).get(entry_id("extracted", "202302171205_region_1_scwf.zarr"))
    assert row is not None and row.kind == "spatialdata_zarr" and row.summary["n_points"] >= 1


# ---------------------------------------------------------------- review R1 regressions


def test_truncated_and_non_anndata_files_get_their_own_reasons(cfg: Config, data: Path) -> None:
    import h5py

    whole = tiny_h5ad(data / "whole.h5ad").read_bytes()
    (data / "half.h5ad").write_bytes(whole[: len(whole) // 2])  # half-copied
    with h5py.File(data / "other.h5ad", "w") as f:  # HDF5, but not AnnData
        f["x"] = np.arange(3)
    _scan(cfg)
    db = _db(cfg)
    half = db.get(entry_id("data", "half.h5ad"))
    other = db.get(entry_id("data", "other.h5ad"))
    assert half is not None and (half.state, half.reason) == ("failed", "truncated")
    assert other is not None and (other.state, other.reason) == ("unsupported", "not_anndata")


def test_unreadable_folder_keeps_its_entries(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "ok" / "a.h5ad")
    tiny_h5ad(data / "locked" / "b.h5ad")
    _scan(cfg)
    (data / "locked").chmod(0o000)
    try:
        counts = _scan(cfg)
    finally:
        (data / "locked").chmod(0o755)
    assert counts["removed"] == 0 and counts["walk"]["unreadable"] == 1  # type: ignore[index]
    assert set(_paths(cfg)) == {"ok/a.h5ad", "locked/b.h5ad"}


def test_root_listing_empty_keeps_entries_until_full_scan(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    (data / "a.h5ad").unlink()  # looks like an unmounted share: the mount point is empty
    counts = _scan(cfg)
    assert counts["roots_unavailable"] == 1 and set(_paths(cfg)) == {"a.h5ad"}
    assert any(e["event"] == "root_empty_kept" for e in log_events())
    assert _scan(cfg, full=True)["removed"] == 1


def test_symlink_to_root_is_not_walked_twice(cfg: Config, data: Path) -> None:
    tiny_h5ad(data / "sub" / "a.h5ad")
    (data / "sub" / "up").symlink_to(data)
    (data / "alias").symlink_to(data / "sub")
    _scan(cfg)
    assert set(_paths(cfg)) == {"sub/a.h5ad", "alias/a.h5ad"}


def test_cli_scan_leaves_a_live_scan_alone(cfg: Config) -> None:
    import os

    db = _db(cfg)
    live = db.start_scan("startup", False)  # this (live) process owns it
    with db.connect() as con:
        con.execute(
            "INSERT INTO scans (trigger, full, started, status, pid) "
            "VALUES ('x', 0, 0, 'running', 999999)"
        )
    CatalogDB(cfg.cache_dir / DB_FILE)  # what another process does when it opens the index
    with db.connect() as con:
        rows = {r["id"]: (r["status"], r["pid"]) for r in con.execute("SELECT * FROM scans")}
    assert rows[live] == ("running", os.getpid())
    assert ("failed", 999999) in rows.values()


def test_scan_progress_heartbeat(cfg: Config, data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from canopy.catalog import crawl

    monkeypatch.setattr(crawl.Progress, "HEARTBEAT_S", 0.05)
    tiny_h5ad(data / "a.h5ad")
    real = crawl._check_one

    def slow(*a: object, **k: object) -> object:
        time.sleep(0.2)
        return real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(crawl, "_check_one", slow)
    _scan(cfg)
    beats = [e for e in log_events() if e["event"] == "scan_progress"]
    assert beats and any(b.get("oldest_path") == "a.h5ad" for b in beats)


def test_a_conversion_queued_under_the_old_id_follows_its_dataset(cfg: Config, data: Path) -> None:
    """A conversion requeued across the upgrade (earlier id) publishes under the new one
    (review full-2 A-L6)."""
    from canopy.inspect.core import dataset_id, version_stamp
    from canopy.jobs.db import DB_FILE as JOBS
    from canopy.jobs.db import JobsDB

    tiny_h5ad(data / "a.h5ad")
    _scan(cfg)
    new = _versions(cfg)["a.h5ad"]
    old = dataset_id(data / "a.h5ad", version_stamp(data / "a.h5ad"))
    with _db(cfg).connect() as con:
        con.execute("UPDATE datasets SET version=?", (old,))
    (cfg.cache_dir / old / "c0").mkdir(parents=True)
    (cfg.cache_dir / old / "c0" / "manifest.json").write_text("{}")
    jobs = JobsDB(cfg.cache_dir / JOBS)
    jid = jobs.add("convert", "d", old, {}, str(data / "a.h5ad"), json.dumps({"dataset_id": old}))
    counts = scan(
        _db(cfg), cfg.data_roots, cfg.catalog, cache_dir=cfg.cache_dir, state_dir=cfg.cache_dir
    )
    assert counts.adopted == 1, counts
    job = jobs.get(jid)
    assert (
        job is not None and job.version == new and json.loads(job.fingerprint)["dataset_id"] == new
    )
