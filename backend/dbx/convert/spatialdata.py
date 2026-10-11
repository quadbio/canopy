"""Convert a SpatialData zarr store into a dbx store (contract/SPEC.md §2–§8).

Tables go through the AnnData path (`write_table`). Images are written level by level from the
lazy (dask) source, so an image larger than memory is converted in tiles. Labels, shapes and
points are read whole: they are small next to the host's memory for the data seen so far
(5.6 M transcripts ≈ 0.3 GB); streaming them is noted in ADR 0011 for very large panels.
Every element gets one 3×3 affine per coordinate system, composed by `spatialdata`.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import dask.array as da
import numpy as np
import pandas as pd
import zarr
from anndata.io import read_elem

import dbx
from dbx.contract import MANIFEST, validate_store
from dbx.contract.models import (
    CONTRACT_VERSION,
    LOD_CAP,
    OVERVIEW_POINTS,
    POLY_BLOCK,
    TILE,
    ChannelInfo,
    ConverterInfo,
    ImageEntry,
    LabelsEntry,
    Manifest,
    PointsEntry,
    ShapesEntry,
    SourceInfo,
    TableEntry,
)
from dbx.contract.write import (
    array,
    codecs,
    consolidate,
    downsample_mean,
    image_label_attrs,
    labels_container_attrs,
    needs_relabel,
    ome_multiscale_attrs,
    points_layout,
    polygon_layout,
    pyramid_levels,
    relabel,
    sharded,
    write_points,
    write_polygons,
    write_pyramid,
)
from dbx.convert.anndata import (
    ConvertOptions,
    Progress,
    Result,
    StageProgress,
    _publish,
    store_dir,
    table_memory_estimate,
    write_table,
)
from dbx.convert.matrix import ConversionError
from dbx.inspect.core import dataset_id, version_stamp
from dbx.inspect.models import Fingerprint
from dbx.logs import get_logger, timed

# Overall progress: share of the work each stage takes (roughly).
STAGES = {
    "tables": 0.3,
    "images": 0.3,
    "labels": 0.1,
    "shapes": 0.1,
    "points": 0.15,
    "finish": 0.05,
}
CHANNEL_COLOURS = ["FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF", "FFFFFF"]
_FEATURE_NAME = re.compile(r"gene|feature|target|probe|transcript", re.I)

# Bytes of memory per element read whole (for the job's memory guard).
POINT_BYTES = 48  # x, y, (z), codes, cell, sort keys
LABEL_BYTES = 16  # source int64 + uint32 copy + level copies


def safe_name(name: str) -> str:
    """Element names in the store: `[A-Za-z0-9._-]`, others replaced by `_` (SPEC §2)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "_"


def spatial_memory_estimate(fp: Fingerprint) -> int:
    """Memory read whole besides the tables' matrices: obs/var, points, labels, shapes."""
    sd = fp.spatialdata
    if sd is None:
        return 0
    total = sum(table_memory_estimate(t) for t in sd.tables.values())
    total += sum(p.n_rows * POINT_BYTES for p in sd.points.values())
    total += sum(s.n_rows * 400 for s in sd.shapes.values())  # ~40 vertices per polygon
    for lab in sd.labels.values():
        if lab.levels:
            total += int(np.prod(lab.levels[0].shape)) * LABEL_BYTES
    return total


def _affines(element: Any) -> dict[str, list[list[float]]]:
    """Each coordinate system → 3×3 affine from the element's intrinsic x, y."""
    from spatialdata.transformations import get_transformation

    out: dict[str, list[list[float]]] = {}
    for cs, t in get_transformation(element, get_all=True).items():
        m = np.asarray(t.to_affine_matrix(input_axes=("x", "y"), output_axes=("x", "y")))
        out[str(cs)] = [[float(v) for v in row] for row in m]
    return out


def _level0(element: Any) -> Any:
    """The full-resolution DataArray of an image or labels element (single or multiscale)."""
    if hasattr(element, "children") and element.children:
        node = element["scale0"]
        ds = node.ds if hasattr(node, "ds") else node
        return next(iter(ds.data_vars.values()))
    return element


