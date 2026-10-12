from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from conftest import CACHE, EXAMPLES, log_events, missing_example
from fastapi.testclient import TestClient

from canopy.app import create_app
from canopy.catalog import entry_id
from canopy.config import Config, DataRoot, MemoryConfig

DTYPES = {"float32": "<f4", "int8": "i1", "int16": "<i2", "int32": "<i4", "uint8": "u1"}


def decode(r: object) -> np.ndarray:
    h = r.headers  # type: ignore[attr-defined]
    shape = tuple(int(n) for n in h["x-canopy-shape"].split(","))
    return np.frombuffer(r.content, dtype=DTYPES[h["x-canopy-dtype"]]).reshape(shape)  # type: ignore[attr-defined]


def tiny(path: Path) -> ad.AnnData:
    rng = np.random.default_rng(0)
    n = 300
    a = ad.AnnData(X=rng.random((n, 4), dtype=np.float32))
    a.obs["cluster"] = pd.Categorical(rng.choice(["b", "a", "c"], n), categories=["b", "a", "c"])
    a.obs["cluster"] = a.obs["cluster"].cat.add_categories(["unused"])
    a.obs.loc[a.obs.index[:3], "cluster"] = np.nan
    a.obs["score"] = rng.random(n)
    a.obs["count"] = np.arange(n)
    a.obs["flag"] = np.arange(n) % 3 == 0
    a.obs["label"] = [f"s{i % 5}" for i in range(n)]
    a.obs["nullable"] = pd.array([1, None, 3] * 100, dtype="Int64")
    a.uns["cluster_colors"] = ["#ff0000", "#00ff00", "#0000ff", "#123456"]
    a.obsm["X_umap"] = rng.random((n, 2)).astype(np.float64)
    a.obsm["X_pca"] = rng.random((n, 10), dtype=np.float32)
    a.obsm["one_col"] = rng.random((n, 1), dtype=np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".zarr":
        a.write_zarr(path)
    else:
        a.write_h5ad(path)
    return a


def client_for(cfg: Config) -> TestClient:
    app = create_app(cfg, background=False)
    app.state.catalog.scan_now()
    return TestClient(app)


@pytest.fixture(params=["h5ad", "zarr"])
def tiny_ds(request: pytest.FixtureRequest, cfg: Config) -> tuple[TestClient, str, ad.AnnData]:
    name = f"tiny.{request.param}"
    a = tiny(cfg.data_roots[0].path / name)
    return client_for(cfg), entry_id("data", name), a


# ---------------------------------------------------------------- golden: tiny files


def test_meta_describes_columns_colors_and_embeddings(
    tiny_ds: tuple[TestClient, str, ad.AnnData],
) -> None:
    c, id, a = tiny_ds
    m = c.get(f"/api/datasets/{id}/meta").json()
    assert (m["n_obs"], m["n_vars"], m["mode"], m["table"]) == (300, 4, "lazy", None)
    assert [e["key"] for e in m["embeddings"]] == ["X_umap", "X_pca"]  # X_umap first, 1-D left out
    assert m["embeddings"][1]["dims"] == 10
    cols = {col["name"]: col for col in m["obs"]}
    assert cols["cluster"]["categories"] == ["b", "a", "c", "unused"]
    assert cols["cluster"]["colors"] == list(a.uns["cluster_colors"])
    assert cols["score"]["kind"] == "numeric" and cols["score"]["categories"] is None


def test_obs_matches_anndata(tiny_ds: tuple[TestClient, str, ad.AnnData]) -> None:
    c, id, a = tiny_ds
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "cluster"})
    assert r.headers["x-canopy-kind"] == "categorical" and r.headers["x-canopy-dtype"] == "int8"
    np.testing.assert_array_equal(decode(r), a.obs["cluster"].cat.codes.to_numpy())
    score = decode(c.get(f"/api/datasets/{id}/obs", params={"column": "score"}))
    np.testing.assert_array_equal(score, a.obs["score"].to_numpy().astype(np.float32))
    count = decode(c.get(f"/api/datasets/{id}/obs", params={"column": "count"}))
    np.testing.assert_array_equal(count, np.arange(300, dtype=np.float32))
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "flag"})
    assert r.headers["x-canopy-kind"] == "boolean"
    np.testing.assert_array_equal(decode(r), a.obs["flag"].to_numpy().astype(np.uint8))
    nullable = decode(c.get(f"/api/datasets/{id}/obs", params={"column": "nullable"}))
    assert np.isnan(nullable[1]) and nullable[2] == 3


