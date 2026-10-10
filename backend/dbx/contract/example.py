"""A small, complete reference store built from synthetic data.

It shows every element of the contract, is used by validator tests and lets frontend work start
before the real converter exists. The converter (S5/S6) must produce the same layout.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import zarr

import dbx
from dbx.contract.models import (
    INDPTR_CHUNK,
    X_CHUNK,
    CategoricalInfo,
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
from dbx.contract.validate import MANIFEST
from dbx.contract.write import (
    ANNDATA_ARRAY,
    anndata_dict_group,
    array,
    csc_x,
    morton_order,
    ome_multiscale_attrs,
    write_pyramid,
)
from dbx.synth.anndata_gen import consolidate
from dbx.synth.matrix import SynthMatrix

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
ROTATE_90 = [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]


def write_example_store(
    out: Path,
    n_cells: int = 300,
    n_genes: int = 40,
    size: tuple[int, int] = (1100, 900),
    n_points: int = 5000,
) -> Path:
    """Write a complete store to `out` (replaced if present)."""
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    rng = np.random.default_rng(0)
    h, w = size
    root = zarr.open_group(out, mode="w")
    root.attrs["dbx"] = {"contract_version": "0.1.0"}

    # ---- table: one cell per grid square
    side = int(np.ceil(np.sqrt(n_cells)))
    cy, cx = divmod(np.arange(n_cells), side)
    cell_h, cell_w = h / side, w / side
    centers = np.stack([(cx + 0.5) * cell_w, (cy + 0.5) * cell_h], axis=1).astype(np.float32)
    k = 6
    cluster = pd.Categorical.from_codes(
        rng.integers(0, k, n_cells), categories=[f"type_{i}" for i in range(k)]
    )
    obs = pd.DataFrame(
        {
            "cluster": cluster,
            "area": rng.random(n_cells).astype(np.float32),
            "region": pd.Categorical(["cells"] * n_cells),
            "cell_id": np.arange(n_cells),
        },
        index=pd.Index([f"cell{i}" for i in range(n_cells)]),
    )
    var = pd.DataFrame(index=pd.Index([f"Gene{i}" for i in range(n_genes)]))
    table = ad.AnnData(obs=obs, var=var)
    table.obsm["X_umap"] = rng.normal(size=(n_cells, 2)).astype(np.float32)
    table.obsm["X_pca"] = rng.normal(size=(n_cells, 10)).astype(np.float32)
    table.obsm["spatial"] = centers
    table.uns["cluster_colors"] = [
        "#1b9e77",
        "#d95f02",
        "#7570b3",
        "#e7298a",
        "#66a61e",
        "#e6ab02",
    ][:k]
    tables = root.require_group("tables")
    table.write_zarr(out / "tables" / "cells")
    tg = zarr.open_group(out / "tables" / "cells", mode="a", use_consolidated=False)
    x = SynthMatrix(n_cells, n_genes, 0.3, seed=1).cols(0, n_genes).tocsc()
    csc_x(tg, x.data, x.indices, x.indptr, (n_cells, n_genes), X_CHUNK, INDPTR_CHUNK)
    dbxg = anndata_dict_group(tg["uns"], "dbx")  # type: ignore[arg-type]
    stats = anndata_dict_group(dbxg, "gene_stats")
    dense = x.toarray()
    nz = np.where(dense > 0, dense, np.nan)
    with np.errstate(all="ignore"):
        p99 = np.nan_to_num(np.nanpercentile(nz, 99, axis=0))
    for name, v in {
        "nnz": (dense > 0).sum(0),
        "mean": dense.mean(0),
        "max": dense.max(0),
        "p99": p99,
    }.items():
        array(stats, name, np.asarray(v, dtype=np.float32), attrs=ANNDATA_ARRAY)
    del tables

    # ---- image: 3 channels, smooth gradients + cell blobs
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.stack(
        [
            (xx / w * 255),
            (yy / h * 255),
            255
            * np.exp(
                -(((xx % cell_w) - cell_w / 2) ** 2 + ((yy % cell_h) - cell_h / 2) ** 2)
                / (2 * (cell_w / 5) ** 2)
            ),
        ]
    ).astype(np.uint8)
    ig = root.require_group("images").require_group("img")
    levels = write_pyramid(ig, img, nearest=False)
    colors = ["FF0000", "00FF00", "0000FF"]
    channels = [
        {
            "label": f"ch{i}",
            "color": colors[i],
            "active": True,
            "window": {"start": 0.0, "end": 255.0, "min": 0.0, "max": 255.0},
        }
        for i in range(3)
    ]
    ig.attrs.update(ome_multiscale_attrs("img", ["c", "y", "x"], len(levels), channels))

    # ---- labels: each grid square is its cell's label (value = row + 1)
    lab = (yy // cell_h).astype(np.int64) * side + (xx // cell_w).astype(np.int64) + 1
    lab[lab > n_cells] = 0
    lg = root.require_group("labels").require_group("cells")
    lab_levels = write_pyramid(lg, lab.astype(np.uint32), nearest=True)
    lg.attrs.update(ome_multiscale_attrs("cells", ["y", "x"], len(lab_levels)))
    lookup = np.full(n_cells + 1, -1, dtype=np.int32)
    lookup[1:] = np.arange(n_cells, dtype=np.int32)
    ag = root.require_group("aux").require_group("labels").require_group("cells")
    array(ag, "row", lookup)

    # ---- shapes: one square polygon per cell
    half = np.array([cell_w, cell_h], dtype=np.float32) * 0.4
    corners = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], dtype=np.float32)
    verts = (centers[:, None, :] + corners[None, :, :] * half).reshape(-1, 2)
    sg = root.require_group("shapes").require_group("cells")
    array(sg, "vertices", verts.astype(np.float32))
    array(sg, "starts", np.arange(0, 4 * n_cells + 1, 4, dtype=np.int64))
    array(sg, "row", np.arange(n_cells, dtype=np.int32))
    bbox = np.concatenate([centers - half, centers + half], axis=1).astype(np.float32)
    array(sg, "bbox", bbox)

    # ---- points: transcripts near cell centres, 10 genes
    n_feat = 10
    owner = rng.integers(0, n_cells, n_points)
    px = centers[owner, 0] + rng.normal(0, cell_w / 6, n_points)
    py = centers[owner, 1] + rng.normal(0, cell_h / 6, n_points)
    feat = rng.integers(0, n_feat, n_points)
    pbox = (0.0, 0.0, float(w), float(h))
    order = np.lexsort((morton_order(px, py, pbox), feat))
    pg = root.require_group("points").require_group("transcripts")
    array(pg, "xy", np.stack([px, py], 1)[order].astype(np.float32))
    array(pg, "feature", feat[order].astype(np.int32))
    array(pg, "cell", owner[order].astype(np.int32))
    pz = rng.integers(0, 7, n_points).astype(np.float32)  # 7 z-planes, as in MERSCOPE
    array(pg, "z", pz[order])
    pg.attrs["dbx"] = {"features": [f"Gene{i}" for i in range(n_feat)]}
    counts = np.bincount(feat, minlength=n_feat)
    array(pg, "offsets", np.concatenate([[0], np.cumsum(counts)]).astype(np.int64))
    array(
        pg,
        "overview",
        np.sort(rng.choice(n_points, min(n_points, 1000), replace=False)).astype(np.int32),
    )

    systems = {"global": IDENTITY, "rot": ROTATE_90}
    manifest = Manifest(
        dataset_id="example0000000000",
        source=SourceInfo(
            path="<synthetic>",
            kind="spatialdata_zarr",
            size_bytes=0,
            mtime=0.0,
            fingerprint_version=1,
        ),
        converter=ConverterInfo(
            dbx_version=dbx.__version__,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            duration_s=0.0,
        ),
        coordinate_systems=["global", "rot"],
        tables={
            "cells": TableEntry(
                path="tables/cells",
                n_obs=n_cells,
                n_vars=n_genes,
                nnz=int(x.nnz),
                embeddings={"X_umap": 2, "X_pca": 10},
                spatial_keys=["spatial"],
                categorical={
                    "cluster": CategoricalInfo(n_categories=k, has_colors=True),
                    "region": CategoricalInfo(n_categories=1),
                },
                numeric=["area"],
                region=["cells"],
                region_key="region",
                instance_key="cell_id",
            )
        },
        images={
            "img": ImageEntry(
                path="images/img",
                axes=["c", "y", "x"],
                dtype="uint8",
                levels=levels,
                channels=[
                    ChannelInfo(name=f"ch{i}", window=(0.0, 255.0), color=f"#{colors[i]}")
                    for i in range(3)
                ],
                rgb=True,
                transforms=systems,
            )
        },
        labels={
            "cells": LabelsEntry(
                path="labels/cells",
                levels=lab_levels,
                max_label=n_cells,
                table="cells",
                lookup="aux/labels/cells/row",
                transforms=systems,
            )
        },
        shapes={
            "cells": ShapesEntry(
                path="shapes/cells", kind="polygons", n=n_cells, table="cells", transforms=systems
            )
        },
        points={
            "transcripts": PointsEntry(
                path="points/transcripts",
                n=n_points,
                n_features=n_feat,
                feature_key="gene",
                has_cell=True,
                has_z=True,
                z_range=(float(pz.min()), float(pz.max())),
                z_planes=sorted(float(v) for v in np.unique(pz)),
                bbox=pbox,
                table="cells",
                transforms={"global": IDENTITY},
            )
        },
    )
    consolidate(out / "tables" / "cells")
    consolidate(out)
    (out / MANIFEST).write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
    return out
