from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import anndata as ad
import numpy as np
import pytest
import zarr

from canopy.config import Config
from canopy.contract import MANIFEST, validate_store
from canopy.contract.example import write_example_store
from canopy.contract.models import manifest_json_schema
from canopy.contract.write import morton_order, pyramid_levels

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def example_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_example_store(tmp_path_factory.mktemp("store") / "c0")


@pytest.fixture
def store(example_store: Path, tmp_path: Path) -> Path:
    """A private copy that a test may corrupt."""
    dst = tmp_path / "c0"
    shutil.copytree(example_store, dst)
    return dst


def test_example_store_is_valid(cfg: Config, example_store: Path) -> None:
    rep = validate_store(example_store, deep=True)
    assert rep.ok, rep.errors
    assert rep.warnings == []


def test_table_is_plain_anndata(example_store: Path) -> None:
    a = ad.read_zarr(example_store / "tables" / "cells")
    assert a.shape == (300, 40)
    assert a.X.format == "csc" and a.X.dtype == np.float32
    assert list(a.uns["cluster_colors"])[:1] == ["#1b9e77"]
    assert set(a.uns["canopy"]["gene_stats"]) == {"nnz", "mean", "max", "p99"}


def test_schema_file_in_sync() -> None:
    on_disk = json.loads((REPO / "contract" / "schema" / "manifest.schema.json").read_text())
    assert on_disk == manifest_json_schema(), "run `pixi run -e dev contract-schema`"


def _edit_manifest(store: Path, fn: Callable[[dict], None]) -> None:  # type: ignore[type-arg]
    m = json.loads((store / MANIFEST).read_text())
    fn(m)
    (store / MANIFEST).write_text(json.dumps(m))


def _errors(store: Path) -> str:
    rep = validate_store(store, deep=True)
    assert not rep.ok
    return "\n".join(rep.errors)


def test_missing_manifest(cfg: Config, store: Path) -> None:
    (store / MANIFEST).unlink()
    assert "manifest.json missing" in _errors(store)


def test_wrong_major_version(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m.update(contract_version="1.0.0"))
    assert "contract major 1" in _errors(store)


def test_bad_affine(cfg: Config, store: Path) -> None:
    _edit_manifest(
        store,
        lambda m: m["images"]["img"]["transforms"].update(
            {"global": [[1, 0, 0], [0, 1, 0], [1, 1, 1]]}
        ),
    )
    assert "last row" in _errors(store)


def test_undeclared_coordinate_system(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m.update(coordinate_systems=["global"]))
    assert "undeclared systems ['rot']" in _errors(store)


def test_x_must_be_csc(cfg: Config, store: Path) -> None:
    g = zarr.open_group(store / "tables" / "cells" / "X", mode="a")
    g.attrs["encoding-type"] = "csr_matrix"
    assert "X must be csc_matrix" in _errors(store)


def test_stale_consolidated_metadata(cfg: Config, store: Path) -> None:
    t = zarr.open_group(store / "tables" / "cells", mode="a", use_consolidated=False)
    zarr.open_group(store / "tables" / "cells", mode="a", use_consolidated=False)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        del t["X"]
        zarr.consolidate_metadata(store / "tables" / "cells")
        t2 = zarr.open_group(store / "tables" / "cells", mode="a", use_consolidated=False)
        t2.create_group("X")
    assert "stale" in _errors(store) or "missing node" in _errors(store)


def test_colour_count_mismatch(cfg: Config, store: Path) -> None:
    _edit_manifest(
        store, lambda m: m["tables"]["cells"]["categorical"]["cluster"].update(n_categories=7)
    )
    assert "7" in _errors(store)


def test_points_must_be_sorted(cfg: Config, store: Path) -> None:
    a = zarr.open_array(store / "points" / "transcripts" / "xy", mode="a")
    v = np.asarray(a[:])
    a[:] = v[::-1].copy()
    assert "not sorted by feature, then Morton code" in _errors(store)


def test_z_outside_planes(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["points"]["transcripts"].update(z_planes=[0.0, 1.0]))
    assert "z values not in z_planes" in _errors(store)


def test_z_range_requires_has_z(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["points"]["transcripts"].update(has_z=False))
    assert "without has_z" in _errors(store)


def test_labels_lookup_length(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["labels"]["cells"].update(max_label=999))
    assert "lookup must be int32 of length max_label + 1" in _errors(store)


def test_pyramid_level_shape(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["images"]["img"]["levels"][1].__setitem__(1, 999))
    assert "level 1 shape" in _errors(store)


def test_polygon_starts(cfg: Config, store: Path) -> None:
    a = zarr.open_array(store / "shapes" / "cells" / "starts", mode="a")
    a[-1] = 5
    assert "starts must run" in _errors(store)


def test_unknown_manifest_field_rejected(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["tables"]["cells"].update(surprise=1))
    assert "unknown field tables.cells.surprise" in _errors(store)


