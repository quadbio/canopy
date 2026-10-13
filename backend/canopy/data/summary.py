"""Summaries for the Plot view: small results the browser draws, computed in one pass over
the values, so plots of atlases stay fast (ADR 0013).

- `violin`: a number per category: one histogram per category over a shared range, plus
  quartiles, mean, the share of values above 0 and the count of missing values.
- `bars`: two categorical columns: a table of counts (rows: the first column's categories).
- `density`: two numbers: counts on a grid of bins × bins over both ranges.

Missing values (NaN, code −1) are left out of the shapes and counted. Values are never sent,
only counts and statistics. Features with more than MAX_PLOT_CATEGORIES categories are refused
(`check_categories`): e.g. a cell id column chosen by mistake would make tables of millions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

MAX_BINS = 256
# More categories than this are not plotted (nor coloured by in the browser): the owner's limit,
# far above real annotations, low enough that a table of two such columns stays small.
MAX_PLOT_CATEGORIES = 1000


class TooManyCategoriesError(ValueError):
    """A categorical feature with more than MAX_PLOT_CATEGORIES categories."""

    expected = True
    code = "too_many_categories"

    def __init__(self, n: int) -> None:
        super().__init__(f"{n} categories (at most {MAX_PLOT_CATEGORIES})")
        self.n = n


@dataclass
class Feature:
    """One feature as the summaries need it: numbers (float32, NaN = missing) or category codes
    (−1 = missing) with their labels."""

    kind: str  # "number" | "category"
    values: np.ndarray
    labels: list[str] | None = None


def check_categories(*features: Feature) -> None:
    """Refuses before any table is allocated when a feature has too many categories."""
    for f in features:
        if f.kind == "category" and len(f.labels or []) > MAX_PLOT_CATEGORIES:
            raise TooManyCategoriesError(len(f.labels or []))


def _range(v: np.ndarray, log1p: bool) -> tuple[float, float]:
    ok = v[np.isfinite(v)]
    if not ok.size:
        return (0.0, 1.0)
    lo, hi = float(ok.min()), float(ok.max())
    if log1p:
        lo, hi = float(np.log1p(max(lo, 0.0))), float(np.log1p(max(hi, 0.0)))
    return (lo, hi if hi > lo else lo + 1.0)


def _transform(v: np.ndarray, log1p: bool) -> np.ndarray:
    return np.log1p(np.maximum(v, 0)) if log1p else v


def violin(cat: Feature, num: Feature, bins: int = 64, log1p: bool = False) -> dict[str, Any]:
    """Per category of `cat`: histogram of `num` over the shared range and its statistics."""
    bins = int(min(max(bins, 8), MAX_BINS))
    check_categories(cat)
    # the codes stay narrow (int8/int16): numpy sorts them by radix, ~40× faster than int64;
    # float32 values are plenty for histograms and quartiles, and half the memory
    codes = cat.values
    vals = num.values.astype(np.float32, copy=False)
    labels = cat.labels or []
    k = len(labels)
    lo, hi = _range(vals, log1p)
    tv = _transform(vals, log1p)
    in_cat = (codes >= 0) & (codes < k)
    ok = np.isfinite(tv) & in_cat
    c_ok = codes[ok]
    tv_ok = tv[ok]
    # one pass: (category, bin) pairs counted together
    b = np.clip(((tv_ok - lo) / (hi - lo) * bins).astype(np.int64), 0, bins - 1)
    hist = np.bincount(c_ok.astype(np.int64) * bins + b, minlength=k * bins).reshape(k, bins)
    # quartiles per category: values sorted by category (stable sort on small integer codes)
    order = np.argsort(c_ok, kind="stable")
    sorted_vals = tv_ok[order]
    counts = np.bincount(c_ok, minlength=k)
    starts = np.concatenate([[0], np.cumsum(counts)])
    raw = vals[ok][order]
    groups = []
    for c in range(k):
        g = sorted_vals[starts[c] : starts[c + 1]]
        r = raw[starts[c] : starts[c + 1]]
        if g.size:
            q1, med, q3 = (float(x) for x in np.quantile(g, [0.25, 0.5, 0.75]))
            groups.append(
                {
                    "n": int(g.size),
                    "hist": hist[c].tolist(),
                    "q1": q1,
                    "median": med,
                    "q3": q3,
                    "mean": float(g.mean(dtype=np.float64)),
                    "frac_above_zero": float((r > 0).mean()),
                }
            )
        else:
            groups.append({"n": 0, "hist": hist[c].tolist()})
    # each missing cell counted once: no category first, else no value
    missing = int((~np.isfinite(vals) & in_cat).sum())
    return {
        "kind": "violin",
        "categories": labels,
        "range": [lo, hi],
        "bins": bins,
        "log1p": log1p,
        "groups": groups,
        "n_missing_value": missing,
        "n_missing_category": int((~in_cat).sum()),
    }


def bars(a: Feature, b: Feature) -> dict[str, Any]:
    """Counts of cells per (category of a, category of b)."""
    check_categories(a, b)
    ka, kb = len(a.labels or []), len(b.labels or [])
    ca = a.values.astype(np.int64, copy=False)
    cb = b.values.astype(np.int64, copy=False)
    ok = (ca >= 0) & (ca < ka) & (cb >= 0) & (cb < kb)
    table = np.bincount(ca[ok] * kb + cb[ok], minlength=ka * kb).reshape(ka, kb)
    return {
        "kind": "bars",
        "rows": a.labels or [],
        "columns": b.labels or [],
        "counts": table.tolist(),
        "n_missing": int((~ok).sum()),
    }


def density(
    x: Feature, y: Feature, bins: int = 128, log1p_x: bool = False, log1p_y: bool = False
) -> dict[str, Any]:
    """Cells per cell of a bins × bins grid over both ranges."""
    bins = int(min(max(bins, 16), MAX_BINS))
    xv = _transform(x.values.astype(np.float64, copy=False), log1p_x)
    yv = _transform(y.values.astype(np.float64, copy=False), log1p_y)
    xr, yr = _range(x.values, log1p_x), _range(y.values, log1p_y)
    ok = np.isfinite(xv) & np.isfinite(yv)
    bx = np.clip(((xv[ok] - xr[0]) / (xr[1] - xr[0]) * bins).astype(np.int64), 0, bins - 1)
    by = np.clip(((yv[ok] - yr[0]) / (yr[1] - yr[0]) * bins).astype(np.int64), 0, bins - 1)
    grid = np.bincount(by * bins + bx, minlength=bins * bins)
    return {
        "kind": "density",
        "x_range": list(xr),
        "y_range": list(yr),
        "bins": bins,
        "log1p": [log1p_x, log1p_y],
        "counts": grid.tolist(),  # row-major, y then x (row 0 = lowest y)
        "max": int(grid.max()) if grid.size else 0,
        "n_missing": int((~ok).sum()),
    }
