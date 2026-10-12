from __future__ import annotations

import os
from pathlib import Path

import pytest

from canopy.config import Config
from canopy.inspect import Fingerprint, dataset_id, detect_kind, inspect_path, redact
from canopy.synth import write_anndata
from canopy.synth.spec import AnnDataSpec, ColumnSpec, MatrixSpec


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
    codes = {w.code: w for w in fp.warnings}
    assert "x_csr" in codes and "CSR" in codes["x_csr"].message
    assert codes["x_dtype"].message == "X dtype is int32"


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
    single = [w for w in fp.warnings if w.code == "single_level"]
    assert {w.element for w in single} >= {"img"} and fp.errors == []


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


# ---------------------------------------------------------------- review batch 1


def test_legacy_pre07_layout(cfg: Config, tmp_path: Path) -> None:
    from legacy_h5ad import N_OBS, N_VARS, write_pre07

    fp = inspect_path(write_pre07(tmp_path / "old.h5ad"))
    a = fp.anndata
    assert a is not None and a.X is not None
    assert a.layout == "pre-0.7"
    assert (a.n_obs, a.n_vars, a.X.encoding, a.X.shape) == (N_OBS, N_VARS, "csr", [N_OBS, N_VARS])
    cols = {c.name: c for c in a.obs.columns}
    assert cols["leiden"].kind == "categorical" and cols["leiden"].n_categories == 2
    assert cols["leiden"].has_colors and cols["n_genes"].kind == "numeric"
    assert {k: v.shape for k, v in a.obsm.items()} == {"X_umap": [N_OBS, 2], "X_pca": [N_OBS, 4]}
    assert a.raw is not None and a.raw_n_vars == N_VARS
    assert a.uns_keys == ["leiden_colors"]
    assert "legacy_layout" in {w.code for w in fp.warnings}


def test_legacy_v07_layout(cfg: Config, tmp_path: Path) -> None:
    from legacy_h5ad import N_OBS, write_v07

    fp = inspect_path(write_v07(tmp_path / "v07.h5ad"))
    a = fp.anndata
    assert a is not None and a.layout == "0.7" and a.n_obs == N_OBS
    cols = {c.name: c for c in a.obs.columns}
    assert (cols["leiden"].kind, cols["leiden"].n_categories, cols["leiden"].has_colors) == (
        "categorical",
        2,
        True,
    )
    # fixed-length byte strings are strings, not numbers
    assert (cols["label"].kind, cols["label"].dtype) == ("string", "string")


def test_legacy_fixtures_match_anndata(tmp_path: Path) -> None:
    """The hand-built fixtures are real layouts: anndata itself reads them."""
    import anndata as ad
    from legacy_h5ad import N_OBS, N_VARS, write_pre07, write_v07

    for fn in (write_pre07, write_v07):
        with pytest.warns(Warning):  # anndata: "written with a very old version of AnnData"
            a = ad.read_h5ad(fn(tmp_path / f"{fn.__name__}.h5ad"))
        assert a.shape == (N_OBS, N_VARS)
        assert str(a.obs["leiden"].dtype) == "category"


def test_broken_element_is_isolated(cfg: Config, tmp_path: Path, example) -> None:  # type: ignore[no-untyped-def]
    import json
    import shutil

    src = example("202302171205_region_1_scwf.zarr")
    dst = tmp_path / "broken.zarr"
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.parquet", "c"))
    # Corrupt the image's metadata, both in its own zarr.json and in the root's consolidated copy.
    meta = dst / "images" / "dapi_scaled" / "zarr.json"
    m = json.loads(meta.read_text())
    m["attributes"]["ome"]["multiscales"][0]["datasets"] = [{"path": "missing"}]
    meta.write_text(json.dumps(m))
    root_meta = dst / "zarr.json"
    r = json.loads(root_meta.read_text())
    r["consolidated_metadata"]["metadata"]["images/dapi_scaled"] = m
    root_meta.write_text(json.dumps(r))
    fp = inspect_path(dst)
    assert fp.spatialdata is not None
    assert "dapi_scaled" not in fp.spatialdata.images and "img" in fp.spatialdata.images
    assert [(e.kind, e.element) for e in fp.errors if e.kind == "images"] == [
        ("images", "dapi_scaled")
    ]
    assert "element_errors" in {w.code for w in fp.warnings}


def test_spatialdata_without_root_attrs(cfg: Config, tmp_path: Path) -> None:
    import zarr

    root = zarr.open_group(tmp_path / "old_sd.zarr", mode="w")
    root.create_group("images")
    assert detect_kind(tmp_path / "old_sd.zarr") == "spatialdata_zarr"
    fp = inspect_path(tmp_path / "old_sd.zarr")
    assert "no_spatialdata_attrs" in {w.code for w in fp.warnings}


