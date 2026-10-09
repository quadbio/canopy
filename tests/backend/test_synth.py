from __future__ import annotations

from pathlib import Path

import anndata as ad
import numpy as np
import pytest

from dbx.config import Config
from dbx.inspect import inspect_path
from dbx.synth import SynthMatrix, structure_signature, write_anndata, write_mock
from dbx.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec


def test_matrix_rows_and_cols_agree() -> None:
    m = SynthMatrix(700, 230, 0.07, seed=3)
    rows = m.rows(0, 700).toarray()
    assert np.array_equal(rows, m.cols(0, 230).toarray())
    assert np.array_equal(rows[100:200], m.rows(100, 200).toarray())
    assert 0.05 < (rows > 0).mean() < 0.09
    assert rows.max() <= m.max_count and rows.dtype == np.float32


def test_matrix_seed_changes_values() -> None:
    a = SynthMatrix(50, 50, 0.3, seed=1).rows(0, 50).toarray()
    b = SynthMatrix(50, 50, 0.3, seed=2).rows(0, 50).toarray()
    assert not np.array_equal(a, b)


def _spec(encoding: str) -> AnnDataSpec:
    return AnnDataSpec(
        n_obs=3000,
        n_vars=150,
        X=MatrixSpec(encoding=encoding, density=0.1, chunk=500),  # type: ignore[arg-type]
        layers={"counts": MatrixSpec(encoding=encoding, dtype="int32")},  # type: ignore[arg-type]
        obs=[
            ColumnSpec(name="cluster", kind="categorical", n_categories=7, colors=True),
            ColumnSpec(name="score", kind="numeric"),
            ColumnSpec(name="flag", kind="boolean"),
        ],
        obsm={"X_umap": 2},
        extra_colors={"sample": 4},
    )


@pytest.mark.parametrize("encoding", ["csr", "csc", "dense"])
@pytest.mark.parametrize("fmt", ["h5ad", "zarr"])
def test_write_anndata_layouts_hold_same_matrix(
    cfg: Config, tmp_path: Path, encoding: str, fmt: str
) -> None:
    p = write_anndata(_spec(encoding), tmp_path / f"x.{fmt}")
    a = ad.read_h5ad(p) if fmt == "h5ad" else ad.read_zarr(p)
    x = a.X.toarray() if hasattr(a.X, "toarray") else np.asarray(a.X)
    assert np.array_equal(x, SynthMatrix(3000, 150, 0.1).rows(0, 3000).toarray())
    assert len(a.uns["cluster_colors"]) == 7 and len(a.uns["sample_colors"]) == 4

    fp = inspect_path(p)
    assert fp.anndata is not None and fp.anndata.X is not None
    assert fp.anndata.X.encoding == encoding
    assert fp.anndata.X.storage.chunks is not None
    assert [c.kind for c in fp.anndata.obs.columns] == ["categorical", "numeric", "boolean"]
    assert fp.anndata.layers["counts"].dtype == "int32"


def test_mock_round_trip_synthetic(cfg: Config, tmp_path: Path) -> None:
    src = write_anndata(_spec("csr"), tmp_path / "src.h5ad")
    fp = inspect_path(src)
    mock = write_mock(fp, tmp_path / "mock.h5ad", scale=0.2)
    fp2 = inspect_path(mock)
    assert structure_signature(fp2) == structure_signature(fp)
    assert fp2.anndata is not None and fp2.anndata.n_obs == 600


@pytest.mark.parametrize(
    "name", ["dissociated.h5ad", "spatial.h5ad", "202302171205_region_1_scwf.zarr"]
)
def test_mock_round_trip_examples(cfg: Config, tmp_path: Path, example, name: str) -> None:  # type: ignore[no-untyped-def]
    fp = inspect_path(example(name))
    suffix = ".zarr" if name.endswith(".zarr") else ".h5ad"
    mock = write_mock(fp, tmp_path / f"mock{suffix}", scale=0.03, max_side=256)
    fp2 = inspect_path(mock)
    assert structure_signature(fp2) == structure_signature(fp)
    assert fp2.size_bytes < fp.size_bytes
