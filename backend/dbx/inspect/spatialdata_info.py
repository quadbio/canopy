"""Describe a SpatialData zarr store from metadata only (zarr attributes, parquet footers)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from dbx.inspect.anndata_info import anndata_info
from dbx.inspect.models import (
    CoordinateTransform,
    ElementError,
    ImageInfo,
    ParquetInfo,
    SpatialDataInfo,
    TableRef,
)
from dbx.inspect.nodes import Node, attr_list
from dbx.logs import get_logger
from dbx.privacy import scrub_text

DETAIL_CHARS = 200


def _guarded[T](
    errors: list[ElementError], kind: str, name: str, fn: Callable[[], T | None]
) -> T | None:
    """Describe one element; a failure is recorded and the inspection continues."""
    try:
        return fn()
    except Exception as e:
        errors.append(
            ElementError(
                kind=kind,
                element=name,
                error_type=type(e).__name__,
                detail=scrub_text(str(e))[:DETAIL_CHARS],
            )
        )
        get_logger("dbx.inspect").warning(
            "element_unreadable", kind=kind, error_type=type(e).__name__
        )
        return None


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
    if not levels:
        raise ValueError("multiscales lists no readable levels")
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


def spatialdata_info(root: Node, path: Path, errors: list[ElementError]) -> SpatialDataInfo:
    sd_attrs = root.attrs.get("spatialdata_attrs") or {}
    info = SpatialDataInfo(
        format_version=sd_attrs.get("version"),
        software_version=sd_attrs.get("spatialdata_software_version"),
    )
    for kind in ("images", "labels"):
        if kind not in root:
            continue
        for name, node in root[kind].items():
            if not node.is_group:
                continue
            img = _guarded(errors, kind, name, lambda node=node: _image(node))  # type: ignore[misc]
            if img is not None:
                getattr(info, kind)[name] = img
    for kind in ("points", "shapes"):
        if kind not in root:
            continue
        for name, node in root[kind].items():
            pq_info = _guarded(
                errors,
                kind,
                name,
                lambda name=name, node=node, kind=kind: _parquet(  # type: ignore[misc]
                    path / kind / name, f"{kind}.parquet", node.attrs
                ),
            )
            if pq_info is not None:
                getattr(info, kind)[name] = pq_info
    if "tables" in root:
        for name, node in root["tables"].items():
            table = _guarded(errors, "tables", name, lambda node=node: anndata_info(node))  # type: ignore[misc]
            if table is None:
                continue
            info.tables[name] = table
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
