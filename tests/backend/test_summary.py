"""Plot summaries (ADR 0013): violins, count tables and densities equal straightforward
calculations, through the API, for obs columns and genes (zeros included)."""

from __future__ import annotations

import anndata as ad
import numpy as np
import pandas as pd
import scipy.sparse as sp
from test_datasets import client_for

from canopy.catalog import entry_id
from canopy.config import Config


def _data(cfg: Config) -> tuple[ad.AnnData, str]:
    rng = np.random.default_rng(0)
    n = 5000
    a = ad.AnnData(X=sp.random(n, 20, density=0.3, format="csr", dtype=np.float32, random_state=1))
    a.obs["cluster"] = pd.Categorical(rng.choice(["b", "a", "c"], n))
    a.obs["sample"] = pd.Categorical(rng.choice(["s1", "s2"], n))
    a.obs["score"] = rng.normal(2, 1, n).astype(np.float32)
    a.obs.loc[a.obs.index[:7], "score"] = np.nan
    a.obs["depth"] = rng.uniform(0, 10, n).astype(np.float32)
    a.write_h5ad(cfg.data_roots[0].path / "p.h5ad")
    return a, entry_id("data", "p.h5ad")


def test_violin_of_a_gene_per_cluster(cfg: Config) -> None:
    a, id = _data(cfg)
    c = client_for(cfg)
    r = c.get(
        f"/api/datasets/{id}/summary", params={"kind": "violin", "x": "obs:cluster", "y": "gene:3"}
    )
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["categories"] == ["a", "b", "c"]
    g = np.asarray(a.X[:, 3].todense()).ravel()
    for k, cat in enumerate(s["categories"]):
        vals = g[(a.obs["cluster"] == cat).to_numpy()]
        grp = s["groups"][k]
        assert grp["n"] == vals.size and sum(grp["hist"]) == vals.size  # zeros counted
        assert np.isclose(grp["median"], np.median(vals))
        assert np.isclose(grp["q3"], np.quantile(vals, 0.75))
        assert np.isclose(grp["frac_above_zero"], (vals > 0).mean())
    # cached: the second answer is the same and an ETag works
    again = c.get(
        f"/api/datasets/{id}/summary", params={"kind": "violin", "x": "obs:cluster", "y": "gene:3"}
    )
    assert again.json() == s
    assert (
        c.get(
            f"/api/datasets/{id}/summary",
            params={"kind": "violin", "x": "obs:cluster", "y": "gene:3"},
            headers={"if-none-match": r.headers["etag"]},
        ).status_code
        == 304
    )


def test_violin_of_a_column_counts_missing_values(cfg: Config) -> None:
    a, id = _data(cfg)
    s = (
        client_for(cfg)
        .get(
            f"/api/datasets/{id}/summary",
            params={"kind": "violin", "x": "obs:sample", "y": "obs:score"},
        )
        .json()
    )
    assert s["n_missing_value"] == 7
    assert sum(g["n"] for g in s["groups"]) == len(a) - 7


def test_bars_equal_a_crosstab(cfg: Config) -> None:
    a, id = _data(cfg)
    s = (
        client_for(cfg)
        .get(
            f"/api/datasets/{id}/summary",
            params={"kind": "bars", "x": "obs:cluster", "y": "obs:sample"},
        )
        .json()
    )
    want = pd.crosstab(a.obs["cluster"], a.obs["sample"]).loc[s["rows"], s["columns"]].to_numpy()
    assert np.array_equal(np.asarray(s["counts"]), want)


def test_density_counts_every_cell_once(cfg: Config) -> None:
    a, id = _data(cfg)
    s = (
        client_for(cfg)
        .get(
            f"/api/datasets/{id}/summary",
            params={"kind": "density", "x": "obs:depth", "y": "obs:score", "bins": 32},
        )
        .json()
    )
    assert sum(s["counts"]) == len(a) - 7 and s["n_missing"] == 7 and len(s["counts"]) == 32 * 32
    want, _, _ = np.histogram2d(
        a.obs["score"].dropna(),
        a.obs["depth"][a.obs["score"].notna()],
        bins=32,
        range=[s["y_range"], s["x_range"]],
    )
    assert np.abs(np.asarray(s["counts"]).reshape(32, 32) - want).sum() <= 2 * 32  # edge bins


def test_wrong_kinds_are_explained(cfg: Config) -> None:
    _, id = _data(cfg)
    c = client_for(cfg)
    r = c.get(
        f"/api/datasets/{id}/summary",
        params={"kind": "violin", "x": "obs:score", "y": "obs:cluster"},
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_features"
    r = c.get(
        f"/api/datasets/{id}/summary", params={"kind": "violin", "x": "obs:nope", "y": "gene:1"}
    )
    assert r.status_code == 404
