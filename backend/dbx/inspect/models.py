"""Fingerprint models: the structure of a dataset, never its values.

A fingerprint is what the crawler shows, what the loaders decide on, what diagnostic bundles
carry off the host, and what the mock generator rebuilds a look-alike file from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FINGERPRINT_VERSION = 1

DatasetKind = Literal["h5ad", "anndata_zarr", "spatialdata_zarr"]
MatrixEncoding = Literal["dense", "csr", "csc"]
ColumnKind = Literal["categorical", "numeric", "boolean", "string", "other"]


class ArrayInfo(BaseModel):
    shape: list[int]
    dtype: str
    chunks: list[int] | None = None
    shards: list[int] | None = None
    compression: str | None = None


class MatrixInfo(BaseModel):
    encoding: MatrixEncoding
    shape: list[int]
    dtype: str
    nnz: int | None = None
    # For sparse: the `data` array's storage; for dense: the array itself.
    storage: ArrayInfo
    indices_dtype: str | None = None
    indptr_dtype: str | None = None


class ColumnInfo(BaseModel):
    name: str
    kind: ColumnKind
    dtype: str
    n_categories: int | None = None
    ordered: bool | None = None
    has_colors: bool = False


class FrameInfo(BaseModel):
    n_rows: int
    index_name: str | None = None
    columns: list[ColumnInfo] = Field(default_factory=list)


class AnnDataInfo(BaseModel):
    encoding_version: str | None = None
    n_obs: int
    n_vars: int
    X: MatrixInfo | None = None
    layers: dict[str, MatrixInfo] = Field(default_factory=dict)
    raw: MatrixInfo | None = None
    raw_n_vars: int | None = None
    obs: FrameInfo
    var: FrameInfo
    obsm: dict[str, ArrayInfo] = Field(default_factory=dict)
    varm: dict[str, ArrayInfo] = Field(default_factory=dict)
    obsp: dict[str, MatrixInfo] = Field(default_factory=dict)
    uns_keys: list[str] = Field(default_factory=list)
    # uns["<col>_colors"] lengths, keyed by column name.
    colors: dict[str, int] = Field(default_factory=dict)


class CoordinateTransform(BaseModel):
    """One transform of an element into a named coordinate system."""

    output: str
    type: str
    # Flattened parameters (affine matrix rows, scale or translation vector), as stored.
    params: list[list[float]] | list[float] | None = None


class ImageInfo(BaseModel):
    axes: list[str]
    levels: list[ArrayInfo]
    channel_names: list[str] | None = None
    transforms: list[CoordinateTransform] = Field(default_factory=list)


class TableRef(BaseModel):
    region: list[str] = Field(default_factory=list)
    region_key: str | None = None
    instance_key: str | None = None


class ParquetInfo(BaseModel):
    n_rows: int
    n_files: int
    columns: dict[str, str]  # name -> arrow type
    feature_key: str | None = None
    instance_key: str | None = None
    geometry_types: list[str] | None = None
    transforms: list[CoordinateTransform] = Field(default_factory=list)


class SpatialDataInfo(BaseModel):
    format_version: str | None = None
    software_version: str | None = None
    images: dict[str, ImageInfo] = Field(default_factory=dict)
    labels: dict[str, ImageInfo] = Field(default_factory=dict)
    points: dict[str, ParquetInfo] = Field(default_factory=dict)
    shapes: dict[str, ParquetInfo] = Field(default_factory=dict)
    tables: dict[str, AnnDataInfo] = Field(default_factory=dict)
    table_refs: dict[str, TableRef] = Field(default_factory=dict)
    coordinate_systems: list[str] = Field(default_factory=list)


class Fingerprint(BaseModel):
    fingerprint_version: int = FINGERPRINT_VERSION
    dataset_id: str
    kind: DatasetKind
    path: str
    name: str
    size_bytes: int
    size_exact: bool = True
    mtime: float
    anndata: AnnDataInfo | None = None
    spatialdata: SpatialDataInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    redacted: bool = False