def test_string_column_is_served_as_sorted_categories(
    tiny_ds: tuple[TestClient, str, ad.AnnData],
) -> None:
    c, id, a = tiny_ds
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "label"})
    codes = decode(r)
    m = c.get(f"/api/datasets/{id}/meta").json()
    label = next(col for col in m["obs"] if col["name"] == "label")
    # anndata writes string columns as categoricals; plain string arrays: see
    # test_string_and_nullable_boolean_columns
    cats = label["categories"]
    assert [cats[i] for i in codes[:6]] == list(a.obs["label"][:6])


def test_obsm_matches_anndata_and_selects_dims(tiny_ds: tuple[TestClient, str, ad.AnnData]) -> None:
    c, id, a = tiny_ds
    r = c.get(f"/api/datasets/{id}/obsm", params={"key": "X_umap"})
    assert r.headers["x-canopy-dtype"] == "float32" and r.headers["x-canopy-shape"] == "300,2"
    np.testing.assert_array_equal(decode(r), a.obsm["X_umap"].astype(np.float32))
    r = c.get(f"/api/datasets/{id}/obsm", params={"key": "X_pca", "dims": "7,2"})
    np.testing.assert_array_equal(decode(r), a.obsm["X_pca"][:, [7, 2]])
    for dims in ("2,2", "0,10", "a,b"):
        bad = c.get(f"/api/datasets/{id}/obsm", params={"key": "X_pca", "dims": dims})
        assert bad.status_code in (400, 422), dims
    assert c.get(f"/api/datasets/{id}/obsm", params={"key": "one_col"}).status_code == 404


def test_errors_unknown(tiny_ds: tuple[TestClient, str, ad.AnnData]) -> None:
    c, id, _ = tiny_ds
    assert c.get("/api/datasets/0123456789abcdef/meta").status_code == 404
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "nope"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_element"
    assert c.get(f"/api/datasets/{id}/meta", params={"table": "x"}).status_code == 404