def _region(attrs: dict[str, Any]) -> dict[str, Any]:
    region = attrs.get("region")
    return {
        "region": [region] if isinstance(region, str) else list(region or []),
        "region_key": attrs.get("region_key"),
        "instance_key": attrs.get("instance_key"),
    }


def _instance_rows(
    obs: pd.DataFrame, instance_key: str, region: dict[str, Any], element: str
) -> dict[Any, int]:
    """Table rows of the instances of `element`: instance id → row."""
    rows = np.arange(len(obs))
    key = region.get("region_key")
    if element and key and key in obs and len(region.get("region", [])) > 1:
        rows = rows[(obs[key].astype(str) == element).to_numpy()]
    ids = obs[instance_key].to_numpy()[rows]
    return {_plain(v): int(r) for v, r in zip(ids, rows, strict=True)}


def _plain(v: Any) -> Any:
    """Instance ids as plain Python values, so 7, np.int64(7) and "7"-free ints compare."""
    return v.item() if isinstance(v, np.generic) else v


# ---------------------------------------------------------------- images and labels
def write_pyramid_lazy(group: zarr.Group, level0: Any, nearest: bool) -> list[list[int]]:
    """A pyramid written tile by tile from a dask array (c, y, x) or (y, x): level 0 as it is,
    each next level from the one just written (2×2 mean, or every second pixel)."""
    lead = (1,) * (level0.ndim - 2)
    shapes: list[list[int]] = []
    current = level0
    prev: zarr.Array[Any] | None = None
    for i, (h, w) in enumerate(pyramid_levels((level0.shape[-2], level0.shape[-1]))):
        if prev is not None:
            src = da.from_zarr(prev).rechunk((*lead, 2 * TILE, 2 * TILE))  # type: ignore[no-untyped-call]
            if nearest:
                current = src[..., 0::2, 0::2]
            else:
                halved = tuple(tuple((c + 1) // 2 for c in cs) for cs in src.chunks[-2:])
                current = src.map_blocks(
                    downsample_mean, chunks=(*src.chunks[:-2], *halved), dtype=src.dtype
                )
        chunks = (*lead, min(TILE, h), min(TILE, w))
        a = group.create_array(
            str(i),
            shape=(*level0.shape[:-2], h, w),
            dtype=level0.dtype,
            chunks=chunks,
            compressors=codecs(),
            overwrite=True,
            config={"write_empty_chunks": True},  # no 404s for empty tiles in the browser
        )
        da.store(current.rechunk(chunks), a, lock=False)  # type: ignore[arg-type]
        shapes.append([*level0.shape[:-2], h, w])
        prev = a
    return shapes


def _window(a: np.ndarray) -> tuple[float, float]:
    """0.5th–99.5th percentile of a channel (from the smallest level)."""
    v = a[np.isfinite(a)] if a.dtype.kind == "f" else a.ravel()
    if not v.size:
        return (0.0, 1.0)
    lo, hi = np.percentile(v, [0.5, 99.5])
    return (float(lo), float(hi) if hi > lo else float(lo) + 1.0)


def _write_image(
    store: zarr.Group, name: str, element: Any, warnings: list[str]
) -> ImageEntry | None:
    arr = _level0(element)
    dims = [str(d) for d in arr.dims]
    if dims != ["c", "y", "x"]:
        warnings.append("image_not_cyx_skipped")
        return None
    data = arr.data if isinstance(arr.data, da.Array) else da.from_array(np.asarray(arr.data))  # type: ignore[no-untyped-call]
    g = store.require_group("images").require_group(name)
    levels = write_pyramid_lazy(g, data, nearest=False)
    last: Any = g[str(len(levels) - 1)]
    smallest = np.asarray(last[...])
    names = [str(c) for c in arr.coords["c"].values] if "c" in arr.coords else []
    n_c = int(arr.shape[0])
    names = names if len(names) == n_c else [f"ch{i}" for i in range(n_c)]
    dt = np.dtype(arr.dtype)
    rgb = dt == np.uint8 and n_c in (3, 4)
    full = (float(np.iinfo(dt).min), float(np.iinfo(dt).max)) if dt.kind in "iu" else None
    channels: list[ChannelInfo] = []
    omero: list[dict[str, Any]] = []
    for i in range(n_c):
        # from the data also for RGB-like images: 3 uint8 channels are often a dim stain
        win = _window(smallest[i])
        lo, hi = full or (float(np.nanmin(smallest[i])), float(np.nanmax(smallest[i])))
        colour = CHANNEL_COLOURS[i % len(CHANNEL_COLOURS)]
        channels.append(ChannelInfo(name=names[i], window=win, color=f"#{colour.lower()}"))
        omero.append(
            {
                "label": names[i],
                "color": colour,
                "active": i < 3,
                "window": {"start": win[0], "end": win[1], "min": lo, "max": hi},
            }
        )
    g.attrs.update(ome_multiscale_attrs(name, ["c", "y", "x"], len(levels), omero))
    return ImageEntry(
        path=f"images/{name}",
        axes=["c", "y", "x"],
        dtype=str(dt),
        levels=levels,
        channels=channels,
        rgb=bool(rgb),
        transforms=_affines(element),
    )


def _write_labels(
    store: zarr.Group,
    name: str,
    element: Any,
    table: tuple[str, pd.DataFrame, dict[str, Any]] | None,
    source_image: str | None,
    warnings: list[str],
) -> LabelsEntry | None:
    arr = _level0(element)
    if [str(d) for d in arr.dims] != ["y", "x"]:
        warnings.append("labels_not_yx_skipped")
        return None
    raw = np.asarray(arr.data).astype(np.int64, copy=False)
    if (raw < 0).any():
        warnings.append("labels_negative_as_background")
        raw = np.where(raw < 0, 0, raw)
    ids = np.unique(raw)
    ids = ids[ids != 0]
    n_labels, max_label = int(ids.size), int(ids.max()) if ids.size else 0
    relabelled = needs_relabel(max_label, n_labels)
    original: np.ndarray | None = None
    if relabelled:
        lab, original = relabel(raw)
        max_label = n_labels
    else:
        lab = raw.astype(np.uint32)
    del raw
    g = store.require_group("labels").require_group(name)
    levels = write_pyramid(g, lab, nearest=True)
    attrs = ome_multiscale_attrs(name, ["y", "x"], len(levels))
    attrs["ome"].update(image_label_attrs(source_image))
    g.attrs.update(attrs)
    aux = store.require_group("aux").require_group("labels").require_group(name)
    if original is not None:
        array(aux, "original_id", original)
    table_name = lookup = None
    if table is not None:
        tname, obs, region = table
        rows = _instance_rows(obs, region["instance_key"], region, name)
        lk = np.full(max_label + 1, -1, dtype=np.int32)
        for label_id, row in rows.items():
            try:
                v = int(label_id)
            except (TypeError, ValueError):
                continue
            if original is not None:
                k = int(np.searchsorted(original, np.uint64(v)))
                if k < len(original) and int(original[k]) == v:
                    lk[k] = row
            elif 0 < v <= max_label:
                lk[v] = row
        array(aux, "row", lk)
        table_name, lookup = tname, f"aux/labels/{name}/row"
    return LabelsEntry(
        path=f"labels/{name}",
        levels=levels,
        max_label=max_label,
        n_labels=n_labels,
        relabelled=relabelled,
        table=table_name,
        lookup=lookup,
        transforms=_affines(element),
    )


# ---------------------------------------------------------------- shapes
def _write_shapes(
    store: zarr.Group,
    name: str,
    gdf: Any,
    table: tuple[str, pd.DataFrame, dict[str, Any]] | None,
    warnings: list[str],
) -> ShapesEntry | None:
    rows_of: dict[Any, int] | None = None
    if table is not None:
        tname, obs, region = table
        rows_of = _instance_rows(obs, region["instance_key"], region, name)
    index = [_plain(v) for v in gdf.index]
    if rows_of is not None:
        rows = np.array([rows_of.get(v, -1) for v in index], dtype=np.int64)
        if (rows < 0).any():
            warnings.append("shapes_without_table_row")
    else:
        rows = np.arange(len(gdf), dtype=np.int64)
    types = set(gdf.geometry.geom_type.unique())
    g = store.require_group("shapes").require_group(name)
    transforms = _affines(gdf)
    if types <= {"Point"}:
        if "radius" not in gdf.columns:
            warnings.append("points_shapes_without_radius_skipped")
            return None
        center = np.c_[gdf.geometry.x.to_numpy(), gdf.geometry.y.to_numpy()].astype(np.float32)
        sharded(g, "center", center, POLY_BLOCK)
        sharded(g, "radius", gdf["radius"].to_numpy(np.float32), POLY_BLOCK)
        sharded(g, "row", rows.astype(np.int32), POLY_BLOCK)
        return ShapesEntry(
            path=f"shapes/{name}",
            kind="circles",
            n=len(gdf),
            table=table[0] if table else None,
            transforms=transforms,
        )
    if not types <= {"Polygon", "MultiPolygon"}:
        warnings.append("shapes_geometry_skipped")
        return None
    rings: list[np.ndarray] = []
    ring_rows: list[int] = []
    holes = False
    for geom, row in zip(gdf.geometry, rows, strict=True):
        if geom is None or geom.is_empty:
            continue
        parts = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for part in parts:
            holes = holes or len(part.interiors) > 0
            rings.append(np.asarray(part.exterior.coords, dtype=np.float32)[:, :2])
            ring_rows.append(int(row))
    if holes:
        warnings.append("polygon_holes_dropped")
    lay = polygon_layout(rings, np.asarray(ring_rows, dtype=np.int64))
    write_polygons(g, lay)
    return ShapesEntry(
        path=f"shapes/{name}",
        kind="polygons",
        n=len(rings),
        n_vertices=int(lay.vertices.shape[0]),
        table=table[0] if table else None,
        transforms=transforms,
    )


# ---------------------------------------------------------------- points
def feature_column(df: pd.DataFrame, declared: str | None, exclude: set[str]) -> tuple[str, bool]:
    """The column holding gene names: the declared `feature_key`, else the one categorical
    column (instance ids excluded), else the categorical one named like genes."""
    if declared and declared in df.columns:
        return declared, False
    cats = [
        str(c)
        for c in df.columns
        if isinstance(df[c].dtype, pd.CategoricalDtype) and str(c) not in exclude
    ]
    if len(cats) == 1:
        return cats[0], True
    named = [c for c in cats if _FEATURE_NAME.search(c)]
    if len(named) == 1:
        return named[0], True
    raise ConversionError(
        "feature_key_ambiguous",
        f"cannot tell which column holds the genes ({len(cats)} candidates)",
    )


def _write_points(
    store: zarr.Group,
    name: str,
    ddf: Any,
    tables: dict[str, tuple[pd.DataFrame, dict[str, Any]]],
    warnings: list[str],
) -> PointsEntry:
    attrs = dict(ddf.attrs.get("spatialdata_attrs") or {})
    transforms = _affines(ddf)
    df: pd.DataFrame = ddf.compute()
    # the table whose instance key the points carry (e.g. MERSCOPE cell_id)
    link: tuple[str, str] | None = None
    for tname, (obs, region) in tables.items():
        key = attrs.get("instance_key") or region.get("instance_key")
        if key and key in df.columns and key in obs.columns:
            link = (tname, key)
            break
    exclude = {link[1]} if link else set()
    if attrs.get("instance_key"):
        exclude.add(str(attrs["instance_key"]))
    fkey, inferred = feature_column(df, attrs.get("feature_key"), exclude)
    feat = df[fkey]
    if not isinstance(feat.dtype, pd.CategoricalDtype):
        feat = feat.astype("category")
    features = [str(c) for c in feat.cat.categories]
    codes = feat.cat.codes.to_numpy()
    keep = codes >= 0
    if not keep.all():
        warnings.append("points_without_feature_dropped")
    xy = np.c_[df["x"].to_numpy(), df["y"].to_numpy()].astype(np.float32)[keep]
    z = df["z"].to_numpy(np.float32)[keep] if "z" in df.columns else None
    cell = None
    if link is not None:
        obs, region = tables[link[0]]
        rows_of = _instance_rows(obs, link[1], region, "")
        ids = df[link[1]]
        if isinstance(ids.dtype, pd.CategoricalDtype):
            cat_rows = np.array([rows_of.get(_plain(v), -1) for v in ids.cat.categories], np.int32)
            c = ids.cat.codes.to_numpy()
            cell = np.where(c >= 0, cat_rows[np.maximum(c, 0)], -1).astype(np.int32)[keep]
        else:
            cell = np.array([rows_of.get(_plain(v), -1) for v in ids.to_numpy()], np.int32)[keep]
    del df
    lay = points_layout(xy, codes[keep], len(features), z=z, cell=cell)
    write_points(store.require_group("points").require_group(name), lay, features)
    z_planes = None
    if z is not None:
        u = np.unique(z)
        z_planes = [float(v) for v in u] if len(u) <= 256 else None
    return PointsEntry(
        path=f"points/{name}",
        n=int(xy.shape[0]),
        n_features=len(features),
        feature_key=fkey,
        feature_key_inferred=inferred,
        has_cell=cell is not None,
        has_z=z is not None,
        z_range=(float(z.min()), float(z.max())) if z is not None and z.size else None,
        z_planes=z_planes,
        bbox=lay.bbox,
        n_overview=min(int(xy.shape[0]), OVERVIEW_POINTS),
        lod_cap=LOD_CAP,
        table=link[0] if link else None,
        transforms=transforms,
    )


# ---------------------------------------------------------------- the conversion
def convert_spatialdata(
    src: Path,
    fp: Fingerprint,
    cache_dir: Path,
    opts: ConvertOptions,
    progress: Progress,
) -> Result:
    import spatialdata

    sdi = fp.spatialdata
    if sdi is None or fp.kind != "spatialdata_zarr":
        raise ConversionError("not_spatialdata", "only SpatialData stores are converted here")
    log = get_logger("dbx.convert")
    t0 = time.monotonic()
    version = fp.dataset_id
    stamp = version_stamp(src)
    if dataset_id(src, stamp) != version:
        raise ConversionError("source_changed", "the store changed since it was inspected")
    final = store_dir(cache_dir, version)
    tmp = final.parent / f"{final.name}.tmp-j{opts.job}-{uuid.uuid4().hex[:8]}"
    work = (opts.tmp_dir or cache_dir / "tmp") / tmp.name
    prog = StageProgress(progress, STAGES)
    warnings: list[str] = []
    try:
        tmp.mkdir(parents=True)
        store = zarr.open_group(tmp, mode="w")
        store.attrs["dbx"] = {"contract_version": CONTRACT_VERSION}
        store.require_group("tables")
        with timed("convert/read_spatialdata", version=version):
            sdata = spatialdata.read_zarr(src)
        src_root: Any = zarr.open_group(src, mode="r")

        # ---- tables (AnnData path), with what they annotate
        tables: dict[str, TableEntry] = {}
        table_obs: dict[str, tuple[pd.DataFrame, dict[str, Any]]] = {}
        names = list(sdi.tables)
        for i, tname in enumerate(names):
            troot: Any = src_root["tables"][tname]
            attrs: Any = (
                read_elem(troot["uns"]["spatialdata_attrs"])
                if "spatialdata_attrs" in troot["uns"]
                else {}
            )
            region = _region(dict(attrs))
            safe = safe_name(tname)
            inner = StageProgress(
                lambda _s, f, i=i: prog("tables", (i + f) / max(len(names), 1))  # type: ignore[misc]
            )
            topts = opts
            tinfo = sdi.tables[tname]
            if opts.x_source.startswith("layers/") and opts.x_source[7:] not in tinfo.layers:
                # the chosen layer is in another table: this one keeps its X
                topts = dataclasses.replace(opts, x_source="X")
                warnings.append("layer_missing_used_X")
            entry = write_table(
                troot, tinfo, tmp, safe, topts, work, inner, warnings, version, region
            )
            entry.region = [safe_name(r) for r in entry.region]
            if safe != tname:
                entry.source_name = tname
            tables[safe] = entry
            obs: pd.DataFrame = read_elem(troot["obs"])
            table_obs[safe] = (obs, region)

        def annotating(element: str) -> tuple[str, pd.DataFrame, dict[str, Any]] | None:
            for tname, (obs, region) in table_obs.items():
                if element in region["region"] and region.get("instance_key") in obs.columns:
                    return tname, obs, region
            return None

        # ---- images
        images: dict[str, ImageEntry] = {}
        for i, (name, el) in enumerate(sdata.images.items()):
            with timed("convert/image", version=version, element=name) as extra:
                entry_i = _write_image(store, safe_name(name), el, warnings)
                if entry_i is not None:
                    extra.update(levels=len(entry_i.levels), shape=entry_i.levels[0])
                    if safe_name(name) != name:
                        entry_i.source_name = name
                    images[safe_name(name)] = entry_i
            prog("images", (i + 1) / max(len(sdata.images), 1))

        # ---- labels
        labels: dict[str, LabelsEntry] = {}
        first_image = next(iter(images), None)
        for i, (name, el) in enumerate(sdata.labels.items()):
            with timed("convert/labels", version=version, element=name):
                entry_l = _write_labels(
                    store, safe_name(name), el, annotating(name), first_image, warnings
                )
                if entry_l is not None:
                    labels[safe_name(name)] = entry_l
            prog("labels", (i + 1) / max(len(sdata.labels), 1))
        if labels:
            store.require_group("labels").attrs.update(labels_container_attrs(list(labels)))

        # ---- shapes
        shapes: dict[str, ShapesEntry] = {}
        for i, (name, gdf) in enumerate(sdata.shapes.items()):
            with timed("convert/shapes", version=version, element=name) as extra:
                entry_s = _write_shapes(store, safe_name(name), gdf, annotating(name), warnings)
                if entry_s is not None:
                    extra.update(n=entry_s.n, kind=entry_s.kind)
                    shapes[safe_name(name)] = entry_s
            prog("shapes", (i + 1) / max(len(sdata.shapes), 1))

        # ---- points
        points: dict[str, PointsEntry] = {}
        for i, (name, ddf) in enumerate(sdata.points.items()):
            with timed("convert/points", version=version, element=name) as extra:
                entry_p = _write_points(store, safe_name(name), ddf, table_obs, warnings)
                extra.update(n=entry_p.n, n_features=entry_p.n_features)
                points[safe_name(name)] = entry_p
            prog("points", (i + 1) / max(len(sdata.points), 1))

        # ---- manifest, consolidation, validation, publish
        systems = sorted(
            {
                cs
                for kind in (images, labels, shapes, points)
                for e in kind.values()
                for cs in e.transforms
            }
        )
        systems.sort(key=lambda s: s != "global")
        consolidate(tmp)
        manifest = Manifest(
            dataset_id=fp.dataset_id,
            source=SourceInfo(
                path=src.name,
                kind="spatialdata_zarr",
                size_bytes=int(fp.size_bytes or 0),
                mtime=float(fp.mtime or 0.0),
                fingerprint_version=fp.fingerprint_version,
            ),
            converter=ConverterInfo(
                dbx_version=dbx.__version__,
                created=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_s=round(time.monotonic() - t0, 1),
                warnings=sorted(set(warnings)),
            ),
            coordinate_systems=systems,
            units={s: "unknown" for s in systems},
            tables=tables,
            images=images,
            labels=labels,
            shapes=shapes,
            points=points,
        )
        (tmp / MANIFEST).write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
        prog("finish", 0.3)
        with timed("convert/validate", version=version) as extra:
            report = validate_store(tmp, deep=True)
            extra.update(errors=len(report.errors), warnings=len(report.warnings))
        if not report.ok:
            log.error("store_invalid", version=version, details=report.errors[:20])
            raise ConversionError("store_invalid", "the converted store failed validation")
        if version_stamp(src) != stamp:
            raise ConversionError("source_changed", "the store changed during the conversion")
        prog("finish", 0.9)
        _publish(tmp, final)
        prog("finish", 1.0)
        return Result(final, manifest, warnings)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
