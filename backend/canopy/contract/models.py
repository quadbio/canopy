"""Manifest of a canopy store (data contract v0.2). See contract/SPEC.md.

The JSON schema in contract/schema/manifest.schema.json is generated from these models
(`canopy contract schema`); a test keeps it in sync.

These models are the *reader* view: unknown fields are ignored so that stores written by a newer
minor version still open (SPEC §9). `canopy validate` reports unknown fields separately.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT: Final = "canopy-store"
CONTRACT_VERSION = "0.2.0"
CONTRACT_MAJOR = 0
CONTRACT_MINOR = 2

# Layout constants (SPEC §4–§8)
X_CHUNK = 262_144
INDPTR_CHUNK = 65_536
TILE = 512
IMAGE_SHARD = 4_096  # shards of image levels larger than IMAGE_SHARD_ABOVE px (SPEC §5)
IMAGE_SHARD_ABOVE = 8_192
ROW_CHUNK = 1_048_576
OBSM_CHUNK = 65_536
DOWNSAMPLE_THRESHOLD = 200_000
DOWNSAMPLE_SIZES = (200_000, 1_000_000)
OVERVIEW_POINTS = 1_000_000
ZSTD_LEVEL = 3
SHARD_CHUNKS = 64  # chunks per shard file (SPEC §1.7)
POINT_CHUNK = 16_384
LOD_CAP = 100_000
POLY_BLOCK = 4_096
VERTEX_CHUNK = 65_536
NAME_PATTERN = r"^[A-Za-z0-9._-]+$"

Affine = list[list[float]]  # 3x3, row-major, homogeneous 2-D
Unit = Literal["micrometer", "pixel", "unknown"]


class _Strict(BaseModel):
    # Readers ignore unknown fields (forward compatibility, SPEC §9).
    model_config = ConfigDict(extra="ignore")


def _check_affine(m: Affine) -> Affine:
    if len(m) != 3 or any(len(r) != 3 for r in m):
        raise ValueError("transform must be a 3x3 matrix")
    if [round(v, 12) for v in m[2]] != [0.0, 0.0, 1.0]:
        raise ValueError("last row of a 2-D affine must be [0, 0, 1]")
    return m


class _Spatial(_Strict):
    path: str
    source_name: str | None = None
    transforms: dict[str, Affine] = Field(default_factory=dict)

    @field_validator("transforms")
    @classmethod
    def _affines(cls, v: dict[str, Affine]) -> dict[str, Affine]:
        for m in v.values():
            _check_affine(m)
        return v


class SourceInfo(_Strict):
    path: str
    kind: Literal["h5ad", "anndata_zarr", "spatialdata_zarr"]
    size_bytes: int
    mtime: float
    fingerprint_version: int


class ConverterInfo(_Strict):
    canopy_version: str
    created: str  # ISO 8601 UTC
    duration_s: float
    peak_rss_gb: float | None = None
    warnings: list[str] = Field(default_factory=list)


class CategoricalInfo(_Strict):
    n_categories: int
    has_colors: bool = False


class TableEntry(_Strict):
    path: str
    source_name: str | None = None
    n_obs: int
    n_vars: int
    nnz: int
    x_source: str = "X"
    x_dtype: Literal["float32"] = "float32"
    embeddings: dict[str, int] = Field(default_factory=dict)
    spatial_keys: list[str] = Field(default_factory=list)
    categorical: dict[str, CategoricalInfo] = Field(default_factory=dict)
    numeric: list[str] = Field(default_factory=list)
    var_names_column: str | None = None
    downsample: list[int] = Field(default_factory=list)
    region: list[str] = Field(default_factory=list)
    region_key: str | None = None
    instance_key: str | None = None
    section_keys: list[str] = Field(default_factory=list)


class ChannelInfo(_Strict):
    name: str
    window: tuple[float, float]
    color: str | None = None  # "#rrggbb" hint


class ImageEntry(_Spatial):
    axes: list[str]
    dtype: str
    levels: list[list[int]]
    channels: list[ChannelInfo]
    rgb: bool = False


class LabelsEntry(_Spatial):
    dtype: Literal["uint32"] = "uint32"
    levels: list[list[int]]
    max_label: int
    n_labels: int
    relabelled: bool = False
    table: str | None = None
    lookup: str | None = None


class ShapesEntry(_Spatial):
    kind: Literal["polygons", "circles"]
    n: int
    n_vertices: int | None = None  # polygons only
    table: str | None = None


class PointsEntry(_Spatial):
    n: int
    n_features: int
    feature_key: str
    feature_key_inferred: bool = False
    has_cell: bool = False
    has_z: bool = False
    z_range: tuple[float, float] | None = None
    z_planes: list[float] | None = None
    bbox: tuple[float, float, float, float]
    n_overview: int
    lod_cap: int = LOD_CAP
    table: str | None = None


class Manifest(_Strict):
    contract: Literal["canopy-store"] = CONTRACT
    contract_version: str = CONTRACT_VERSION
    dataset_id: str
    source: SourceInfo
    converter: ConverterInfo
    coordinate_systems: list[str] = Field(default_factory=list)
    units: dict[str, Unit] = Field(default_factory=dict)
    tables: dict[str, TableEntry] = Field(default_factory=dict)
    images: dict[str, ImageEntry] = Field(default_factory=dict)
    labels: dict[str, LabelsEntry] = Field(default_factory=dict)
    shapes: dict[str, ShapesEntry] = Field(default_factory=dict)
    points: dict[str, PointsEntry] = Field(default_factory=dict)

    @field_validator("contract_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("contract_version must be MAJOR.MINOR.PATCH")
        return v

    @property
    def major(self) -> int:
        return int(self.contract_version.split(".")[0])

    @property
    def minor(self) -> int:
        return int(self.contract_version.split(".")[1])


def manifest_json_schema() -> dict[str, object]:
    """JSON schema of the manifest (written to contract/schema/manifest.schema.json)."""
    schema = Manifest.model_json_schema()
    schema["$id"] = f"https://canopy.local/contract/{CONTRACT_VERSION}/manifest.schema.json"
    schema["title"] = f"canopy store manifest (contract {CONTRACT_VERSION})"
    return schema