def test_etag_revalidation_and_gzip(tiny_ds: tuple[TestClient, str, ad.AnnData]) -> None:
    c, id, _ = tiny_ds
    url, q = f"/api/datasets/{id}/obsm", {"key": "X_pca", "dims": "0,1"}
    r = c.get(url, params=q, headers={"accept-encoding": "gzip"})
    # floats barely compress: sent as they are
    assert r.headers["content-encoding"] == "identity" and r.headers["etag"].startswith('W/"')
    again = c.get(url, params=q, headers={"if-none-match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    meta = c.get(f"/api/datasets/{id}/meta", headers={"accept-encoding": "gzip"})
    assert meta.headers["content-encoding"] == "gzip"  # JSON is (httpx decodes it)


def test_304_is_answered_without_reading(
    tiny_ds: tuple[TestClient, str, ad.AnnData], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, id, _ = tiny_ds
    url, q = f"/api/datasets/{id}/obs", {"column": "score"}
    etag = c.get(url, params=q).headers["etag"]
    c.app.state.datasets.clear()  # type: ignore[attr-defined]

    def no_read(*a: object, **k: object) -> None:
        raise AssertionError("read on revalidation")

    monkeypatch.setattr("canopy.data.reader.LazySource.obs", no_read)
    assert c.get(url, params=q, headers={"if-none-match": etag}).status_code == 304


def test_arrays_are_cached_and_logged(tiny_ds: tuple[TestClient, str, ad.AnnData]) -> None:
    c, id, _ = tiny_ds
    for _ in range(3):
        c.get(f"/api/datasets/{id}/obs", params={"column": "score"})
    loads = [e for e in log_events() if e["event"] == "cache_load"]
    assert [e["what"] for e in loads] == ["source", "array"]
    assert loads[1]["nbytes"] == 300 * 4
    assert any(e.get("stage") == "open_dataset" and e["mode"] == "lazy" for e in log_events())


def test_cache_budget_evicts_and_closed_sources_reopen(cfg: Config) -> None:
    cfg.memory = MemoryConfig(budget_gb=1, cache_gb=2000 / 2**30)  # ~2 kB
    tiny(cfg.data_roots[0].path / "t.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "t.h5ad")
    for col in ("score", "count", "score"):  # 1.2 kB each: every load evicts the previous one
        assert c.get(f"/api/datasets/{id}/obs", params={"column": col}).status_code == 200
    evictions = [e for e in log_events() if e["event"] == "cache_evict"]
    assert len(evictions) >= 2
    cache = c.app.state.datasets  # type: ignore[attr-defined]
    cache.clear()  # closes the open file, as an eviction during a request would
    assert c.get(f"/api/datasets/{id}/obs", params={"column": "score"}).status_code == 200


# ---------------------------------------------------------------- states and legacy files


def test_legacy_layout_is_loaded_fully_when_small(cfg: Config) -> None:
    from legacy_h5ad import N_OBS, write_v07

    write_v07(cfg.data_roots[0].path / "old.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "old.h5ad")
    m = c.get(f"/api/datasets/{id}/meta").json()
    assert m["mode"] == "memory" and m["n_obs"] == N_OBS
    leiden = next(col for col in m["obs"] if col["name"] == "leiden")
    assert leiden["categories"] == ["a", "b"]
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "leiden"})
    assert decode(r).shape == (N_OBS,)


def test_legacy_layout_too_large_needs_conversion(cfg: Config) -> None:
    from legacy_h5ad import write_v07

    cfg.memory = MemoryConfig(small_file_gb=1e-9)
    write_v07(cfg.data_roots[0].path / "old.h5ad")
    c = client_for(cfg)
    r = c.get(f"/api/datasets/{entry_id('data', 'old.h5ad')}/meta")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "needs_conversion:legacy_layout"


def test_unsupported_and_vanished(cfg: Config) -> None:
    data = cfg.data_roots[0].path
    (data / "fake.h5ad").write_text("x")
    tiny(data / "gone.h5ad")
    c = client_for(cfg)
    r = c.get(f"/api/datasets/{entry_id('data', 'fake.h5ad')}/meta")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "state:unsupported"
    (data / "gone.h5ad").unlink()
    r = c.get(f"/api/datasets/{entry_id('data', 'gone.h5ad')}/meta")
    assert r.status_code == 410


def test_changed_file_triggers_rescan(cfg: Config) -> None:
    import os
    import time

    p = cfg.data_roots[0].path / "t.h5ad"
    tiny(p)
    c = client_for(cfg)
    later = time.time() + 10
    os.utime(p, (later, later))
    cat = c.app.state.catalog  # type: ignore[attr-defined]
    asked = []
    cat.refresh = lambda full=False: asked.append(full) or True
    assert c.get(f"/api/datasets/{entry_id('data', 't.h5ad')}/meta").status_code == 200
    assert asked == [False]


# ---------------------------------------------------------------- golden: example datasets


def _examples_cfg(cfg: Config, example: Callable[[str], Path]) -> Config:
    if not (EXAMPLES / "spatial.h5ad").exists():
        missing_example("spatial.h5ad")
    example("202302171205_region_1_scwf.zarr")
    cfg.data_roots = [
        DataRoot(name="examples", path=EXAMPLES),
        DataRoot(name="extracted", path=CACHE),
    ]
    return cfg


@pytest.mark.parametrize("name", ["spatial.h5ad", "dissociated.h5ad"])
def test_example_h5ad_golden(cfg: Config, example: Callable[[str], Path], name: str) -> None:
    c = client_for(_examples_cfg(cfg, example))
    id = entry_id("examples", name)
    a = ad.read_h5ad(EXAMPLES / name, backed="r")
    m = c.get(f"/api/datasets/{id}/meta").json()
    assert m["n_obs"] == a.n_obs and m["n_vars"] == a.n_vars
    assert {e["key"] for e in m["embeddings"]} <= set(a.obsm)
    for col in m["obs"]:
        s = a.obs[col["name"]]
        r = c.get(f"/api/datasets/{id}/obs", params={"column": col["name"]})
        assert r.status_code == 200, col["name"]
        got = decode(r)
        if isinstance(s.dtype, pd.CategoricalDtype):
            if col["categories"] is None:  # e.g. barcodes: too many to send or colour by
                assert col["n_categories"] == len(s.cat.categories) > 10_000
            else:
                assert col["categories"] == [str(x) for x in s.cat.categories]
            np.testing.assert_array_equal(got, s.cat.codes.to_numpy())
            colors = a.uns.get(f"{col['name']}_colors")
            if colors is not None and len(colors) == len(s.cat.categories):
                assert col["colors"] == [c.lower() for c in colors], col["name"]
        elif s.dtype.kind in "iuf":
            np.testing.assert_array_equal(got, s.to_numpy().astype(np.float32))
    for e in m["embeddings"]:
        got = decode(c.get(f"/api/datasets/{id}/obsm", params={"key": e["key"]}))
        np.testing.assert_array_equal(got, np.asarray(a.obsm[e["key"]])[:, :2].astype(np.float32))


def test_example_spatialdata_table_golden(cfg: Config, example: Callable[[str], Path]) -> None:
    c = client_for(_examples_cfg(cfg, example))
    store = CACHE / "202302171205_region_1_scwf.zarr"
    id = entry_id("extracted", store.name)
    m = c.get(f"/api/datasets/{id}/meta").json()
    assert m["kind"] == "spatialdata_zarr" and len(m["tables"]) == 2
    for table in m["tables"]:
        mt = c.get(f"/api/datasets/{id}/meta", params={"table": table}).json()
        a = ad.read_zarr(store / "tables" / table)
        assert mt["n_obs"] == a.n_obs
        key = mt["embeddings"][0]["key"]
        got = decode(c.get(f"/api/datasets/{id}/obsm", params={"key": key, "table": table}))
        np.testing.assert_array_equal(got, np.asarray(a.obsm[key])[:, :2].astype(np.float32))
        cat = next((col for col in mt["obs"] if col["kind"] == "categorical"), None)
        if cat is not None:
            r = c.get(f"/api/datasets/{id}/obs", params={"column": cat["name"], "table": table})
            np.testing.assert_array_equal(decode(r), a.obs[cat["name"]].cat.codes.to_numpy())


# ---------------------------------------------------------------- converted stores


@pytest.fixture
def store_client(cfg: Config) -> tuple[TestClient, str, Path]:
    from canopy.contract.example import write_example_store

    base = cfg.cache_dir / "0123456789abcdef" / "c0"
    write_example_store(base)
    return TestClient(create_app(cfg, background=False)), "/api/stores/0123456789abcdef/c0", base


def test_store_ranges(store_client: tuple[TestClient, str, Path]) -> None:
    c, url, base = store_client
    data = (base / "manifest.json").read_bytes()
    full = c.get(f"{url}/manifest.json")
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "bytes" and full.headers["cache-control"] == "no-cache"
    assert (
        "content-encoding"
        not in c.get(f"{url}/manifest.json", headers={"accept-encoding": "gzip"}).headers
    )
    r = c.get(f"{url}/manifest.json", headers={"range": "bytes=2-9"})
    assert r.status_code == 206 and r.content == data[2:10]
    assert r.headers["content-range"] == f"bytes 2-9/{len(data)}"
    r = c.get(f"{url}/manifest.json", headers={"range": "bytes=-5"})
    assert r.content == data[-5:]
    r = c.get(f"{url}/manifest.json", headers={"range": f"bytes=10-{len(data) + 100}"})
    assert r.status_code == 206 and r.content == data[10:]  # clamped
    r = c.get(f"{url}/manifest.json", headers={"range": f"bytes={len(data)}-"})
    assert r.status_code == 416 and r.headers["content-range"] == f"bytes */{len(data)}"
    r = c.get(f"{url}/manifest.json", headers={"range": "bytes=0-1,4-5"})
    assert r.status_code in (200, 206)  # whole file, or multipart from Starlette
    h = c.head(f"{url}/manifest.json", headers={"range": "bytes=0-3"})
    assert h.status_code == 206 and h.headers["content-length"] == "4"
    again = c.get(f"{url}/manifest.json", headers={"if-none-match": full.headers["etag"]})
    assert again.status_code == 304
    # a range request is never answered with 304 and not stored by the browser (review S6 B-B2)
    rr = c.get(
        f"{url}/manifest.json",
        headers={"if-none-match": full.headers["etag"], "range": "bytes=0-3"},
    )
    assert rr.status_code == 206 and rr.content == data[:4]
    assert rr.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "key",
    [
        "../../catalog.sqlite",
        "..%2F..%2Fcatalog.sqlite",
        ".hidden",
        "a//b",
    ],
)
def test_store_rejects_unsafe_keys(store_client: tuple[TestClient, str, Path], key: str) -> None:
    c, url, _ = store_client
    assert c.get(f"{url}/{key}").status_code in (400, 404)


def test_store_file_refuses_dot_segments(store_client: tuple[TestClient, str, Path]) -> None:
    from fastapi import HTTPException

    from canopy.stores import store_file

    # HTTP clients normalise `..` away before sending; the server checks anyway.
    _, _, base = store_client
    with pytest.raises(HTTPException):
        store_file(base.parent.parent, "0123456789abcdef", "c0", "images/../manifest.json")


def test_store_must_be_complete(store_client: tuple[TestClient, str, Path], cfg: Config) -> None:
    c, _, base = store_client
    (base / "manifest.json").unlink()  # still being written
    assert c.get("/api/stores/0123456789abcdef/c0/zarr.json").status_code == 404
    assert c.get("/api/stores/NOT-A-VERSION/c0/zarr.json").status_code == 404


def test_spa_fallback(cfg: Config) -> None:
    from canopy.app import FRONTEND_DIST

    if not (FRONTEND_DIST / "index.html").exists():
        pytest.skip("frontend not built")
    c = TestClient(create_app(cfg, background=False))
    page = c.get("/datasets/abc")
    assert page.status_code == 200 and '<div id="root">' in page.text
    assert c.get("/api/nope").status_code == 404
    assert c.get("/missing.js").status_code == 404


# ---------------------------------------------------------------- review R1 regressions


def test_store_range_spanning_blocks_is_streamed(
    store_client: tuple[TestClient, str, Path],
) -> None:
    from canopy.stores import BLOCK

    c, url, base = store_client
    big = base / "big.bin"
    data = np.random.default_rng(0).integers(0, 255, 3 * BLOCK + 17, dtype=np.uint8).tobytes()
    big.write_bytes(data)
    r = c.get(f"{url}/big.bin", headers={"range": f"bytes={BLOCK - 5}-"})
    assert r.status_code == 206 and r.content == data[BLOCK - 5 :]
    assert r.headers["content-length"] == str(len(data) - BLOCK + 5)
    whole = c.get(f"{url}/big.bin")
    assert whole.status_code == 200 and whole.content == data
    bad = c.get(f"{url}/big.bin", headers={"range": "bytes=5-3"})  # malformed: ignored
    assert bad.status_code == 200 and len(bad.content) == len(data)


def test_string_and_nullable_boolean_columns(cfg: Config) -> None:
    import h5py

    p = cfg.data_roots[0].path / "s.h5ad"
    a = tiny(p)
    with h5py.File(p, "a") as f:  # a plain string array, as R and older tools write it
        del f["obs/label"]
        f["obs"].create_dataset(
            "label",
            data=np.array([f"s{i % 5}" for i in range(a.n_obs)], dtype=object),
            dtype=h5py.string_dtype(),
        )
        f["obs/label"].attrs["encoding-type"] = "string-array"
        f["obs/label"].attrs["encoding-version"] = "0.2.0"
    a2 = ad.read_h5ad(p)
    a2.obs["maybe"] = pd.array([True, None, False] * 100, dtype="boolean")
    a2.write_h5ad(p)
    with h5py.File(p, "a") as f:
        del f["obs/label"]
        f["obs"].create_dataset(
            "label",
            data=np.array([f"s{i % 5}" for i in range(a.n_obs)], dtype=object),
            dtype=h5py.string_dtype(),
        )
        f["obs/label"].attrs["encoding-type"] = "string-array"
        f["obs/label"].attrs["encoding-version"] = "0.2.0"
    c = client_for(cfg)
    id = entry_id("data", "s.h5ad")
    meta = {col["name"]: col for col in c.get(f"/api/datasets/{id}/meta").json()["obs"]}
    assert meta["label"]["kind"] == "string"
    labels = c.get(f"/api/datasets/{id}/obs/labels", params={"column": "label"}).json()
    assert labels == {
        "kind": "string",
        "categories": ["s0", "s1", "s2", "s3", "s4"],
        "colors": None,
    }
    codes = decode(c.get(f"/api/datasets/{id}/obs", params={"column": "label"}))
    assert [labels["categories"][i] for i in codes[:6]] == ["s0", "s1", "s2", "s3", "s4", "s0"]
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "maybe"})
    assert r.headers["x-canopy-kind"] == "boolean" and r.headers["x-canopy-dtype"] == "int8"
    assert decode(r)[:3].tolist() == [1, -1, 0]  # missing stays missing
    assert meta["maybe"]["categories"] == ["false", "true"]


