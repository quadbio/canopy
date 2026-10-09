"""Describe a SpatialData zarr store from metadata only (zarr attributes, parquet footers)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from dbx.inspect.anndata_info import anndata_info
from dbx.inspect.models import (
    CoordinateTransform,
    ImageInfo,
    ParquetInfo,
    SpatialDataInfo,
    TableRef,
)
from dbx.inspect.nodes import Node, attr_list


def _transforms(raw: list[dict[str, Any]] | None) -> list[CoordinateTransform]:
    out: list[CoordinateTransform] = []
    for t in raw or []:
        output = t.get("output")
        name = output.get("name") if isinstance(output, dict) else output
        ttype = str(t.get("type"))
        params = t.get(ttype) if ttype in ("affine", "scale", "translation") else None
        out.append(CoordinateTransform(output=str(name), type=ttype, params=params))
    return out


def _image(node: Node) -> ImageInfo | None:
    attrs = node.attrs
    ome = attrs.get("ome", attrs)  # NGFF >= 0.5 nests metadata under "ome"
    ms = (ome.get("multiscales") or [None])[0]
    if not ms:
        return None
    axes = [a["name"] if isinstance(a, dict) else str(a) for a in ms.get("axes", [])]
    levels = [node[d["path"]].array_info() for d in ms.get("datasets", []) if d["path"] in node]
    channels = None
    omero = ome.get("omero")
    if isinstance(omero, dict) and omero.get("channels"):
        channels = [str(c.get("label", i)) for i, c in enumerate(omero["channels"])]
    return ImageInfo(
        axes=axes,
        levels=levels,
        channel_names=channels,
        transforms=_transforms(ms.get("coordinateTransformations")),
    )


def _parquet_files(element_dir: Path, name: str) -> list[Path]:
    p = element_dir / name
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(f for f in p.rglob("*.parquet") if f.is_file())
    return []


def _parquet(element_dir: Path, filename: str, attrs: dict[str, Any]) -> ParquetInfo:
    files = _parquet_files(element_dir, filename)
    n_rows = 0
    columns: dict[str, str] = {}
    geometry_types: list[str] | None = None
    for i, f in enumerate(files):
        meta = pq.read_metadata(f)
        n_rows += meta.num_rows
        if i == 0:
            schema = pq.read_schema(f)
            columns = {
                n: str(schema.field(n).type)
                for n in schema.names
                if not n.startswith("__null_dask_index")
            }
            geo = (schema.metadata or {}).get(b"geo")
            if geo:
                import json

                geo_cols = json.loads(geo).get("columns", {})
                types = {t for c in geo_cols.values() for t in c.get("geometry_types", [])}
                geometry_types = sorted(types)
    sd_attrs = attrs.get("spatialdata_attrs") or {}
    return ParquetInfo(
        n_rows=n_rows,
        n_files=len(files),
        columns=columns,
        feature_key=sd_attrs.get("feature_key"),
        instance_key=sd_attrs.get("instance_key"),
        geometry_types=geometry_types,
        transforms=_transforms(attrs.get("coordinateTransformations")),
    )


def spatialdata_info(root: Node, path: Path) -> SpatialDataInfo:
    sd_attrs = root.attrs.get("spatialdata_attrs") or {}
    info = SpatialDataInfo(
        format_version=sd_attrs.get("version"),
        software_version=sd_attrs.get("spatialdata_software_version"),
    )
    for kind in ("images", "labels"):
        if kind not in root:
            continue
        for name, node in root[kind].items():
            if node.is_group and (img := _image(node)) is not None:
                getattr(info, kind)[name] = img
    if "points" in root:
        for name, node in root["points"].items():
            info.points[name] = _parquet(path / "points" / name, "points.parquet", node.attrs)
    if "shapes" in root:
        for name, node in root["shapes"].items():
            info.shapes[name] = _parquet(path / "shapes" / name, "shapes.parquet", node.attrs)
    if "tables" in root:
        for name, node in root["tables"].items():
            info.tables[name] = anndata_info(node)
            a = node.attrs
            info.table_refs[name] = TableRef(
                region=attr_list(a, "region"),
                region_key=a.get("region_key"),
                instance_key=a.get("instance_key"),
            )
    systems: set[str] = set()
    elements: list[ImageInfo | ParquetInfo] = [
        *info.images.values(),
        *info.labels.values(),
        *info.points.values(),
        *info.shapes.values(),
    ]
    for el in elements:
        systems.update(t.output for t in el.transforms)
    info.coordinate_systems = sorted(systems)
    return info
