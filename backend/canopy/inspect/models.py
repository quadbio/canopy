"""Fingerprint models: the structure of a dataset, never its values.

A fingerprint is what the crawler shows, what the loaders decide on, what diagnostic bundles
carry off the host, and what the mock generator rebuilds a look-alike file from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FINGERPRINT_VERSION = 2

DatasetKind = Literal["h5ad", "anndata_zarr", "spatialdata_zarr"]
MatrixEncoding = Literal["dense", "csr", "csc"]
ColumnKind = Literal["categorical", "numeric", "boolean", "string", "other"]
# "pre-0.7": obs/var as compound datasets, h5sparse X (anndata < 0.7);
# "0.7": categories referenced from <frame>/__categories (anndata 0.7.x).
AnnDataLayout = Literal["current", "0.7", "pre-0.7"]


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
    layout: AnnDataLayout = "current"
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


class Notice(BaseModel):
    """A structural fact that changes how the dataset can be served. Names are kept in `element`
    only (never inside `message`), so strict redaction can replace them reliably."""

    code: str
    message: str
    kind: str | None = None  # "table", "image", "labels", "points", "shapes"
    element: str | None = None


class ElementError(BaseModel):
    """Part of a dataset that could not be described; the rest of the fingerprint is valid."""

    kind: str
    element: str
    error_type: str
    detail: str | None = None  # exception text (truncated); dropped at strict level


class Fingerprint(BaseModel):
    fingerprint_version: int = FINGERPRINT_VERSION
    dataset_id: str
    # What the version is tied to besides the content: "<root name>/<path in the root>" for a
    # dataset of the index (moving the whole data folder keeps it); None: the real path
    identity: str | None = None
    kind: DatasetKind
    path: str
    name: str
    # None when the size was not measured (directory stores on network storage are counted
    # lazily by the crawler).
    size_bytes: int | None = None
    size_exact: bool = True
    mtime: float
    anndata: AnnDataInfo | None = None
    spatialdata: SpatialDataInfo | None = None
    warnings: list[Notice] = Field(default_factory=list)
    errors: list[ElementError] = Field(default_factory=list)
    redacted: bool = False
