"""A small, internally consistent example dataset for demos and browser tests.

Unlike the generic generator (random values per matrix), the matrices here belong together, as in
a scanpy-processed file: `layers["counts"]` holds integer counts and `X` holds
log1p(counts / total × 10 000). Cell types have marker genes, so colouring by a gene shows a
population. It also has a column with more categories than the default palette (`sample`, no
colours saved) and a boolean column.
"""

from __future__ import annotations

from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import scipy.sparse as sp

CELL_TYPES = [
    "T cell",
    "B cell",
    "NK cell",
    "Monocyte",
    "Macrophage",
    "Dendritic cell",
    "Neutrophil",
    "Erythroblast",
    "Endothelial",
    "Fibroblast",
    "Epithelial",
    "Hepatocyte",
]
MARKERS_PER_TYPE = 8
N_SAMPLES = 80  # more than the 54 colours of the default palette


def write_demo(out: Path, n_obs: int = 8_000, n_vars: int = 600, seed: int = 0) -> Path:
    """Write the example to `out` (.h5ad); returns the path."""
    rng = np.random.default_rng(seed)
    k = len(CELL_TYPES)
    if n_vars < k * MARKERS_PER_TYPE:
        raise ValueError(f"n_vars must be at least {k * MARKERS_PER_TYPE}")
    cell_type = rng.integers(0, k, n_obs)

    # Means per gene: a low background, raised for each type's markers.
    base = rng.lognormal(mean=-1.5, sigma=1.0, size=n_vars)
    means = np.tile(base, (k, 1))
    for t in range(k):
        markers = slice(t * MARKERS_PER_TYPE, (t + 1) * MARKERS_PER_TYPE)
        means[t, markers] = rng.uniform(3, 12, MARKERS_PER_TYPE)
    size = rng.lognormal(0, 0.4, n_obs)  # library size per cell
    counts = rng.poisson(means[cell_type] * size[:, None]).astype(np.int32)
    counts_csr = sp.csr_matrix(counts)
    total = counts.sum(axis=1, keepdims=True).clip(min=1)
    x = sp.csr_matrix(np.log1p(counts / total * 1e4).astype(np.float32))

    # Clusters on a ring in the embedding, so cell types are easy to see.
    angle = 2 * np.pi * cell_type / k
    umap = np.c_[np.cos(angle), np.sin(angle)] * 10 + rng.normal(0, 1.2, (n_obs, 2))

    # Marker genes are named after their cell type ("Monocyte_m3"), the others "GENE0001".
    gene_names = [
        f"{CELL_TYPES[i // MARKERS_PER_TYPE].replace(' ', '')}_m{i % MARKERS_PER_TYPE + 1}"
        for i in range(k * MARKERS_PER_TYPE)
    ]
    gene_names += [f"GENE{i:04d}" for i in range(n_vars - len(gene_names))]
    obs = pd.DataFrame(
        {
            "cell_type": pd.Categorical.from_codes(cell_type, categories=CELL_TYPES),
            "sample": pd.Categorical.from_codes(
                rng.integers(0, N_SAMPLES, n_obs),
                categories=[f"donor{i // 4 + 1:02d}_s{i % 4 + 1}" for i in range(N_SAMPLES)],
            ),
            "total_counts": total[:, 0].astype(np.float32),
            # time points as numbers (Split by offers numbers with few values)
            "day": np.array([0.0, 3.5, 7.0, 14.0])[np.arange(n_obs) % 4],
            "is_doublet": rng.random(n_obs) < 0.04,
        },
        index=pd.Index([f"cell{i:06d}" for i in range(n_obs)], name="barcode"),
    )
    var = pd.DataFrame(index=pd.Index(gene_names, name="gene"))
    adata = ad.AnnData(
        X=x,
        obs=obs,
        var=var,
        obsm={"X_umap": umap.astype(np.float32)},
        layers={"counts": counts_csr},
        uns={
            "cell_type_colors": [
                "#1b9e77",
                "#d95f02",
                "#7570b3",
                "#e7298a",
                "#66a61e",
                "#e6ab02",
                "#a6761d",
                "#b2182b",
                "#2166ac",
                "#8c510a",
                "#01665e",
                "#762a83",
            ]
        },
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    adata.write_h5ad(out)
    return out
