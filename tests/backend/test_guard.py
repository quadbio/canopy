"""Nothing outside a dataset is read through it: HDF5 external storage, external links, virtual
datasets, and symlinks out of a zarr store (review full-2 A-H1)."""

from __future__ import annotations

import os
from pathlib import Path

import anndata as ad
import h5py
import numpy as np
import pandas as pd
import pytest
from test_convert_spatial import synthetic
from test_datasets import client_for

from canopy.catalog import entry_id
from canopy.config import Config
from canopy.convert.anndata import ConvertOptions
from canopy.convert.matrix import ConversionError
from canopy.convert.spatialdata import convert_spatialdata
from canopy.inspect import inspect_path
from canopy.inspect.guard import OutsideDataError, links_outside, open_zarr

SECRET = b"SECRET-OUTSIDE-ROOT-0123456789ab"


def _secret(tmp_path: Path) -> Path:
    p = tmp_path / "outside" / "id_rsa"
    p.parent.mkdir()
    p.write_bytes(SECRET)
    return p


def _adata() -> ad.AnnData:
    a = ad.AnnData(X=np.ones((8, 3), np.float32))
    a.obs["x"] = pd.Categorical(["a", "b"] * 4)
    return a


@pytest.mark.parametrize("how", ["external_storage", "external_link", "virtual"])
def test_an_h5ad_pointing_outside_is_refused(cfg: Config, tmp_path: Path, how: str) -> None:
    secret = _secret(tmp_path)
    path = cfg.data_roots[0].path / "evil.h5ad"
    _adata().write_h5ad(path)
    with h5py.File(path, "a") as f:
        if how == "external_storage":
            f.create_dataset("uns/leak", shape=(8,), dtype="f4", external=[(str(secret), 0, 32)])
        elif how == "external_link":
            other = tmp_path / "outside" / "other.h5"
            with h5py.File(other, "w") as o:
                o["d"] = np.arange(3)
            f["uns/leak"] = h5py.ExternalLink(str(other), "d")
        else:
            other = tmp_path / "outside" / "other.h5"
            with h5py.File(other, "w") as o:
                o["d"] = np.arange(8, dtype="f4")
            layout = h5py.VirtualLayout(shape=(8,), dtype="f4")
            layout[:] = h5py.VirtualSource(str(other), "d", shape=(8,))
            f.create_virtual_dataset("uns/leak", layout)
    with pytest.raises(OutsideDataError):
        inspect_path(path)
    c = client_for(cfg)
    c.app.state.catalog.scan_now()  # type: ignore[attr-defined]
    row = c.get(f"/api/catalog/datasets/{entry_id('data', 'evil.h5ad')}").json()
    assert row["state"] == "unsupported" and row["reason"] == "links_outside"
    r = c.get(f"/api/datasets/{entry_id('data', 'evil.h5ad')}/meta")
    assert r.status_code >= 400 and SECRET not in r.content


def test_a_zarr_chunk_symlinked_outside_is_never_read(cfg: Config, tmp_path: Path) -> None:
    secret = _secret(tmp_path)
    store = cfg.data_roots[0].path / "evil.zarr"
    a = _adata()
    a.obs["leak"] = np.arange(
        1, 9, dtype=np.float32
    )  # non-zero: zarr writes no chunk of fill values
    a.write_zarr(store)
    chunk = next(
        p
        for p in (store / "obs" / "leak").rglob("*")
        if p.is_file() and p.name != "zarr.json" and not p.name.startswith(".")
    )
    chunk.unlink()
    os.symlink(secret, chunk)
    g = open_zarr(store)
    with pytest.raises(Exception) as e:
        g["obs/leak"][:]
    assert (
        isinstance(e.value, OutsideDataError)
        or isinstance(e.value.__cause__, OutsideDataError)
        or "links_outside" in repr(e.value)
    )
    c = client_for(cfg)
    c.app.state.catalog.scan_now()  # type: ignore[attr-defined]
    r = c.get(f"/api/datasets/{entry_id('data', 'evil.zarr')}/obs", params={"column": "leak"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "links_outside"
    assert SECRET not in r.content


def test_a_spatialdata_store_linking_outside_is_not_converted(tmp_path: Path) -> None:
    secret = _secret(tmp_path)
    store = synthetic(tmp_path / "sd.zarr")
    assert not links_outside(store)
    target = next(p for p in (store / "points").rglob("*.parquet") if p.is_file())
    target.unlink()
    os.symlink(secret, target)
    assert links_outside(store)
    with pytest.raises(OutsideDataError):
        inspect_path(store)
    fp = inspect_path(synthetic(tmp_path / "ok.zarr"))
    # the same check in the converter (the file may have changed after inspection)
    fp2 = fp.model_copy(update={"path": str(store)})
    with pytest.raises(ConversionError) as e:
        convert_spatialdata(store, fp2, tmp_path / "cache", ConvertOptions(job=1), lambda *a: None)
    assert e.value.code == "links_outside"


def test_a_cache_belongs_to_one_installation(tmp_path: Path) -> None:
    """A second installation (its own state folder) on the same cache would delete the first's
    running conversions and copies: it is refused (review full-2 A-M1)."""
    from canopy.jobs.manager import OWNER_FILE, CacheOwnedError, claim_cache

    cache = tmp_path / "cache"
    claim_cache(cache, tmp_path / "state_a")
    claim_cache(cache, tmp_path / "state_a")  # the owner again: fine
    with pytest.raises(CacheOwnedError) as e:
        claim_cache(cache, tmp_path / "state_b")
    assert "another Canopy installation" in str(e.value)
    (cache / OWNER_FILE).unlink()  # that installation is gone
    claim_cache(cache, tmp_path / "state_b")
