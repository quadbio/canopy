"""A small, complete reference store built from synthetic data.

It shows every element of the contract, is used by validator tests and lets frontend work start
before the real converter exists. The converter (S5/S6) must produce the same layout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import zarr

import dbx
from dbx.contract.models import (
    INDPTR_CHUNK,
    OBSM_CHUNK,
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
    image_label_attrs,
    labels_container_attrs,
    ome_multiscale_attrs,
    points_layout,
    polygon_layout,
    prepare_output_dir,
    write_points,
    write_polygons,
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
    n_points: int = 40_000,
    force: bool = False,
    overview: int = 3_000,
    lod_cap: int = 2_000,
) -> Path:
    """Write a complete store to `out`. An existing store there is replaced; any other
    non-empty folder is refused unless `force`."""
    out = Path(out)
    prepare_output_dir(out, force=force)
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
            "section": pd.Categorical(np.where(cx < side / 2, "left", "right")),
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
    obsm = tg["obsm"]
    assert isinstance(obsm, zarr.Group)
    for key in table.obsm:
        v: np.ndarray = np.asarray(table.obsm[key])
        array(obsm, key, v, chunks=(OBSM_CHUNK, v.shape[1]), attrs=ANNDATA_ARRAY)
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
    attrs = ome_multiscale_attrs("cells", ["y", "x"], len(lab_levels))
    attrs["ome"].update(image_label_attrs("img"))
    lg.attrs.update(attrs)
    root.require_group("labels").attrs.update(labels_container_attrs(["cells"]))
    lookup = np.full(n_cells + 1, -1, dtype=np.int32)
    lookup[1:] = np.arange(n_cells, dtype=np.int32)
    ag = root.require_group("aux").require_group("labels").require_group("cells")
    array(ag, "row", lookup)

    # ---- shapes: one square polygon per cell (written clockwise and open: the layout fixes it)
    half = np.array([cell_w, cell_h], dtype=np.float32) * 0.4
    corners = np.array([[-1, -1], [-1, 1], [1, 1], [1, -1]], dtype=np.float32)
    rings = [c + corners * half for c in centers]
    poly = polygon_layout(rings, np.arange(n_cells))
    write_polygons(root.require_group("shapes").require_group("cells"), poly)

    # ---- points: transcripts near cell centres, 10 genes with skewed expression
    n_feat = 10
    owner = rng.integers(0, n_cells, n_points)
    pxy = np.stack(
        [
            centers[owner, 0] + rng.normal(0, cell_w / 6, n_points),
            centers[owner, 1] + rng.normal(0, cell_h / 6, n_points),
        ],
        1,
    )
    weights = 0.5 ** np.arange(n_feat)
    feat = rng.choice(n_feat, n_points, p=weights / weights.sum())
    pz = rng.integers(0, 7, n_points).astype(np.float32)  # 7 z-planes, as in MERSCOPE
    pts = points_layout(pxy, feat, n_feat, z=pz, cell=owner, overview=overview, lod_cap=lod_cap)
    write_points(
        root.require_group("points").require_group("transcripts"),
        pts,
        [f"Gene{i}" for i in range(n_feat)],
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
        units={"global": "micrometer", "rot": "micrometer"},
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
                    "section": CategoricalInfo(n_categories=2),
                },
                numeric=["area"],
                region=["cells"],
                region_key="region",
                instance_key="cell_id",
                section_keys=["section"],
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
                n_labels=n_cells,
                table="cells",
                lookup="aux/labels/cells/row",
                transforms=systems,
            )
        },
        shapes={
            "cells": ShapesEntry(
                path="shapes/cells",
                kind="polygons",
                n=n_cells,
                n_vertices=int(poly.vertices.shape[0]),
                table="cells",
                transforms=systems,
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
                bbox=pts.bbox,
                n_overview=int(pts.overview["xy"].shape[0]),
                lod_cap=lod_cap,
                table="cells",
                transforms={"global": IDENTITY},
            )
        },
    )
    consolidate(out / "tables" / "cells")
    consolidate(out)
    (out / MANIFEST).write_text(json.dumps(manifest.model_dump(mode="json"), indent=2))
    return out
