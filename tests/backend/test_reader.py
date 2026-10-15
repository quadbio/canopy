"""Obs columns as served (`to_obs_values`)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from canopy.data.reader import to_obs_values


def test_nullable_whole_numbers_beyond_float32_stay_exact() -> None:
    """Int64 [16777217, 123456789] came out as [16777216, 123456792] (review full-2 B-L1)."""
    v = to_obs_values(pd.array([16_777_217, None, 123_456_789], dtype="Int64"))
    assert v.values.dtype == np.float64 and v.values[0] == 16_777_217 and np.isnan(v.values[1])
    small = to_obs_values(pd.array([1, None, 3], dtype="Int64"))
    assert small.values.dtype == np.float32


def test_embeddings_made_for_viewing_come_first() -> None:
    """A new view opens the first embedding: UMAP, then t-SNE, then other visualisation
    methods, then other 2-column embeddings, then PCA and latent spaces (owner, 2026-10-15)."""
    from canopy.data.reader import embedding_rank

    def order(keys: dict[str, int]) -> list[str]:
        return sorted(keys, key=lambda k: embedding_rank(k, keys[k]))

    assert order({"X_pca": 50, "X_scVI": 30, "X_tsne": 2, "X_umap_harmony": 2, "X_umap": 2}) == [
        "X_umap",
        "X_umap_harmony",
        "X_tsne",
        "X_pca",
        "X_scVI",
    ]
    assert order({"X_pca": 50, "X_draw_graph_fa": 2, "spatial": 2, "X_diffmap": 15}) == [
        "X_draw_graph_fa",
        "spatial",
        "X_pca",
        "X_diffmap",
    ]
    # a 3-D UMAP still beats a 2-D anything else; among UMAPs, 2 columns first
    assert order({"X_umap_3d": 3, "X_umap_2d": 2, "X_phate": 2}) == [
        "X_umap_2d",
        "X_umap_3d",
        "X_phate",
    ]
    assert order({"X_Latent": 10, "X_PCA": 50}) == ["X_PCA", "X_Latent"]  # shorter name first
