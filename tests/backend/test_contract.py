from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import anndata as ad
import numpy as np
import pytest
import zarr

from dbx.config import Config
from dbx.contract import MANIFEST, validate_store
from dbx.contract.example import write_example_store
from dbx.contract.models import manifest_json_schema
from dbx.contract.write import morton_order, pyramid_levels

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
    assert set(a.uns["dbx"]["gene_stats"]) == {"nnz", "mean", "max", "p99"}


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
    a = zarr.open_array(store / "points" / "transcripts" / "feature", mode="a")
    v = np.asarray(a[:])
    a[:] = v[::-1].copy()
    assert "not sorted by feature" in _errors(store)


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
    _edit_manifest(store, lambda m: m.update(surprise=1))
    assert "surprise" in _errors(store)


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