def test_dataset_id_changes_with_size_and_store_metadata(cfg: Config, tmp_path: Path) -> None:
    p = write_anndata(AnnDataSpec(n_obs=50, n_vars=10), tmp_path / "a.h5ad")
    st = p.stat()
    first = dataset_id(p)
    with p.open("ab") as f:  # same mtime afterwards, different size (e.g. cp -p)
        f.write(b"\0")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert dataset_id(p) != first

    z = write_anndata(AnnDataSpec(n_obs=50, n_vars=10), tmp_path / "a.zarr")
    zid = dataset_id(z)
    meta = z / "obsm" / "zarr.json"
    os.utime(meta, ns=(meta.stat().st_atime_ns, meta.stat().st_mtime_ns + 10**9))
    assert dataset_id(z) != zid


def test_size_can_be_skipped(cfg: Config, tmp_path: Path) -> None:
    z = write_anndata(AnnDataSpec(n_obs=50, n_vars=10), tmp_path / "a.zarr")
    fp = inspect_path(z, measure_size=False)
    assert fp.size_bytes is None and not fp.size_exact


def test_hdf5_locking_disabled(cfg: Config, tmp_path: Path) -> None:
    import h5py

    import canopy  # noqa: F401  (sets the environment default)
    from canopy.inspect.core import open_h5

    p = write_anndata(AnnDataSpec(n_obs=5, n_vars=3), tmp_path / "a.h5ad")
    assert os.environ["HDF5_USE_FILE_LOCKING"] == "FALSE"
    # A writer holding the file does not stop a read-only open without locking.
    with h5py.File(p, "a"), open_h5(p) as f:
        assert "X" in f


def test_redact_rejects_unknown_level(cfg: Config, tmp_path: Path) -> None:
    fp = inspect_path(write_anndata(AnnDataSpec(n_obs=5, n_vars=3), tmp_path / "a.h5ad"))
    for bad in ("Strict", "STRICT", "strcit", ""):
        with pytest.raises(ValueError):
            redact(fp, bad, salt="s")
    with pytest.raises(ValueError):
        redact(fp, "strict", salt="")


def test_installation_salt_is_secret_and_stable(tmp_path: Path) -> None:
    from canopy.inspect.redact import SALT_FILE, installation_salt, token

    s1 = installation_salt(tmp_path)
    assert s1 == installation_salt(tmp_path) and len(s1) == 64
    assert (tmp_path / SALT_FILE).stat().st_mode & 0o777 == 0o600
    assert installation_salt(tmp_path / "other") != s1
    # A dictionary attack with the old public salt finds nothing.
    words = ["cluster", "sample", "leiden", "batch", "donor", "cell_type"]
    assert {token(w, s1) for w in words}.isdisjoint({token(w, "canopy") for w in words})


def _user_names(fp: Fingerprint) -> set[str]:
    """Every user-chosen name in a standard fingerprint (independent of redact's own logic)."""
    names = {fp.name, fp.path}
    tables = [fp.anndata] if fp.anndata else []
    sd = fp.spatialdata
    if sd:
        tables += list(sd.tables.values())
        names |= set(sd.images) | set(sd.labels) | set(sd.points) | set(sd.shapes)
        names |= set(sd.tables) | set(sd.coordinate_systems)
        for img in [*sd.images.values(), *sd.labels.values()]:
            names |= set(img.channel_names or [])
        for el in [*sd.points.values(), *sd.shapes.values()]:
            names |= set(el.columns) | {el.feature_key or "", el.instance_key or ""}
        for ref in sd.table_refs.values():
            names |= set(ref.region) | {ref.region_key or "", ref.instance_key or ""}
    for t in tables:
        assert t is not None
        for frame in (t.obs, t.var):
            names |= {c.name for c in frame.columns} | {frame.index_name or ""}
        names |= set(t.layers) | set(t.obsm) | set(t.varm) | set(t.obsp) | set(t.uns_keys)
    for e in fp.errors:
        names.add(e.element)
    for w in fp.warnings:
        names.add(w.element or "")
    return {n for n in names if n and n != "/"}


def _field_names() -> set[str]:
    """Field names of the fingerprint models (JSON keys that are not user names)."""
    schema = Fingerprint.model_json_schema()
    out = set(schema["properties"])
    for d in schema.get("$defs", {}).values():
        out |= set(d.get("properties", {}))
    return out


def test_strict_leaves_no_user_name(cfg: Config, tmp_path: Path, example) -> None:  # type: ignore[no-untyped-def]
    import json

    from legacy_h5ad import write_pre07

    from canopy.inspect.redact import KEEP

    paths = [
        write_pre07(tmp_path / "o'brien lab.h5ad"),  # apostrophes once leaked through warnings
        example("dissociated.h5ad"),
        example("202302171205_region_1_scwf.zarr"),
    ]
    for p in paths:
        fp = inspect_path(p)
        strict = redact(fp, "strict", salt="secret")
        strings: list[str] = []

        def walk(v: object, out: list[str] = strings) -> None:
            if isinstance(v, dict):
                for k, x in v.items():
                    out.append(k)
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str):
                out.append(v)

        walk(json.loads(strict.model_dump_json()))
        leaked = (_user_names(fp) - KEEP) & (set(strings) - _field_names())
        assert not leaked, f"{p.name}: {sorted(leaked)[:10]}"
        assert strict.redacted
