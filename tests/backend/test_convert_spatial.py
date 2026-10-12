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
    # transcripts point to their cell's table row: per gene, the same rows as in the source
    root = zarr.open_group(r.store, mode="r")
    cell = root["points/signal/cell"][...]
    off = root["points/signal/offsets"][...]
    feats = root["points/signal"].attrs["dbx"]["features"]
    sd = spatialdata.read_zarr(src, selection=("points", "tables"))
    t = sd.tables["table_cells"]
    key = t.uns["spatialdata_attrs"]["instance_key"]
    row_of = {v: i for i, v in enumerate(t.obs[key].to_numpy())}
    df = sd.points["signal"][["gene_id", key]].compute()
    for f in (0, 7, len(feats) - 1):
        ids = df.loc[df["gene_id"].astype(str) == feats[f], key].to_numpy()
        want = np.sort(np.array([row_of.get(v, -1) for v in ids]))
        np.testing.assert_array_equal(np.sort(cell[off[f] : off[f + 1]]), want)
    assert (cell >= 0).mean() > 0.3


# ---------------------------------------------------------------- hard cases (review S6)
def _sdata(
    path: Path,
    shapes: dict[str, tuple[list[object], str]],
    points: tuple[object, str] | None = None,
    table_regions: list[str] | None = None,
    ids: dict[str, list[int]] | None = None,
) -> Path:
    """A small SpatialData: shapes (geometries, coordinate system), optional points (frame,
    coordinate system) and one table over `table_regions` with instance ids `ids`."""
    import anndata as ad
    import geopandas as gpd
    import pandas as pd
    from spatialdata import SpatialData
    from spatialdata.models import PointsModel, ShapesModel, TableModel
    from spatialdata.transformations import Identity

    els: dict[str, object] = {}
    for name, (geoms, cs) in shapes.items():
        gdf = gpd.GeoDataFrame(geometry=geoms, index=(ids or {}).get(name))
        els[name] = ShapesModel.parse(gdf, transformations={cs: Identity()})
    pts = {}
    if points is not None:
        df, cs = points
        pts["tx"] = PointsModel.parse(
            df, feature_key="gene", instance_key="cell_id", transformations={cs: Identity()}
        )
    tables = {}
    if table_regions:
        region_col, inst = [], []
        for r in table_regions:
            n = len((ids or {}).get(r) or shapes[r][0])
            region_col += [r] * n
            inst += list((ids or {}).get(r) or range(n))
        obs = pd.DataFrame(
            {"region": pd.Categorical(region_col), "cell_id": np.asarray(inst, np.int64)},
            index=[f"c{i}" for i in range(len(inst))],
        )
        adata = ad.AnnData(X=np.ones((len(inst), 3), np.float32), obs=obs)
        tables["table"] = TableModel.parse(
            adata, region=table_regions, region_key="region", instance_key="cell_id"
        )
    SpatialData(shapes=els, points=pts, tables=tables).write(path)
    return path


def _square(x: float, y: float, s: float = 1.0) -> object:
    from shapely.geometry import Polygon

    return Polygon([(x, y), (x + s, y), (x + s, y + s), (x, y + s)])


def _convert(src: Path, tmp: Path, **opts: object) -> object:
    return convert_spatialdata(
        src,
        inspect_path(src),
        tmp / "cache",
        ConvertOptions(**opts),
        lambda s, f: None,  # type: ignore[arg-type]
    )


def test_multi_region_table_links_cells_per_coordinate_system(tmp_path: Path) -> None:
    import pandas as pd

    sq = [_square(i * 2, 0) for i in range(3)]
    tx = pd.DataFrame(
        {
            "x": [0.5, 2.5, 4.5, 0.5],
            "y": [0.5, 0.5, 0.5, 0.5],
            "gene": pd.Categorical(["a", "b", "a", "b"]),
            "cell_id": [0, 1, 2, 0],
        }
    )
    # sections in their own coordinate systems: the points (in "a") link to secA's rows
    src = _sdata(
        tmp_path / "own.zarr",
        {"secA": (sq, "a"), "secB": (sq, "b")},
        points=(tx, "a"),
        table_regions=["secA", "secB"],
    )
    r = _convert(src, tmp_path / "own")
    m = r.manifest  # type: ignore[attr-defined]
    t = m.tables["table"]
    assert t.region == ["secA", "secB"] and t.section_keys == ["region"]
    assert m.points["tx"].has_cell and m.points["tx"].table == "table"
    root = zarr.open_group(r.store, mode="r")  # type: ignore[attr-defined]
    cell = root["points/tx/cell"][...]
    xy = root["points/tx/xy"][...]
    # rows 0..2 are secA's cells 0..2 (secB's are rows 3..5): x 0.5 → 0, 2.5 → 1, 4.5 → 2
    assert sorted(zip(xy[:, 0].round(1).tolist(), cell.tolist(), strict=True)) == [
        (0.5, 0), (0.5, 0), (2.5, 1), (4.5, 2)
    ]  # fmt: skip
    # each region's shapes map to their own rows
    srow = root["shapes/secB/row"][...]
    assert sorted(srow.tolist()) == [3, 4, 5]

    # all in one coordinate system with repeating ids: ambiguous, so no link (never a guess)
    src2 = _sdata(
        tmp_path / "shared.zarr",
        {"secA": (sq, "global"), "secB": (sq, "global")},
        points=(tx, "global"),
        table_regions=["secA", "secB"],
    )
    r2 = _convert(src2, tmp_path / "shared")
    assert not r2.manifest.points["tx"].has_cell  # type: ignore[attr-defined]
    assert "points_cell_link_ambiguous" in r2.warnings  # type: ignore[attr-defined]