def test_newer_minor_version_is_read_with_warning(cfg: Config, store: Path) -> None:
    """SPEC §9: readers ignore unknown fields; a newer minor version is only a warning."""
    from canopy.contract.validate import read_manifest

    _edit_manifest(store, lambda m: m.update(contract_version="0.9.0", new_feature={"a": 1}))
    assert read_manifest(store).contract_version == "0.9.0"  # the reader opens it
    rep = validate_store(store, deep=True)
    assert rep.ok, rep.errors
    assert any("newer than this validator" in w for w in rep.warnings)
    assert any("new_feature" in w for w in rep.warnings)


# ---------------------------------------------------------------- v0.2 layout rules
def _rewrite(store: Path, path: str, **kw: object) -> None:
    """Rewrite an array with different chunking/codec, keeping its values and attributes."""
    from canopy.contract.write import array

    parent, name = path.rsplit("/", 1)
    a = zarr.open_array(store / path, mode="r")
    data, attrs = np.asarray(a[:]), dict(a.attrs)
    g = zarr.open_group(store / parent, mode="a", use_consolidated=False)
    del g[name]
    array(g, name, data, attrs=attrs, **kw)  # type: ignore[arg-type]


def test_x_in_one_chunk_rejected(cfg: Config, store: Path) -> None:
    n = zarr.open_array(store / "tables/cells/X/data").shape[0]
    _rewrite(store, "tables/cells/X/data", chunks=(n,))
    assert "X/data: chunks" in _errors(store)


def test_uncompressed_rejected(cfg: Config, store: Path) -> None:
    g = zarr.open_group(store / "shapes/cells", mode="a", use_consolidated=False)
    data = np.asarray(g["block_bbox"][:])  # type: ignore[index]
    del g["block_bbox"]
    g.create_array("block_bbox", data=data, compressors=None)
    assert "block_bbox: codec" in _errors(store)


def test_points_must_be_sharded(cfg: Config, store: Path) -> None:
    from canopy.contract.models import POINT_CHUNK

    _rewrite(store, "points/transcripts/xy", chunks=(POINT_CHUNK, 2))
    assert "xy: shards" in _errors(store)


def test_csc_duplicate_rows_rejected(cfg: Config, store: Path) -> None:
    a = zarr.open_array(store / "tables/cells/X/indices", mode="a")
    ip = np.asarray(zarr.open_array(store / "tables/cells/X/indptr")[:])
    j = int(np.argmax(np.diff(ip) >= 2))  # a gene with at least two cells
    a[int(ip[j]) + 1] = a[int(ip[j])]
    assert "not canonical CSC" in _errors(store)


def test_stale_root_consolidated_metadata(cfg: Config, store: Path) -> None:
    g = zarr.open_group(store / "aux", mode="a", use_consolidated=False)
    g.create_group("extra")
    assert "root consolidated metadata is stale" in _errors(store)


def test_bad_element_name(cfg: Config, store: Path) -> None:
    def move(m: dict) -> None:  # type: ignore[type-arg]
        m["images"]["../img"] = m["images"].pop("img")

    _edit_manifest(store, move)
    errors = _errors(store)
    assert "name must match" in errors and "path 'images/img'" in errors


def test_polygon_rings_must_be_ccw(cfg: Config, store: Path) -> None:
    v = zarr.open_array(store / "shapes/cells/vertices", mode="a")
    data = np.asarray(v[:])
    data[:, 0] = -data[:, 0]  # mirroring flips every ring's orientation
    v[:] = data
    assert "not counter-clockwise" in _errors(store)


def test_polygon_rings_must_be_closed(cfg: Config, store: Path) -> None:
    v = zarr.open_array(store / "shapes/cells/vertices", mode="a")
    v[0] = np.asarray(v[0]) + 1.0
    assert "not closed" in _errors(store)


def test_chunk_bbox_must_contain_points(cfg: Config, store: Path) -> None:
    a = zarr.open_array(store / "points/transcripts/chunk_bbox", mode="a")
    a[0] = np.array([0, 0, 1, 1], np.float32)
    assert "outside chunk_bbox" in _errors(store)


def test_lod_counts(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["points"]["transcripts"].update(lod_cap=10**6))
    assert "lod" in _errors(store)


def test_overview_must_be_materialised(cfg: Config, store: Path) -> None:
    g = zarr.open_group(store / "points/transcripts", mode="a", use_consolidated=False)
    del g["overview"]
    assert "overview" in _errors(store)


def test_section_key_must_be_categorical(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["tables"]["cells"].update(section_keys=["area"]))
    assert "section key 'area'" in _errors(store)


def test_categorical_listed_as_numeric(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["tables"]["cells"]["numeric"].append("cluster"))
    assert "obs/cluster is listed as numeric" in _errors(store)


