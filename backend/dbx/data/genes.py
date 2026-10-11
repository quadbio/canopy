"""Gene expression for the preview path: which matrices can be read per gene, the gene list, and
one gene's values encoded for the browser.

How a gene is read depends on the matrix layout (from the fingerprint, no file access):
- CSC (gene-major) and dense: read directly, one column at a time ("direct");
- CSR (cell-major): one gene needs the whole matrix, so the matrix is loaded once into memory as
  CSC, if it fits the limit (`memory.small_file_gb`, and half the cache budget) ("memory");
  larger CSR matrices need conversion ("needs_conversion").

Values are sent sparse (uint32 cell indices, then values) when that is smaller than one value
per cell, else dense; optionally quantised to uint16 (value = offset + code * scale).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from dbx.inspect.models import AnnDataInfo, MatrixInfo

Access = Literal["direct", "memory", "needs_conversion"]

# var columns that usually hold readable gene symbols when the index holds ids.
_SYMBOL_COLUMNS = (
    "gene_symbols",
    "gene_symbol",
    "symbol",
    "symbols",
    "feature_name",
    "gene_name",
    "gene_names",
    "gene",
    "genes",
    "name",
)
_ENSEMBL = re.compile(r"^ENS[A-Z]*G\d{6,}")


def matrices(info: AnnDataInfo) -> dict[str, MatrixInfo]:
    """The matrices with one column per gene: X, then the layers."""
    out: dict[str, MatrixInfo] = {}
    if info.X is not None:
        out["X"] = info.X
    out.update(info.layers)
    return {k: m for k, m in out.items() if len(m.shape) == 2 and m.shape[1] == info.n_vars}


def matrix_bytes(m: MatrixInfo) -> int:
    item = np.dtype(m.dtype).itemsize
    if m.encoding == "dense":
        return int(np.prod(m.shape)) * item
    idx = np.dtype(m.indices_dtype or "int32").itemsize
    return (m.nnz or 0) * (item + idx) + (m.shape[1] + 1) * 8


def access(m: MatrixInfo, memory_limit: int) -> Access:
    if m.encoding in ("csc", "dense"):
        return "direct"
    return "memory" if matrix_bytes(m) <= memory_limit else "needs_conversion"


@dataclass
class GeneList:
    names: list[str]  # the var index (what requests and saved settings use)
    symbols: list[str] | None  # readable symbols, when the index holds ids
    symbol_column: str | None


def gene_list(var: pd.DataFrame) -> GeneList:
    names = [str(x) for x in var.index]
    lower = {str(c).lower(): c for c in var.columns}
    ids_like = sum(bool(_ENSEMBL.match(n)) for n in names[:200]) > min(100, len(names)) // 2
    if ids_like:
        for key in _SYMBOL_COLUMNS:
            col = lower.get(key)
            if col is None:
                continue
            s = var[col]
            if pd.api.types.is_numeric_dtype(s.dtype) or pd.api.types.is_bool_dtype(s.dtype):
                continue
            symbols = ["" if pd.isna(x) else str(x) for x in s]
            if symbols != names and sum(bool(x) for x in symbols) > len(symbols) // 2:
                return GeneList(names, symbols, str(col))
    return GeneList(names, None, None)


@dataclass
class GeneValues:
    """One gene across all cells: the cells with a stored (usually non-zero) value."""

    n: int
    indices: np.ndarray  # int64, sorted
    values: np.ndarray  # float32


def from_dense(col: np.ndarray) -> GeneValues:
    col = np.asarray(col, dtype=np.float32).ravel()
    nz = np.flatnonzero(col)
    return GeneValues(len(col), nz.astype(np.int64), col[nz])


def from_column(indices: np.ndarray, data: np.ndarray, n: int) -> GeneValues:
    """One column of a CSC matrix: its row indices and values."""
    order = np.argsort(indices, kind="stable")  # usually sorted already
    idx = np.asarray(indices, dtype=np.int64)[order]
    return GeneValues(n, idx, np.asarray(data, dtype=np.float32)[order])


def from_scipy_csc(m: object, i: int) -> GeneValues:
    a, b = int(m.indptr[i]), int(m.indptr[i + 1])  # type: ignore[attr-defined]
    return from_column(m.indices[a:b], m.data[a:b], m.shape[0])  # type: ignore[attr-defined]


@dataclass
class Stats:
    min: float
    max: float
    p99: float  # 99th percentile of the stored non-zero values (colour scale top)
    nnz: int


def stats(g: GeneValues) -> Stats:
    v = g.values[np.isfinite(g.values)]
    nonzero = v[v != 0]
    lo = float(min(v.min(initial=0.0), 0.0)) if g.n > len(v) else float(v.min(initial=0.0))
    hi = float(v.max(initial=0.0))
    p99 = float(np.percentile(nonzero, 99)) if len(nonzero) else hi
    return Stats(min=lo, max=max(hi, lo), p99=p99, nnz=int(len(nonzero)))


@dataclass
class Encoded:
    body: bytes
    headers: dict[str, str]


def encode(g: GeneValues, quantise: bool) -> Encoded:
    """Sparse or dense, float32 or uint16; headers describe it (see module docstring)."""
    st = stats(g)
    value_bytes = 2 if quantise else 4
    sparse = len(g.indices) * (4 + value_bytes) < g.n * value_bytes
    if sparse:
        vals = g.values
    else:
        vals = np.zeros(g.n, dtype=np.float32)
        vals[g.indices] = g.values
    headers = {
        "x-dbx-encoding": "sparse" if sparse else "dense",
        "x-dbx-n": str(g.n),
        "x-dbx-nnz": str(len(g.indices)),
        "x-dbx-min": repr(st.min),
        "x-dbx-max": repr(st.max),
        "x-dbx-p99": repr(st.p99),
    }
    if quantise:
        finite = vals[np.isfinite(vals)]
        lo = float(finite.min()) if len(finite) else 0.0
        hi = float(finite.max()) if len(finite) else 0.0
        scale = (hi - lo) / 65534 if hi > lo else 1.0
        # 65535 marks a missing value (NaN); the rest maps linearly onto [lo, hi].
        codes = np.full(len(vals), 65535, dtype="<u2")
        ok = np.isfinite(vals)
        codes[ok] = np.clip(np.rint((vals[ok] - lo) / scale), 0, 65534).astype("<u2")
        payload = codes
        headers |= {"x-dbx-offset": repr(lo), "x-dbx-scale": repr(scale)}
    else:
        payload = np.ascontiguousarray(vals, dtype="<f4")
    headers["x-dbx-dtype"] = payload.dtype.name
    body = payload.tobytes()
    if sparse:
        body = np.ascontiguousarray(g.indices, dtype="<u4").tobytes() + body
    return Encoded(body, headers)


def decode(body: bytes, headers: dict[str, str]) -> np.ndarray:
    """Inverse of `encode`, dense float32 (used by tests and by `dbx` tools)."""
    n, nnz = int(headers["x-dbx-n"]), int(headers["x-dbx-nnz"])
    sparse = headers["x-dbx-encoding"] == "sparse"
    dtype = np.dtype("<u2") if headers["x-dbx-dtype"] == "uint16" else np.dtype("<f4")
    off = 4 * nnz if sparse else 0
    raw = np.frombuffer(body, dtype=dtype, offset=off, count=nnz if sparse else n)
    if dtype.kind == "u":
        vals = float(headers["x-dbx-offset"]) + raw.astype(np.float64) * float(
            headers["x-dbx-scale"]
        )
        vals = np.where(raw == 65535, np.nan, vals).astype(np.float32)
    else:
        vals = raw.astype(np.float32)
    if not sparse:
        return vals
    out = np.zeros(n, dtype=np.float32)
    out[np.frombuffer(body, dtype="<u4", count=nnz)] = vals
    return out
