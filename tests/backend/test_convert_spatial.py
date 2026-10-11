"""The SpatialData converter (SpatialData zarr → dbx store, SPEC §5–§8)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import spatialdata
import zarr
from test_convert import _wait
from test_datasets import client_for

from dbx.catalog import entry_id
from dbx.config import Config
from dbx.contract import read_manifest, validate_store
from dbx.convert.anndata import ConvertOptions
from dbx.convert.matrix import ConversionError
from dbx.convert.spatialdata import convert_spatialdata, feature_column, safe_name
from dbx.inspect import inspect_path
from dbx.synth import write_spatialdata
from dbx.synth.spec import (
    AnnDataSpec,
    ColumnSpec,
    ImageSpec,
    LabelsSpec,
    PointsSpec,
    ShapesSpec,
    SpatialDataSpec,
    TableSpec,
)


def synthetic(path: Path) -> Path:
    spec = SpatialDataSpec(
        images={
            "img": ImageSpec(channels=["DAPI", "PolyT"], height=700, width=600, dtype="uint16")
        },
        labels={"cells": LabelsSpec(height=700, width=600, n_labels=60)},
        points={
            "tx": PointsSpec(
                n_rows=20_000, columns={"gene": "category"}, feature_key="gene", n_features=12
            )
        },
        shapes={"outlines": ShapesSpec(n_polygons=80)},
        tables={
            "table": TableSpec(
                table=AnnDataSpec(
                    n_obs=60,
                    n_vars=15,
                    obs=[
                        ColumnSpec(name="cluster", kind="categorical", n_categories=4, colors=True)
                    ],
                ),
                region="cells",
            )
        },
    )
    return write_spatialdata(spec, path)


def test_synthetic_spatialdata_converts_faithfully(tmp_path: Path) -> None:
    src = synthetic(tmp_path / "s.zarr")
    fp = inspect_path(src)
    r = convert_spatialdata(src, fp, tmp_path / "cache", ConvertOptions(), lambda s, f: None)
    assert validate_store(r.store, deep=True).ok
    m = read_manifest(r.store)
    sd = spatialdata.read_zarr(src)
    root = zarr.open_group(r.store, mode="r")

    # image: level 0 equals the source, the pyramid ends at ≤ 512 px
    img = m.images["img"]
    assert [c.name for c in img.channels] == ["DAPI", "PolyT"] and not img.rgb
    np.testing.assert_array_equal(root["images/img/0"][...], np.asarray(sd.images["img"].data))
    assert max(img.levels[-1][-2:]) <= 512

    # labels: the table annotates them; label v → its table row
    lab = m.labels["cells"]
    assert lab.table == "table" and lab.lookup == "aux/labels/cells/row"
    lookup = root["aux/labels/cells/row"][...]
    ids = sd.tables["table"].obs["instance_id"].to_numpy()
    for row, v in enumerate(ids[:10]):
        assert lookup[int(v)] == row

    # shapes: every polygon, closed and in Morton order (validated); no table → own index
    assert m.shapes["outlines"].n == 80 and m.shapes["outlines"].table is None

    # points: all of them, grouped by gene
    pts = m.points["tx"]
    assert pts.n == 20_000 and pts.feature_key == "gene" and not pts.feature_key_inferred
    off = root["points/tx/offsets"][...]
    genes = sd.points["tx"]["gene"].compute()
    feats = root["points/tx"].attrs["dbx"]["features"]
    for f in (0, 5):
        assert off[f + 1] - off[f] == int((genes == feats[f]).sum())
    assert m.coordinate_systems == ["global"]
    assert m.images["img"].transforms["global"] == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def test_feature_column_is_declared_or_the_only_category() -> None:
    import pandas as pd

    df = pd.DataFrame(
        {
            "gene_id": pd.Categorical(["a", "b"]),
            "cell_id": pd.Categorical(["c1", "c2"]),
            "tile": ["t", "t"],
        }
    )
    assert feature_column(df, None, {"cell_id"}) == ("gene_id", True)
    assert feature_column(df, "tile", set()) == ("tile", False)
    assert feature_column(df, None, set()) == ("gene_id", True)  # named like genes
    df["other"] = pd.Categorical(["x", "y"])
    df = df.rename(columns={"gene_id": "kind"})
    with pytest.raises(ConversionError):
        feature_column(df, None, set())
    assert safe_name("my cells/1") == "my_cells_1"


def test_spatialdata_conversion_job_and_serving(cfg: Config) -> None:
    synthetic(cfg.data_roots[0].path / "s.zarr")
    c = client_for(cfg)
    id = entry_id("data", "s.zarr")
    assert (
        c.get(f"/api/datasets/{id}/meta", params={"table": "table"}).json()["mode"] != "converted"
    )
    job = c.post(f"/api/datasets/{id}/convert", json={}).json()
    done = _wait(c, job["id"], c.app)
    assert done["state"] == "done", done
    meta = c.get(f"/api/datasets/{id}/meta", params={"table": "table"}).json()
    assert meta["mode"] == "converted"
    assert meta["matrices"][0]["access"] == "direct"
    detail = c.get(f"/api/catalog/datasets/{id}").json()
    assert detail["conversion"]["converted"]


def test_merscope_example_converts(tmp_path: Path, example) -> None:  # type: ignore[no-untyped-def]
    src = example("202302171205_region_1_scwf.zarr")
    r = convert_spatialdata(
        src, inspect_path(src), tmp_path / "cache", ConvertOptions(), lambda s, f: None
    )
    m = r.manifest
    assert (
        m.shapes["boundaries_cells"].n == 6125
        and m.shapes["boundaries_cells"].table == "table_cells"
    )
    assert m.shapes["boundaries_tiles"].n == 81787
    pts = m.points["signal"]
    assert pts.n == 5_565_992 and pts.feature_key == "gene_id" and pts.feature_key_inferred
    assert pts.has_cell and pts.table == "table_cells"
    assert set(m.coordinate_systems) == {"global", "rot"}
    # transcripts point to their cell's table row
    cell = zarr.open_group(r.store, mode="r")["points/signal/cell"][:200_000]
    assert ((cell >= -1) & (cell < 6125)).all() and (cell >= 0).mean() > 0.3
