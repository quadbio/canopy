"""Specifications of synthetic datasets (what to generate), independent of values."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Encoding = Literal["csr", "csc", "dense"]
Compression = Literal["gzip", "lzf", "zstd", "none"]


class ColumnSpec(BaseModel):
    name: str
    kind: Literal["categorical", "numeric", "boolean", "string"]
    dtype: str = "float32"
    n_categories: int = 10
    colors: bool = False


class MatrixSpec(BaseModel):
    encoding: Encoding = "csr"
    dtype: str = "float32"
    density: float = 0.05
    # 1-D chunk length of sparse data/indices (or row chunk of dense); None = library default.
    chunk: int | None = None
    compression: Compression = "gzip"
    seed: int = 0


class AnnDataSpec(BaseModel):
    n_obs: int
    n_vars: int
    X: MatrixSpec | None = Field(default_factory=MatrixSpec)
    layers: dict[str, MatrixSpec] = Field(default_factory=dict)
    obs: list[ColumnSpec] = Field(default_factory=list)
    var: list[ColumnSpec] = Field(default_factory=list)
    obsm: dict[str, int] = Field(default_factory=dict)  # name -> number of dimensions
    # uns["<key>_colors"] for keys that are not obs columns: key -> number of colours
    extra_colors: dict[str, int] = Field(default_factory=dict)
    obs_index_name: str | None = None
    var_index_name: str | None = None
    seed: int = 0


class TransformSpec(BaseModel):
    output: str
    type: Literal["identity", "affine", "scale", "translation", "sequence"] = "identity"


class ImageSpec(BaseModel):
    channels: list[str]
    height: int
    width: int
    dtype: str = "uint8"
    levels: int = 1
    chunk: int | None = None  # None = one chunk per level
    transforms: list[TransformSpec] = Field(
        default_factory=lambda: [TransformSpec(output="global")]
    )


class LabelsSpec(BaseModel):
    height: int
    width: int
    dtype: str = "int32"
    levels: int = 1
    chunk: int | None = None
    n_labels: int = 100
    transforms: list[TransformSpec] = Field(
        default_factory=lambda: [TransformSpec(output="global")]
    )


class PointsSpec(BaseModel):
    n_rows: int
    # Extra columns besides x, y: name -> kind ("category", "float", "int", "string").
    columns: dict[str, str] = Field(default_factory=dict)
    feature_key: str | None = None
    instance_key: str | None = None
    n_features: int = 50
    transforms: list[TransformSpec] = Field(
        default_factory=lambda: [TransformSpec(output="global")]
    )


class ShapesSpec(BaseModel):
    n_polygons: int
    index_name: str | None = None
    transforms: list[TransformSpec] = Field(
        default_factory=lambda: [TransformSpec(output="global")]
    )


class TableSpec(BaseModel):
    table: AnnDataSpec
    region: str
    region_key: str = "region"
    instance_key: str = "instance_id"


class SpatialDataSpec(BaseModel):
    images: dict[str, ImageSpec] = Field(default_factory=dict)
    labels: dict[str, LabelsSpec] = Field(default_factory=dict)
    points: dict[str, PointsSpec] = Field(default_factory=dict)
    shapes: dict[str, ShapesSpec] = Field(default_factory=dict)
    tables: dict[str, TableSpec] = Field(default_factory=dict)
    seed: int = 0
