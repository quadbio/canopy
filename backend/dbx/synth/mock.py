"""Rebuild a look-alike dataset from a fingerprint (structure only, random values).

Used to reproduce problems reported from the host without access to the real file: the mock
has the same kind, encodings, dtypes, chunking style, columns, keys, elements and coordinate
systems, at a size that fits the development machine.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from dbx.inspect.models import AnnDataInfo, Fingerprint, MatrixInfo, ParquetInfo
from dbx.synth.anndata_gen import write_anndata
from dbx.synth.spatial_gen import write_spatialdata
from dbx.synth.spec import (
    AnnDataSpec,
    ColumnSpec,
    Compression,
    ImageSpec,
    LabelsSpec,
    MatrixSpec,
    PointsSpec,
    ShapesSpec,
    SpatialDataSpec,
    TableSpec,
    TransformSpec,
)

_COMPRESSION: dict[str | None, Compression] = {"gzip": "gzip", "lzf": "lzf", None: "none"}


def _scaled(n: int, scale: float, minimum: int = 1) -> int:
    return max(minimum, min(n, math.ceil(n * scale)))


def _matrix_spec(m: MatrixInfo, n_obs: int, n_vars: int, seed: int) -> MatrixSpec:
    total = max(1, m.shape[0] * m.shape[1]) if len(m.shape) == 2 else 1
    density = (m.nnz / total) if m.nnz is not None else 0.1
    comp = m.storage.compression
    compression: Compression = _COMPRESSION.get(comp, "zstd" if comp else "none")
    chunk = m.storage.chunks[0] if m.storage.chunks else None
    if chunk is not None:
        bound = n_obs if m.encoding == "dense" else max(1, int(n_obs * n_vars * density))
        chunk = max(1, min(chunk, bound))
    return MatrixSpec(
        encoding=m.encoding,
        dtype=m.dtype,
        density=min(max(density, 1e-4), 1.0),
        chunk=chunk,
        compression=compression,
        seed=seed,
    )


def _columns(info: AnnDataInfo, frame: str) -> list[ColumnSpec]:
    cols = []
    for c in getattr(info, frame).columns:
        if c.kind == "other":
            continue
        cols.append(
            ColumnSpec(
                name=c.name,
                kind=c.kind,
                dtype=c.dtype if c.kind == "numeric" else "float32",
                n_categories=c.n_categories or 1,
                colors=c.has_colors,
            )
        )
    return cols


def anndata_spec(info: AnnDataInfo, scale: float, max_obs: int) -> AnnDataSpec:
    n_obs = min(_scaled(info.n_obs, scale), max_obs)
    n_vars = info.n_vars
    return AnnDataSpec(
        n_obs=n_obs,
        n_vars=n_vars,
        X=_matrix_spec(info.X, n_obs, n_vars, 0) if info.X else None,
        layers={
            k: _matrix_spec(v, n_obs, n_vars, i + 1) for i, (k, v) in enumerate(info.layers.items())
        },
        obs=_columns(info, "obs"),
        var=_columns(info, "var"),
        obsm={
            k: (v.shape[1] if len(v.shape) > 1 else 1)
            for k, v in info.obsm.items()
            if v.dtype != "dataframe"
        },
        extra_colors={
            k: n for k, n in info.colors.items() if k not in {c.name for c in info.obs.columns}
        },
        obs_index_name=info.obs.index_name,
        var_index_name=info.var.index_name,
    )


def _ts(transforms: list[Any]) -> list[TransformSpec]:
    out = [
        TransformSpec(
            output=t.output,
            type=t.type if t.type in ("identity", "affine", "scale", "translation") else "identity",
        )
        for t in transforms
    ]
    return out or [TransformSpec(output="global")]


def _points_columns(p: ParquetInfo) -> dict[str, str]:
    cols: dict[str, str] = {}
    for name, t in p.columns.items():
        if name in ("x", "y", "z"):
            continue
        if t.startswith("dictionary"):
            cols[name] = "category"
        elif t.startswith(("int", "uint")):
            cols[name] = "int"
        elif t in ("double", "float"):
            cols[name] = "float"
        else:
            cols[name] = "string"
    return cols


def spatialdata_spec(fp: Fingerprint, scale: float, max_side: int, max_obs: int) -> SpatialDataSpec:
    sd = fp.spatialdata
    assert sd is not None
    spec = SpatialDataSpec()
    for name, img in sd.images.items():
        c, h, w = img.levels[0].shape[-3:]
        f = min(1.0, max_side / max(h, w))
        hh, ww = max(8, int(h * f)), max(8, int(w * f))
        chunk = img.levels[0].chunks[-1] if img.levels[0].chunks else None
        spec.images[name] = ImageSpec(
            channels=img.channel_names or [str(i) for i in range(c)],
            height=hh,
            width=ww,
            dtype=img.levels[0].dtype,
            levels=len(img.levels),
            chunk=None if chunk is None or chunk >= w else max(8, int(chunk * f)),
            transforms=_ts(img.transforms),
        )
    for name, lab in sd.labels.items():
        h, w = lab.levels[0].shape[-2:]
        f = min(1.0, max_side / max(h, w))
        spec.labels[name] = LabelsSpec(
            height=max(8, int(h * f)),
            width=max(8, int(w * f)),
            dtype=lab.levels[0].dtype,
            levels=len(lab.levels),
            transforms=_ts(lab.transforms),
        )
    for name, p in sd.points.items():
        spec.points[name] = PointsSpec(
            n_rows=_scaled(p.n_rows, scale, 10),
            columns=_points_columns(p),
            feature_key=p.feature_key,
            instance_key=p.instance_key,
            transforms=_ts(p.transforms),
        )
    for name, s in sd.shapes.items():
        spec.shapes[name] = ShapesSpec(
            n_polygons=min(_scaled(s.n_rows, scale, 10), max_obs),
            index_name=next(iter(s.columns.keys() - {"geometry"}), None),
            transforms=_ts(s.transforms),
        )
    for name, t in sd.tables.items():
        ref = sd.table_refs[name]
        region = ref.region[0] if ref.region else ""
        tspec = anndata_spec(t, scale, max_obs)
        # Region/instance columns are recreated by the table writer.
        tspec.obs = [c for c in tspec.obs if c.name not in (ref.region_key, ref.instance_key)]
        if region in spec.shapes:
            tspec.n_obs = spec.shapes[region].n_polygons
        spec.tables[name] = TableSpec(
            table=tspec,
            region=region,
            region_key=ref.region_key or "region",
            instance_key=ref.instance_key or "instance_id",
        )
    return spec


def write_mock(
    fp: Fingerprint, out: Path, scale: float = 0.1, max_side: int = 1024, max_obs: int = 200_000
) -> Path:
    if fp.kind == "spatialdata_zarr":
        return write_spatialdata(spatialdata_spec(fp, scale, max_side, max_obs), out)
    assert fp.anndata is not None
    fmt = "h5ad" if fp.kind == "h5ad" else "zarr"
    return write_anndata(anndata_spec(fp.anndata, scale, max_obs), out, fmt=fmt)  # type: ignore[arg-type]


def structure_signature(fp: Fingerprint) -> dict[str, Any]:
    """The parts of a fingerprint a mock must reproduce (no sizes, no counts)."""

    def ad_sig(a: AnnDataInfo) -> dict[str, Any]:
        return {
            "X": (a.X.encoding, a.X.dtype) if a.X else None,
            "layers": {k: (v.encoding, v.dtype) for k, v in a.layers.items()},
            "obs": sorted((c.name, c.kind) for c in a.obs.columns),
            "var": sorted((c.name, c.kind) for c in a.var.columns),
            "obsm": sorted(k for k, v in a.obsm.items() if v.dtype != "dataframe"),
            "colors": sorted(a.colors),
            "n_vars": a.n_vars,
        }

    sig: dict[str, Any] = {"kind": fp.kind}
    if fp.anndata:
        sig["anndata"] = ad_sig(fp.anndata)
    if fp.spatialdata:
        sd = fp.spatialdata
        sig["images"] = {
            k: (
                v.axes,
                v.levels[0].dtype,
                len(v.levels),
                v.channel_names,
                sorted((t.output, t.type) for t in v.transforms),
            )
            for k, v in sd.images.items()
        }
        sig["labels"] = {
            k: (
                v.axes,
                v.levels[0].dtype,
                len(v.levels),
                sorted((t.output, t.type) for t in v.transforms),
            )
            for k, v in sd.labels.items()
        }
        sig["points"] = {
            k: (
                sorted(_points_columns(v).items()),
                v.feature_key,
                v.instance_key,
                sorted((t.output, t.type) for t in v.transforms),
            )
            for k, v in sd.points.items()
        }
        sig["shapes"] = {
            k: (v.geometry_types, sorted((t.output, t.type) for t in v.transforms))
            for k, v in sd.shapes.items()
        }
        sig["tables"] = {k: ad_sig(v) for k, v in sd.tables.items()}
        sig["table_refs"] = {k: v.model_dump() for k, v in sd.table_refs.items()}
        sig["coordinate_systems"] = sd.coordinate_systems
    return sig