def test_source_closed_during_a_request_is_reopened(cfg: Config) -> None:
    tiny(cfg.data_roots[0].path / "t.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "t.h5ad")
    assert c.get(f"/api/datasets/{id}/meta").status_code == 200
    cache = c.app.state.datasets  # type: ignore[attr-defined]
    real_source = cache.source
    calls = []

    def closing_source(*a: object, **k: object) -> object:
        src = real_source(*a, **k)
        if not calls:  # the first source handed out is closed under the request's feet
            src.close()
        calls.append(1)
        return src

    cache.source = closing_source
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "score"})
    assert r.status_code == 200 and len(calls) == 2


def test_preview_never_reads_the_matrix(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import h5py

    tiny(cfg.data_roots[0].path / "t.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "t.h5ad")
    real_getitem = h5py.Dataset.__getitem__

    def guarded(self: h5py.Dataset, key: object) -> object:
        assert not self.name.startswith("/X"), f"X was read: {self.name}"
        return real_getitem(self, key)

    monkeypatch.setattr(h5py.Dataset, "__getitem__", guarded)
    m = c.get(f"/api/datasets/{id}/meta").json()
    for col in m["obs"]:
        assert c.get(f"/api/datasets/{id}/obs", params={"column": col["name"]}).status_code == 200
    for e in m["embeddings"]:
        assert c.get(f"/api/datasets/{id}/obsm", params={"key": e["key"]}).status_code == 200


def test_reader_failure_is_a_logged_500_not_a_404(
    tiny_ds: tuple[TestClient, str, ad.AnnData], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, id, _ = tiny_ds

    def broken(*a: object, **k: object) -> None:
        raise KeyError("inside a library")

    monkeypatch.setattr("canopy.data.reader.read_elem", broken)
    r = c.get(f"/api/datasets/{id}/obs", params={"column": "score"})
    assert r.status_code == 500 and r.json()["detail"]["code"] == "internal_error"
    failed = next(e for e in log_events() if e["event"] == "request_failed")
    assert failed["exc_type"] == "KeyError" and failed["dataset_id"] == id


def test_idle_entries_are_swept(cfg: Config) -> None:
    import time

    from canopy.data.cache import DatasetCache

    cfg.memory = MemoryConfig(idle_minutes=0.001)  # 60 ms
    cache = DatasetCache(cfg)
    cache.array(("k",), lambda: np.zeros(10), lambda a: int(a.nbytes))
    time.sleep(0.1)
    cache.sweep()
    assert cache.stats()["entries"] == 0
    assert any(e["event"] == "cache_evict" and e["reason"] == "idle" for e in log_events())


# ---------------------------------------------------------------- dataset colours (uns)


def test_parse_colors_accepts_what_scanpy_and_matplotlib_accept() -> None:
    from canopy.data.reader import parse_colors

    assert parse_colors(["#FF0000", "#00ff0080", "#abc"], 3) == (
        ["#ff0000", "#00ff00", "#aabbcc"],
        None,
    )
    assert parse_colors(["red", "lightgray", "tab:blue", "C1"], 4)[0] == [
        "#ff0000",
        "#d3d3d3",
        "#1f77b4",
        "#ff7f0e",
    ]
    assert parse_colors([b"#123456", b"#654321", b"#000000"], 2) == (["#123456", "#654321"], None)
    assert parse_colors(["#123456"], 2) == (None, "too_few")
    assert parse_colors(["#123456", "not-a-colour"], 2) == (None, "not_a_colour")


def test_dataset_colours_are_the_default_whenever_usable(cfg: Config) -> None:
    rng = np.random.default_rng(0)
    n = 60
    a = ad.AnnData(X=np.zeros((n, 2), np.float32))
    a.obs["named"] = pd.Categorical(rng.choice(["x", "y"], n), categories=["x", "y"])
    a.uns["named_colors"] = ["red", "tab:green"]
    a.obs["longer"] = pd.Categorical(rng.choice(["p", "q"], n), categories=["p", "q"])
    a.uns["longer_colors"] = ["#111111", "#222222", "#333333"]  # scanpy uses the first two
    a.obs["short"] = pd.Categorical(rng.choice(["s", "t", "u"], n), categories=["s", "t", "u"])
    a.uns["short_colors"] = ["#111111"]
    a.obs["text"] = [["beta", "alpha"][i % 2] for i in range(n)]  # a plain string column
    a.uns["text_colors"] = ["#0000ff", "#ff0000"]  # scanpy's order: alphabetical
    a.obsm["X_umap"] = np.zeros((n, 2), np.float32)
    path = cfg.data_roots[0].path / "colours.h5ad"
    a.write_h5ad(path)
    c = client_for(cfg)
    id = entry_id("data", "colours.h5ad")
    cols = {col["name"]: col for col in c.get(f"/api/datasets/{id}/meta").json()["obs"]}
    assert cols["named"]["colors"] == ["#ff0000", "#2ca02c"]
    assert cols["longer"]["colors"] == ["#111111", "#222222"]
    assert cols["short"]["colors"] is None  # too few: the browser makes up a palette
    labels = c.get(f"/api/datasets/{id}/obs/labels", params={"column": "text"}).json()
    assert labels["categories"] == ["alpha", "beta"] and labels["colors"] == ["#0000ff", "#ff0000"]
    ignored = [e for e in log_events() if e["event"] == "uns_colors_ignored"]
    assert any(e["column"] == "short" and e["reason"] == "too_few" for e in ignored)


def test_a_barcode_column_is_refused_from_a_sample_and_not_read_again(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from canopy.data import reader

    n = reader.TEXT_SAMPLE + 10
    a = ad.AnnData(X=np.zeros((n, 1), np.float32))
    a.obs["barcode"] = [f"BC{i:07d}" for i in range(n)]  # all distinct
    a.write_h5ad(cfg.data_roots[0].path / "bc.h5ad")
    c = client_for(cfg)
    id = entry_id("data", "bc.h5ad")
    reads = []
    real = reader.read_elem
    monkeypatch.setattr(reader, "read_elem", lambda node: reads.append(1) or real(node))
    for _ in range(2):
        r = c.get(f"/api/datasets/{id}/obs", params={"column": "barcode"})
        assert (
            r.status_code == 409
            and r.json()["detail"]["code"] == "needs_conversion:too_many_values"
        )
    assert reads == []  # refused from the sample; the whole column was never read


def test_large_whole_numbers_keep_their_value(cfg: Config) -> None:
    a = ad.AnnData(X=np.zeros((3, 1), np.float32))
    a.obs["cell_number"] = np.array([2**40 + 1, 2**40 + 2, 5], np.int64)
    a.write_h5ad(cfg.data_roots[0].path / "ids.h5ad")
    c = client_for(cfg)
    r = c.get(f"/api/datasets/{entry_id('data', 'ids.h5ad')}/obs", params={"column": "cell_number"})
    assert r.headers["x-canopy-dtype"] == "float64"
    assert np.frombuffer(r.content, "<f8").tolist() == [2**40 + 1, 2**40 + 2, 5]