def test_sparse_label_ids_need_relabelling(cfg: Config, store: Path) -> None:
    _edit_manifest(store, lambda m: m["labels"]["cells"].update(n_labels=3, max_label=5000))
    assert "must be relabelled" in _errors(store)


def test_labels_need_image_label_metadata(cfg: Config, store: Path) -> None:
    g = zarr.open_group(store / "labels/cells", mode="a", use_consolidated=False)
    ome = dict(g.attrs["ome"])  # type: ignore[arg-type]
    del ome["image-label"]
    g.attrs["ome"] = ome
    assert "image-label" in _errors(store)


def test_relabel() -> None:
    from canopy.contract.write import needs_relabel, relabel

    lab = np.array([[0, 7_000_000_000, 5], [5, -1, 7_000_000_000]], dtype=np.int64)
    new, original = relabel(lab)
    assert new.dtype == np.uint32 and original.dtype == np.uint64
    assert new.tolist() == [[0, 2, 1], [1, 0, 2]]
    assert original.tolist() == [0, 5, 7_000_000_000]
    assert needs_relabel(7_000_000_000, 2) and needs_relabel(10_000, 10)
    assert not needs_relabel(300, 300)


def test_polygon_layout_normalises_rings() -> None:
    from canopy.contract.write import polygon_layout, signed_area

    cw_open = np.array([[0, 0], [0, 1], [1, 1], [1, 0]], np.float32)  # clockwise, not closed
    ccw_closed = np.array([[5, 5], [6, 5], [6, 6], [5, 6], [5, 5]], np.float32)
    lay = polygon_layout([cw_open, ccw_closed], np.array([10, 11]))
    assert lay.starts.dtype == np.uint32 and lay.starts.tolist() == [0, 5, 10]
    for p in range(2):
        ring = lay.vertices[lay.starts[p] : lay.starts[p + 1]]
        assert np.array_equal(ring[0], ring[-1]) and signed_area(ring) > 0
    assert sorted(lay.row.tolist()) == [10, 11]


def test_points_layout_reduced_views() -> None:
    from canopy.contract.write import points_layout

    rng = np.random.default_rng(1)
    xy = rng.random((5000, 2)).astype(np.float32)
    feat = np.where(np.arange(5000) < 4000, 0, 1)  # gene 0 above the cap, gene 1 below
    lay = points_layout(xy, feat, 3, overview=500, lod_cap=1000)
    assert lay.offsets.tolist() == [0, 4000, 5000, 5000]
    assert lay.lod["offsets"].tolist() == [0, 1000, 1000, 1000]
    assert lay.overview["xy"].shape == (500, 2)
    assert lay.overview["offsets"][-1] == 500
    assert lay.chunk_bbox.shape == (1, 4)


def test_large_example_is_valid(cfg: Config, tmp_path: Path) -> None:
    """Several polygon blocks and point chunks."""
    p = write_example_store(tmp_path / "c0", n_cells=9000, n_points=60_000)
    rep = validate_store(p, deep=True)
    assert rep.ok, rep.errors


def test_pyramid_levels_rule() -> None:
    assert pyramid_levels((1100, 900)) == [(1100, 900), (550, 450), (275, 225)]
    assert pyramid_levels((512, 300)) == [(512, 300)]


def test_morton_order_is_spatially_local() -> None:
    x = np.array([0.0, 1.0, 0.0, 1.0])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    codes = morton_order(x, y, (0.0, 0.0, 1.0, 1.0))
    assert list(np.argsort(codes)) == [0, 1, 2, 3]


def test_example_refuses_foreign_folder(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "sub" / "thesis.txt").write_text("precious")
    with pytest.raises(FileExistsError):
        write_example_store(victim)
    assert (victim / "sub" / "thesis.txt").read_text() == "precious"
    # an empty folder and an existing store are fine
    empty = tmp_path / "empty"
    empty.mkdir()
    write_example_store(empty)
    write_example_store(empty)


def test_non_finite_manifest_numbers_fail_and_problems_hide_names(
    tmp_path: Path, store: Path
) -> None:
    from canopy.contract.validate import Report

    bad = tmp_path / "bad"
    shutil.copytree(store, bad)
    text = (bad / "manifest.json").read_text()
    m = json.loads(text)
    pts = next(iter(m["points"].values()))
    pts["bbox"][0] = float("nan")
    (bad / "manifest.json").write_text(json.dumps(m))
    rep = validate_store(bad)
    assert not rep.ok and any("non-finite" in e for e in rep.errors)

    r = Report(store="x")
    r.error("tables/my_table", "obs/patient_42 missing")
    r.error("images/img", "channel 'CD3 secret' window [0, 1] outside uint8")
    assert r.problems() == [
        {"element": "tables/my_table", "problem": "obs/<name> missing"},
        {"element": "images/img", "problem": "channel <name> window [0, 1] outside uint8"},
    ]
