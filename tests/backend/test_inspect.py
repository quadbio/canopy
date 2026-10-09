from __future__ import annotations

import os
from pathlib import Path

import pytest

from dbx.config import Config
from dbx.inspect import dataset_id, detect_kind, inspect_path, redact
from dbx.synth import write_anndata
from dbx.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec


def test_detect_kind_rejects_non_datasets(tmp_path: Path) -> None:
    (tmp_path / "a.h5ad").write_bytes(b"not hdf5")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "plain_dir").mkdir()
    assert detect_kind(tmp_path / "a.h5ad") is None
    assert detect_kind(tmp_path / "b.txt") is None
    assert detect_kind(tmp_path / "plain_dir") is None
    with pytest.raises(ValueError):
        inspect_path(tmp_path / "b.txt")


def test_anndata_zarr_detected(cfg: Config, tmp_path: Path) -> None:
    p = write_anndata(AnnDataSpec(n_obs=100, n_vars=20), tmp_path / "a.zarr")
    assert detect_kind(p) == "anndata_zarr"
    assert inspect_path(p).size_exact


def test_dataset_id_changes_with_mtime(cfg: Config, tmp_path: Path) -> None:
    p = write_anndata(AnnDataSpec(n_obs=50, n_vars=10), tmp_path / "a.h5ad")
    first = dataset_id(p)
    assert dataset_id(p) == first
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert dataset_id(p) != first


def test_csr_warning(cfg: Config, tmp_path: Path) -> None:
    spec = AnnDataSpec(n_obs=100, n_vars=20, X=MatrixSpec(encoding="csr", dtype="int32"))
    fp = inspect_path(write_anndata(spec, tmp_path / "a.h5ad"))
    assert any("CSR" in w for w in fp.warnings)
    assert any("dtype is int32" in w for w in fp.warnings)


def test_examples_h5ad(cfg: Config, example) -> None:  # type: ignore[no-untyped-def]
    d = inspect_path(example("dissociated.h5ad")).anndata
    assert d is not None and d.X is not None
    assert (d.n_obs, d.n_vars, d.X.encoding) == (16496, 18499, "csc")
    assert "X_umap" in d.obsm and d.colors["cluster"] == 14
    s = inspect_path(example("spatial.h5ad")).anndata
    assert s is not None and s.X is not None
    assert (s.n_obs, s.n_vars, s.X.encoding) == (51787, 351, "csr")
    assert s.obsm["spatial"].shape == [51787, 2]


def test_examples_spatialdata(cfg: Config, example) -> None:  # type: ignore[no-untyped-def]
    fp = inspect_path(example("202302171205_region_1_scwf.zarr"))
    sd = fp.spatialdata
    assert fp.kind == "spatialdata_zarr" and sd is not None
    assert set(sd.images) == {"img", "dapi_scaled", "outlines"}
    assert sd.images["img"].levels[0].shape == [3, 4296, 4890]
    assert sd.points["signal"].n_rows == 5_565_992
    assert sd.shapes["boundaries_cells"].n_rows == 6125
    assert sd.shapes["boundaries_cells"].geometry_types == ["Polygon"]
    assert sd.table_refs["table_cells"].region == ["boundaries_cells"]
    assert sd.coordinate_systems == ["global", "rot"]
    assert any("single resolution level" in w for w in fp.warnings)


def test_strict_redaction_hides_names(cfg: Config, tmp_path: Path, example) -> None:  # type: ignore[no-untyped-def]
    spec = AnnDataSpec(
        n_obs=100,
        n_vars=20,
        obs=[ColumnSpec(name="secret_patient_group", kind="categorical", colors=True)],
        obsm={"X_umap": 2, "X_private_embedding": 2},
    )
    p = write_anndata(spec, tmp_path / "confidential_project.h5ad")
    fp = inspect_path(p)
    text = redact(fp, "strict", salt="s").model_dump_json()
    for secret in (
        "secret_patient_group",
        "X_private_embedding",
        "confidential_project",
        str(tmp_path),
    ):
        assert secret not in text
    assert "X_umap" in text  # standard names are kept
    assert redact(fp, "standard", salt="s") == fp
    # Same salt -> same tokens (bundles stay comparable)
    assert redact(fp, "strict", salt="s") == redact(fp, "strict", salt="s")

    sd = inspect_path(example("202302171205_region_1_scwf.zarr"))
    stext = redact(sd, "strict", salt="s").model_dump_json()
    for secret in (
        "boundaries_cells",
        "table_cells",
        "gene_id",
        "supertype",
        "'img'",
        '"rot"',
        "202302171205",
    ):
        assert secret not in stext