def test_messy_geometries_and_coordinates_convert(tmp_path: Path) -> None:
    import json

    import pandas as pd
    from shapely.geometry import LineString, MultiPolygon, Polygon

    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])  # self-intersecting
    flat = Polygon([(0, 0), (1, 0), (2, 0)])  # no area
    clockwise = Polygon([(5, 5), (5, 6), (6, 6), (6, 5)])
    multi = MultiPolygon([_square(10, 10), _square(20, 20)])
    geoms = [_square(0, 5), bowtie, flat, clockwise, multi, LineString([(0, 0), (1, 1)])]
    tx = pd.DataFrame(
        {
            "x": [0.5, np.nan, 2.0, 3.0],
            "y": [0.5, 1.0, np.inf, 3.0],
            "gene": pd.Categorical(["a", "a", "b", "b"]),
            "cell_id": [0, 1, 2, 3],
        }
    )
    src = _sdata(tmp_path / "m.zarr", {"cells": (geoms, "global")}, points=(tx, "global"))
    r = _convert(src, tmp_path)
    m = r.manifest  # type: ignore[attr-defined]
    w = set(r.warnings)  # type: ignore[attr-defined]
    assert {
        "shapes_invalid_repaired",
        "shapes_non_polygon_dropped",
        "points_non_finite_dropped",
    } <= w
    # the square, the bowtie's two triangles, the clockwise square, the two parts
    assert m.shapes["cells"].n == 6
    rows = zarr.open_group(r.store, mode="r")["shapes/cells/row"][...]  # type: ignore[attr-defined]
    assert sorted(rows.tolist()) == [0, 1, 1, 3, 4, 4]
    assert m.points["tx"].n == 2
    json.loads((r.store / "manifest.json").read_text(), parse_constant=lambda c: 1 / 0)  # type: ignore[attr-defined]
    assert validate_store(r.store, deep=True).ok  # type: ignore[attr-defined]


def test_sparse_and_uint64_labels_relabel_with_lookup(tmp_path: Path) -> None:
    import anndata as ad
    import pandas as pd
    from spatialdata import SpatialData
    from spatialdata.models import Labels2DModel, TableModel

    big = np.uint64(2**63 + 5)
    mask = np.zeros((600, 700), np.uint64)
    mask[10:20, 10:20] = 7
    mask[300:320, 600:650] = big
    mask[500:510, 0:10] = 3_000_000_000
    lab = Labels2DModel.parse(mask, dims=("y", "x"), chunks=(256, 256))
    obs = pd.DataFrame(
        {"region": pd.Categorical(["m"] * 3), "id": np.array([7, 3_000_000_000, 99], np.uint64)},
        index=["a", "b", "c"],
    )
    table = TableModel.parse(
        ad.AnnData(X=np.ones((3, 2), np.float32), obs=obs),
        region="m",
        region_key="region",
        instance_key="id",
    )
    src = tmp_path / "l.zarr"
    SpatialData(labels={"m": lab}, tables={"table": table}).write(src)
    r = _convert(src, tmp_path)
    e = r.manifest.labels["m"]  # type: ignore[attr-defined]
    assert e.relabelled and e.n_labels == 3 and e.max_label == 3
    root = zarr.open_group(r.store, mode="r")  # type: ignore[attr-defined]
    orig = root["aux/labels/m/original_id"][...]
    assert orig.tolist() == [0, 7, 3_000_000_000, int(big)]
    lv0 = root["labels/m/0"][...]
    assert lv0[15, 15] == 1 and lv0[505, 5] == 2 and lv0[310, 620] == 3 and lv0[0, 0] == 0
    assert root["aux/labels/m/row"][...].tolist() == [-1, 0, 1, -1]
    lv1 = root["labels/m/1"][...]  # nearest: every second pixel
    np.testing.assert_array_equal(lv1, lv0[::2, ::2])


def test_large_image_levels_are_sharded(tmp_path: Path) -> None:
    from spatialdata import SpatialData
    from spatialdata.models import Image2DModel

    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (1, 300, 8400), dtype=np.uint8)
    src = tmp_path / "i.zarr"
    SpatialData(
        images={"wide": Image2DModel.parse(img, dims=("c", "y", "x"), chunks=(1, 300, 1000))}
    ).write(src)
    r = _convert(src, tmp_path, threads=2)
    root = zarr.open_group(r.store, mode="r")  # type: ignore[attr-defined]
    a0, a1 = root["images/wide/0"], root["images/wide/1"]
    assert a0.shards == (1, 300, 4096) and a0.chunks == (1, 300, 512)
    assert a1.shards is None
    np.testing.assert_array_equal(a0[...], img)
    assert validate_store(r.store, deep=True).ok  # type: ignore[attr-defined]


def test_feature_column_from_option_or_text_names() -> None:
    import pandas as pd

    df = pd.DataFrame(
        {
            "target": ["a", "b"],
            "fov": pd.Categorical(["1", "2"]),
            "codeword": pd.Categorical(["x", "y"]),
        }
    )
    assert feature_column(df, None, set()) == ("target", True)  # text column named like genes
    assert feature_column(df, None, set(), chosen="codeword") == ("codeword", True)
    with pytest.raises(ConversionError, match="choose"):
        feature_column(df.drop(columns="target"), None, set())
    with pytest.raises(ConversionError):
        feature_column(df, None, set(), chosen="nope")


def test_spatial_names_never_collide_or_hide() -> None:
    from dbx.convert.spatialdata import unique_names

    assert safe_name("..") == "_.." and safe_name(".hidden") == "_.hidden"
    assert unique_names(["a b", "a/b", "a_b"]) == {"a b": "a_b", "a/b": "a_b_2", "a_b": "a_b_3"}
