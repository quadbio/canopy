"""Write synthetic SpatialData stores with the official spatialdata models."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import anndata as ad
import dask.array as da
import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from spatialdata import SpatialData
from spatialdata.models import (
    Image2DModel,
    Labels2DModel,
    PointsModel,
    ShapesModel,
    TableModel,
)
from spatialdata.transformations import Affine, Identity, Scale, Translation

from dbx.logs import timed
from dbx.synth.anndata_gen import _frame
from dbx.synth.spec import (
    AnnDataSpec,
    ImageSpec,
    LabelsSpec,
    PointsSpec,
    ShapesSpec,
    SpatialDataSpec,
    TransformSpec,
)

ROTATION = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])


def _transform(t: TransformSpec) -> Any:
    if t.type == "affine":
        return Affine(ROTATION, input_axes=("x", "y"), output_axes=("x", "y"))
    if t.type == "scale":
        return Scale([2.0, 2.0], axes=("x", "y"))
    if t.type == "translation":
        return Translation([10.0, 10.0], axes=("x", "y"))
    return Identity()


def _transforms(ts: list[TransformSpec]) -> dict[str, Any]:
    return {t.output: _transform(t) for t in ts}


def _scale_factors(levels: int) -> list[int] | None:
    return [2] * (levels - 1) if levels > 1 else None


def _image(spec: ImageSpec, rng: np.random.Generator) -> Any:
    shape = (len(spec.channels), spec.height, spec.width)
    chunks = (len(spec.channels), spec.chunk or spec.height, spec.chunk or spec.width)
    info = np.iinfo(spec.dtype) if np.dtype(spec.dtype).kind in "iu" else None
    hi = min(info.max, 255) if info else 1.0
    arr = da.from_array(rng.integers(0, int(hi) + 1, shape).astype(spec.dtype), chunks=chunks)
    return Image2DModel.parse(
        arr,
        dims=("c", "y", "x"),
        c_coords=spec.channels,
        scale_factors=_scale_factors(spec.levels),
        chunks=chunks,
        transformations=_transforms(spec.transforms),
    )


def _labels(spec: LabelsSpec, rng: np.random.Generator) -> Any:
    side = max(1, int(np.sqrt(spec.n_labels)))
    yy = np.arange(spec.height)[:, None] * side // max(spec.height, 1)
    xx = np.arange(spec.width)[None, :] * side // max(spec.width, 1)
    arr = (yy * side + xx + 1).astype(spec.dtype)
    chunks = (spec.chunk or spec.height, spec.chunk or spec.width)
    return Labels2DModel.parse(
        da.from_array(arr, chunks=chunks),
        dims=("y", "x"),
        scale_factors=_scale_factors(spec.levels),
        chunks=chunks,
        transformations=_transforms(spec.transforms),
    )


def _points(spec: PointsSpec, rng: np.random.Generator, extent: float) -> Any:
    n = spec.n_rows
    df = pd.DataFrame({"x": rng.random(n) * extent, "y": rng.random(n) * extent})
    for name, kind in spec.columns.items():
        if kind == "category":
            k = spec.n_features if name == spec.feature_key else 20
            df[name] = pd.Categorical.from_codes(
                rng.integers(0, k, n), categories=[f"{name}_{i}" for i in range(k)]
            )
        elif kind == "int":
            df[name] = rng.integers(0, 1000, n)
        elif kind == "float":
            df[name] = rng.random(n)
        else:
            df[name] = [f"{name}_{i % 97}" for i in range(n)]
    return PointsModel.parse(
        df,
        coordinates={"x": "x", "y": "y"},
        feature_key=spec.feature_key,
        instance_key=spec.instance_key,
        transformations=_transforms(spec.transforms),
    )


def _shapes(spec: ShapesSpec, rng: np.random.Generator, extent: float) -> Any:
    n = spec.n_polygons
    cx, cy = rng.random(n) * extent, rng.random(n) * extent
    r = extent / max(np.sqrt(n), 1) / 3
    polys = [
        shapely.Polygon([(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r)])
        for x, y in zip(cx, cy, strict=True)
    ]
    index = pd.Index([f"s{i}" for i in range(n)], name=spec.index_name)
    gdf = gpd.GeoDataFrame(geometry=polys, index=index)
    return ShapesModel.parse(gdf, transformations=_transforms(spec.transforms))


def _table(
    spec: AnnDataSpec,
    region: str,
    region_key: str,
    instance_key: str,
    instances: pd.Index,
    rng: np.random.Generator,
) -> Any:
    from dbx.synth.matrix import SynthMatrix

    n = len(instances)
    obs = _frame(spec.obs, n, "cell", spec.obs_index_name, rng)
    var = _frame(spec.var, spec.n_vars, "gene", spec.var_index_name, rng)

    def matrix(m: Any) -> Any:
        x = SynthMatrix(n, spec.n_vars, m.density, seed=m.seed, dtype=m.dtype).rows(0, n)
        if m.encoding == "csc":
            return x.tocsc()
        return x.toarray() if m.encoding == "dense" else x

    a = ad.AnnData(X=matrix(spec.X) if spec.X is not None else None, obs=obs, var=var)
    for name, m in spec.layers.items():
        a.layers[name] = matrix(m)
    for name, dims in spec.obsm.items():
        a.obsm[name] = rng.normal(size=(n, dims)).astype("float32")
    for c in spec.obs:
        if c.kind == "categorical" and c.colors:
            k = len(a.obs[c.name].cat.categories)
            a.uns[f"{c.name}_colors"] = [f"#{int(v):06x}" for v in rng.integers(0, 0xFFFFFF, k)]
    for key, k in spec.extra_colors.items():
        a.uns[f"{key}_colors"] = [f"#{int(v):06x}" for v in rng.integers(0, 0xFFFFFF, k)]
    a.obs[region_key] = pd.Categorical([region] * n)
    a.obs[instance_key] = instances.to_numpy()
    return TableModel.parse(a, region=region, region_key=region_key, instance_key=instance_key)


def write_spatialdata(spec: SpatialDataSpec, path: Path) -> Path:
    rng = np.random.default_rng(spec.seed)
    path = Path(path)
    with timed("synth_spatialdata", path=str(path)):
        images = {k: _image(v, rng) for k, v in spec.images.items()}
        labels = {k: _labels(v, rng) for k, v in spec.labels.items()}
        extent = float(max([v.width for v in spec.images.values()] + [1000]))
        points = {k: _points(v, rng, extent) for k, v in spec.points.items()}
        shapes = {k: _shapes(v, rng, extent) for k, v in spec.shapes.items()}
        tables = {}
        for k, t in spec.tables.items():
            instances = (
                shapes[t.region].index
                if t.region in shapes
                else pd.Index(np.arange(1, t.table.n_obs + 1))
            )
            tables[k] = _table(t.table, t.region, t.region_key, t.instance_key, instances, rng)
        sdata = SpatialData(
            images=images, labels=labels, points=points, shapes=shapes, tables=tables
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sdata.write(path, overwrite=True)
    return path
